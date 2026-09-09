# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Behavioral tests for core inventory methods (stubbed transport)."""

from __future__ import annotations

import re

import pytest
from nv_config_manager_dcim import (
    DCIMConflictError,
    DCIMConnectivityError,
    DCIMDeviceSelectionFilter,
    DCIMNotFoundError,
    DeviceInventoryFilter,
)

from nv_config_manager_dcim_netbox.fabric_common import STATUS_PATH
from tests._fake import fake_client as _client

# Area marker for `make <area>` / `pytest -m <area>`.
pytestmark = pytest.mark.inventory

_GQL_DEVICE = {
    "id": "1",
    "name": "leaf-01",
    "role": {"name": "Leaf", "slug": "leaf"},
    "platform": {"name": "Cumulus Linux", "slug": "cumulus-linux"},
    "rack": {"name": "R1"},
    "site": {"name": "DM-Akron"},
    "primary_ip4": {"address": "10.0.0.1/24"},
    "serial": "ABC123",
}
_REST_DEVICE = {
    "id": 1,
    "name": "leaf-01",
    "role": {"slug": "leaf"},
    "platform": {"slug": "cumulus-linux"},
    "device_type": {"slug": "sn2201"},
    "site": {"name": "DM-Akron"},
    "rack": {"name": "R1"},
    "primary_ip4": {"address": "10.0.0.1/24"},
}


async def test_get_device_metadata():
    md = await _client(gql={"device": _GQL_DEVICE}).get_device_metadata("1")
    assert md is not None and md.name == "leaf-01" and md.site == "DM-Akron"


async def test_get_device_metadata_missing_returns_none():
    assert await _client(gql={"device": None}).get_device_metadata("1") is None


async def test_get_device_metadata_not_found_error_returns_none():
    # How NetBox actually reports it: a GraphQL error on the singular query,
    # classified by the transport as DCIMNotFoundError.
    client = _client()

    async def _missing(query, variables=None):
        raise DCIMNotFoundError("NetBox: Device matching query does not exist.")

    client.graphql_query = _missing
    assert await client.get_device_metadata("999999") is None


async def test_get_device_metadata_propagates_real_failures():
    # The old blanket except returned None here, making a broken read
    # indistinguishable from an absent device during reference validation.
    client = _client()

    async def _broken(query, variables=None):
        raise DCIMConnectivityError("NetBox GraphQL request failed")

    client.graphql_query = _broken
    with pytest.raises(DCIMConnectivityError):
        await client.get_device_metadata("1")


async def test_get_device_serial():
    assert await _client(gql={"device": {"serial": "ABC123"}}).get_device_serial("1") == "ABC123"


async def test_get_device_serial_missing_raises():
    with pytest.raises(DCIMNotFoundError):
        await _client(gql={"device": {"serial": None}}).get_device_serial("1")


async def test_get_device_selection_by_name():
    gql = {"device_list": [{"id": "1", "name": "leaf-01", "platform": {"slug": "cumulus-linux"}}]}
    sel = await _client(gql=gql).get_device_selection_by_name("leaf-01")
    assert (sel.id, sel.name, sel.platform) == ("1", "leaf-01", "cumulus-linux")


async def test_get_device_selection_by_name_none_raises():
    with pytest.raises(DCIMNotFoundError):
        await _client(gql={"device_list": []}).get_device_selection_by_name("nope")


async def test_get_device_selection_by_name_multiple_raises():
    gql = {"device_list": [{"id": "1", "name": "x"}, {"id": "2", "name": "x"}]}
    with pytest.raises(DCIMConflictError):
        await _client(gql=gql).get_device_selection_by_name("x")


async def test_list_devices():
    client = _client(rest_list={"dcim/devices/": [_REST_DEVICE]})
    result = await client.list_devices(DCIMDeviceSelectionFilter())
    assert [(s.id, s.name, s.platform) for s in result] == [("1", "leaf-01", "cumulus-linux")]


async def test_get_managed_device_metadata():
    # "Managed" comes from the plugin's DeviceConfigStatus, so the status record is
    # what puts device 1 in the result.
    client = _client(
        rest_list={
            STATUS_PATH: [{"id": 10, "device": 1, "render_enabled": True}],
            "dcim/devices/": [_REST_DEVICE],
        }
    )
    result = await client.get_managed_device_metadata()
    assert [m.name for m in result] == ["leaf-01"]


async def test_get_managed_device_metadata_without_status_records_returns_nothing():
    # Fail closed: no render-enabled DeviceConfigStatus must never fall through to
    # an unfiltered whole-fleet listing.
    client = _client(rest_list={STATUS_PATH: [], "dcim/devices/": [_REST_DEVICE]})
    assert await client.get_managed_device_metadata() == []


async def test_get_network_device():
    client = _client(rest_get={"dcim/devices/1/": _REST_DEVICE})
    dev = await client.get_network_device("1")
    assert dev.name == "leaf-01" and dev.site == "DM-Akron"
    # id is the NetBox pk, and intended_config_path derives from it, so
    # deploy/backup's load_intended_configuration fetches <pk>/startup.yaml from
    # the config-store keyed by that same provider-owned id.
    assert dev.id == "1"
    assert dev.intended_config_path == "1/startup.yaml"


async def test_get_network_device_missing_raises():
    with pytest.raises(DCIMNotFoundError):
        await _client(rest_get={}).get_network_device("1")


async def test_get_network_devices():
    client = _client(rest_list={"dcim/devices/": [_REST_DEVICE]})
    # An entirely unfiltered request now raises; supply a real filter (the mock
    # returns the canned device regardless of the params sent).
    result = await client.get_network_devices(DeviceInventoryFilter(roles=["leaf"]))
    assert [d.name for d in result] == ["leaf-01"]


async def test_get_network_devices_lowercases_status_filter():
    # Workflows default their status filter to the display label the reference
    # DCIM provider uses (["Active", "Provisioned"]). NetBox's status filter
    # matches the slug (lowercase), so the provider must slugify it; forwarding
    # "Active" verbatim makes NetBox 400 and stalls every site-scoped selection
    # (cable/hardware validation, site backup, site password rotation).
    from tests._fake import params_for

    queries: list = []
    client = _client(rest_list={"dcim/devices/": [_REST_DEVICE]}, queries=queries)
    await client.get_network_devices(
        DeviceInventoryFilter(roles=["tan-hleaf"], statuses=["Active", "Provisioned"])
    )
    sent = params_for(queries, "dcim/devices/")
    status_values = [v for k, v in sent if k == "status"]
    assert status_values == ["active", "provisioned"]


async def test_list_devices_lowercases_status_filter():
    from nv_config_manager_dcim import DCIMDeviceSelectionFilter

    from tests._fake import params_for

    queries: list = []
    client = _client(rest_list={"dcim/devices/": [_REST_DEVICE]}, queries=queries)
    await client.list_devices(DCIMDeviceSelectionFilter(statuses=["Active"]))
    sent = params_for(queries, "dcim/devices/")
    assert [v for k, v in sent if k == "status"] == ["active"]


async def test_get_device_interfaces():
    iface = {"id": 11, "name": "swp1", "device": {"name": "leaf-01"}, "vrf": {"id": 8}}
    client = _client(rest_list={"dcim/interfaces/": [iface]})
    result = await client.get_device_interfaces("1")
    assert [i.name for i in result] == ["swp1"] and result[0].vrf_id == "8"


async def test_list_devices_sends_a_site_id_not_a_slug():
    """NVCM passes the site id it got from `list_locations` (a NetBox PK).

    NetBox's `site` filter matches the *slug*; the id belongs under `site_id`.
    Sent as `site=1` NetBox rejects the whole request with HTTP 400
    ("Select a valid choice. 1 is not one of the available choices"), which fails
    device discovery for Multi-Deploy and anything else scoped by site.
    """
    from nv_config_manager_dcim import DCIMDeviceSelectionFilter

    from tests._fake import params_for

    queries: list = []
    client = _client(rest_list={"dcim/devices/": [_REST_DEVICE]}, queries=queries)
    await client.list_devices(DCIMDeviceSelectionFilter(sites=["1"]))
    sent = params_for(queries, "dcim/devices/")
    assert ("site_id", "1") in sent
    assert not [v for k, v in sent if k == "site"]


async def test_list_devices_slugifies_a_role_name():
    # Workflows send the display name ("TAN-HLEAF"); NetBox keys roles by slug.
    from nv_config_manager_dcim import DCIMDeviceSelectionFilter

    from tests._fake import params_for

    queries: list = []
    client = _client(rest_list={"dcim/devices/": [_REST_DEVICE]}, queries=queries)
    await client.list_devices(DCIMDeviceSelectionFilter(roles=["TAN-HLEAF"]))
    assert ("role", "tan-hleaf") in params_for(queries, "dcim/devices/")


async def test_a_slug_filter_is_passed_through_unchanged():
    from nv_config_manager_dcim import DCIMDeviceSelectionFilter

    from tests._fake import params_for

    queries: list = []
    client = _client(rest_list={"dcim/devices/": [_REST_DEVICE]}, queries=queries)
    await client.list_devices(DCIMDeviceSelectionFilter(roles=["tan-hleaf"]))
    assert ("role", "tan-hleaf") in params_for(queries, "dcim/devices/")


# Generated from django.utils.text.slugify, which is what NetBox fills its slug
# field with. Regenerate with:
#   python -c "from django.utils.text import slugify; print(slugify(NAME))"
_DJANGO_SLUGS = (
    ("DSX Air (DC1)", "dsx-air-dc1"),
    ("DSX_Air", "dsx_air"),
    ("Site_01 East", "site_01-east"),
    ("Rack 4.2", "rack-42"),
    ("Caf\u00e9 DC", "cafe-dc"),
    ("A--B", "a-b"),
    ("TAN-HLEAF", "tan-hleaf"),
    ("  spaced  ", "spaced"),
    ("a_", "a"),
    ("\u00dcn\u00efcod\u00e9", "unicode"),
    ("DC1/Row-3", "dc1row-3"),
    ("100G Spine", "100g-spine"),
    ("xxx!!!", "xxx"),
)


def test_slugify_matches_django():
    """A slug that differs by one character filters to an empty list, not an error.

    Underscores, dots and non-ASCII are where a naive regex diverges.
    """
    from nv_config_manager_dcim_netbox._mappers import slugify

    for name, expected in _DJANGO_SLUGS:
        assert slugify(name) == expected, name


def test_slug_or_id_covers_the_name_and_id_forms():
    from nv_config_manager_dcim_netbox._mappers import slug_or_id

    assert slug_or_id("site", "1") == ("site_id", "1")
    assert slug_or_id("role", "TAN-HLEAF") == ("role", "tan-hleaf")
    assert slug_or_id("tenant", "Acme Corp") == ("tenant", "acme-corp")
    # Idempotent for a value that is already a slug.
    assert slug_or_id("role", "tan-hleaf") == ("role", "tan-hleaf")


def test_slug_or_id_slugifies_punctuation_the_way_netbox_does():
    """NetBox's own slugify collapses every run of non-alphanumerics to one hyphen.

    Only whitespace and underscores were collapsed, so a site displayed as
    `DSX Air (DC1)` asked for `site=dsx-air-(dc1)`. NetBox answers that with 200
    and an empty list rather than rejecting it, so Multi-Deploy silently
    discovered no devices instead of failing loudly.
    """
    from nv_config_manager_dcim_netbox._mappers import slug_or_id

    assert slug_or_id("site", "DSX Air (DC1)") == ("site", "dsx-air-dc1")
    assert slug_or_id("site", "TRIAL01 - demo") == ("site", "trial01-demo")
    assert slug_or_id("tenant", "Acme, Inc.") == ("tenant", "acme-inc")
    # Slash is dropped, not hyphenated -- see _DJANGO_SLUGS.
    assert slug_or_id("role", "spine/leaf") == ("role", "spineleaf")
    # Every derived slug is one NetBox would accept: [-a-z0-9_]+ with no leading
    # or trailing separator.
    for name in ("DSX Air (DC1)", "  padded  ", "Acme, Inc.", "a--b", "Site_01 East"):
        key, value = slug_or_id("site", name)
        assert re.fullmatch(r"[a-z0-9_]+(-[a-z0-9_]+)*", value), (name, value)


async def test_a_platform_netbox_does_not_have_is_dropped_not_sent():
    """`Platform` is a fixed enum in the SDK, so a workflow sends whichever members
    it supports regardless of what the DCIM holds.

    NetBox's `platform` filter is a choice field over existing slugs, so an absent
    one fails the *whole* query with HTTP 400 ("Select a valid choice.
    juniper-junos is not one of the available choices"), which fails Site
    Configuration Backup outright on a Cumulus-only fabric.

    NetBox ORs a repeated param, so the platforms that do exist still select
    exactly the devices they should.
    """
    from nv_config_manager_dcim import DCIMDeviceSelectionFilter, Platform

    from tests._fake import params_for

    queries: list = []
    client = _client(
        rest_list={"dcim/devices/": [_REST_DEVICE]},
        gql={"platform_list": [{"slug": "cumulus-linux"}]},
        queries=queries,
    )
    await client.list_devices(
        DCIMDeviceSelectionFilter(platforms=[Platform.CUMULUS_LINUX, Platform.JUNIPER_JUNOS])
    )

    sent = [v for k, v in params_for(queries, "dcim/devices/") if k == "platform"]
    assert sent == ["cumulus-linux"], sent


async def test_no_matching_platform_selects_nothing_rather_than_everything():
    # Dropping the filter entirely would back up the whole fleet instead of the
    # nothing the caller actually asked for.
    from nv_config_manager_dcim import DCIMDeviceSelectionFilter, Platform

    from tests._fake import params_for

    queries: list = []
    client = _client(
        rest_list={"dcim/devices/": [_REST_DEVICE]},
        gql={"platform_list": [{"slug": "cumulus-linux"}]},
        queries=queries,
    )
    result = await client.list_devices(
        DCIMDeviceSelectionFilter(platforms=[Platform.JUNIPER_JUNOS])
    )

    assert result == []
    assert not params_for(queries, "dcim/devices/"), "queried NetBox with no usable filter"


async def test_the_platform_list_is_read_once_per_client():
    # Reference data on the hot path of every device selection.
    from nv_config_manager_dcim import DCIMDeviceSelectionFilter, Platform

    client = _client(
        rest_list={"dcim/devices/": [_REST_DEVICE]},
        gql={"platform_list": [{"slug": "cumulus-linux"}]},
    )
    calls = []
    inner = client.graphql_query

    async def _counting(query, variables=None):
        calls.append(query)
        return await inner(query, variables)

    client.graphql_query = _counting
    for _ in range(3):
        await client.list_devices(DCIMDeviceSelectionFilter(platforms=[Platform.CUMULUS_LINUX]))

    assert len([q for q in calls if "platform_list" in q]) == 1, calls


async def test_site_backup_survives_a_platform_the_fabric_does_not_have():
    """The failure reported from Site Configuration Backup.

    `site_backup.py` calls get_network_devices, which builds its params through a
    different path than list_devices. NetBox answered the whole query with
    HTTP 400 -- {"platform":["Select a valid choice. juniper-junos is not one of
    the available choices."]} -- so the workflow died instead of backing up the
    Cumulus devices it could see.
    """
    from nv_config_manager_dcim import DeviceInventoryFilter, Platform

    from tests._fake import params_for

    queries: list = []
    client = _client(
        rest_list={"dcim/devices/": [_REST_DEVICE]},
        gql={"platform_list": [{"slug": "cumulus-linux"}]},
        queries=queries,
    )
    result = await client.get_network_devices(
        DeviceInventoryFilter(
            sites=["1"], platforms=[Platform.CUMULUS_LINUX, Platform.JUNIPER_JUNOS]
        )
    )

    assert [d.name for d in result] == ["leaf-01"]
    sent = [v for k, v in params_for(queries, "dcim/devices/") if k == "platform"]
    assert sent == ["cumulus-linux"], sent


async def test_an_unreadable_platform_list_forwards_the_filter_unchecked():
    """Fail open, not closed.

    An empty platform list means either a NetBox with no platforms -- where a
    platform filter cannot match anyway -- or a `platform_list` read that came back
    empty. Treating the second as "nothing matches" would silently empty the
    result of every platform-scoped workflow, which is worse than the 400 this
    check exists to prevent.
    """
    from nv_config_manager_dcim import DCIMDeviceSelectionFilter, Platform

    from tests._fake import params_for

    queries: list = []
    client = _client(
        rest_list={"dcim/devices/": [_REST_DEVICE]}, gql={"platform_list": []}, queries=queries
    )
    result = await client.list_devices(
        DCIMDeviceSelectionFilter(platforms=[Platform.CUMULUS_LINUX])
    )

    assert [d.name for d in result] == ["leaf-01"]
    assert ("platform", "cumulus-linux") in params_for(queries, "dcim/devices/")
