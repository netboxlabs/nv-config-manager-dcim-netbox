# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Behavioral tests for the inventory / host / reference / IPAM parity fixes.

These cover the parity adaptations layered on top of the base suites:
unfiltered-query and duplicate-name guards, NVCM status-flag mapping and
filtering, managed-only scoping, host alias, MAC canonicalization, and IPAM
site scoping. Transport is stubbed throughout (no live NetBox).
"""

from __future__ import annotations

import pytest
from nv_config_manager_dcim import (
    DCIMDeviceSelectionFilter,
    DCIMInvalidDataError,
    DeviceInventoryFilter,
    create_dcim_client,
)

from nv_config_manager_dcim_netbox._mappers import (
    canonical_mac,
    host_metadata_from_gql,
    network_device_from_rest,
)
from nv_config_manager_dcim_netbox.fabric_common import STATUS_PATH
from tests._fake import fake_client as _client

# Area marker for `make <area>` / `pytest -m <area>`.
pytestmark = [pytest.mark.inventory, pytest.mark.reference, pytest.mark.ipam, pytest.mark.hosts]

_SETTINGS = {"server": "https://netbox.example", "token": "t"}


def _capture(*, rest_rows=None):
    """Return a client whose ``rest_list`` records every (path, params) call."""
    client = create_dcim_client("netbox", _SETTINGS)
    calls: list[tuple[str, list[tuple[str, str]]]] = []

    async def _rest_list(path, params=None, **kwargs):
        calls.append((path, list(params or [])))
        return list((rest_rows or {}).get(path, []))

    async def _graphql(query, variables=None):
        return {}

    client.rest_list = _rest_list
    client.graphql_query = _graphql
    client._calls = calls
    return client


def _rest_device(name="leaf-01", device_id=1, **extra):
    device = {
        "id": device_id,
        "name": name,
        "role": {"slug": "leaf"},
        "platform": {"slug": "cumulus-linux"},
        "device_type": {"slug": "sn2201"},
        "site": {"name": "DM-Akron"},
        "rack": {"name": "R1"},
        "primary_ip4": {"address": "10.0.0.1/24"},
    }
    device.update(extra)
    return device


# -- Fix 1: unfiltered-query guard -------------------------------------------


async def test_get_network_devices_empty_filter_raises():
    with pytest.raises(DCIMInvalidDataError):
        await _client().get_network_devices(DeviceInventoryFilter())


async def test_get_host_devices_empty_filter_raises():
    with pytest.raises(DCIMInvalidDataError):
        await _client().get_host_devices(DeviceInventoryFilter())


async def test_mac_only_filter_no_match_returns_empty_not_fleet():
    # A MAC filter that matches no interface must exclude everything, not raise
    # and not fall through to an unfiltered query.
    client = _client(rest_list={"dcim/interfaces/": []})
    result = await client.get_network_devices(
        DeviceInventoryFilter(mac_addresses=["aa:bb:cc:00:00:01"])
    )
    assert result == []


async def test_mac_and_device_ids_intersect_rather_than_union():
    # NetBox ORs a repeated id param, so appending device_ids and the MAC-resolved ids
    # separately returned their union: device 9 (not requested) plus 1/2 (wrong MAC).
    client = _capture(
        rest_rows={"dcim/interfaces/": [{"id": 5, "device": {"id": 9}}]},
    )
    result = await client.get_network_devices(
        DeviceInventoryFilter(device_ids=["1", "2"], mac_addresses=["aa:bb:cc:00:00:01"])
    )
    assert result == []
    # Empty intersection short-circuits: no device query is issued at all.
    assert not any(path == "dcim/devices/" for path, _ in client._calls)


async def test_mac_and_device_ids_intersection_keeps_the_overlap():
    client = _capture(
        rest_rows={
            "dcim/interfaces/": [{"id": 5, "device": {"id": 2}}, {"id": 6, "device": {"id": 9}}],
            "dcim/devices/": [_rest_device(name="leaf-02", device_id=2)],
        },
    )
    result = await client.get_network_devices(
        DeviceInventoryFilter(device_ids=["1", "2"], mac_addresses=["aa:bb:cc:00:00:01"])
    )
    assert [d.name for d in result] == ["leaf-02"]
    ids = [
        value
        for path, params in client._calls
        if path == "dcim/devices/"
        for key, value in params
        if key == "id"
    ]
    assert ids == ["2"]


# -- Fix 2: duplicate device names -------------------------------------------


async def test_get_network_devices_duplicate_name_raises():
    client = _client(
        rest_list={"dcim/devices/": [_rest_device(device_id=1), _rest_device(device_id=2)]}
    )
    with pytest.raises(DCIMInvalidDataError):
        await client.get_network_devices(DeviceInventoryFilter(site="DM-Akron"))


async def test_get_network_devices_no_platform_is_skipped_not_raised():
    # The deliberate NetBox improvement: an un-mappable (no-platform) device is
    # skipped, and the rest of the listing still returns.
    good = _rest_device(name="leaf-01", device_id=1)
    no_platform = _rest_device(name="leaf-02", device_id=2, platform=None)
    client = _client(rest_list={"dcim/devices/": [good, no_platform]})
    result = await client.get_network_devices(DeviceInventoryFilter(site="DM-Akron"))
    assert [d.name for d in result] == ["leaf-01"]


async def test_get_host_devices_duplicate_name_raises():
    client = _client(
        rest_list={"dcim/devices/": [{"id": 1}, {"id": 2}]},
        gql={"device_list": [{"id": 1, "name": "dup"}, {"id": 2, "name": "dup"}]},
    )
    with pytest.raises(DCIMInvalidDataError):
        await client.get_host_devices(DeviceInventoryFilter(site="DM-Akron"))


# -- Fix 3: NVCM status-flag mapping + filtering -----------------------------


def test_network_device_status_flags_from_the_device_config_status():
    status = {"render_enabled": True, "ztp_enabled": True}
    nd = network_device_from_rest(_rest_device(), status)
    assert nd.render_enabled is True
    assert nd.ztp_enabled is True
    # Flags absent from the status record degrade to False.
    assert nd.deploy_enabled is False
    assert nd.backup_enabled is False


def test_network_device_status_flags_default_false_without_a_status_record():
    nd = network_device_from_rest(_rest_device())
    assert (nd.render_enabled, nd.deploy_enabled, nd.backup_enabled, nd.ztp_enabled) == (
        False,
        False,
        False,
        False,
    )


def test_network_device_status_flags_ignore_device_custom_fields():
    # A hand-created or imported nvcm_* custom field is not a status record.
    device = _rest_device(custom_fields={"nvcm_render_enabled": True, "nvcm_ztp_enabled": True})
    assert network_device_from_rest(device).render_enabled is False


async def test_status_flag_filters_query_the_plugin_status_endpoint():
    client = _capture(rest_rows={STATUS_PATH: [{"id": 1, "device": 8}]})
    await client.get_network_devices(DeviceInventoryFilter(render_enabled=True, ztp_enabled=False))
    calls = dict((path, params) for path, params in client._calls)
    assert ("render_enabled", "true") in calls[STATUS_PATH]
    assert ("ztp_enabled", "false") in calls[STATUS_PATH]
    # Applied to the device read as an id selection, with no cf_* filter.
    assert ("id", "8") in calls["dcim/devices/"]
    assert not any(key.startswith("cf_nvcm_") for key, _ in calls["dcim/devices/"])


async def test_status_flags_matching_no_device_returns_empty_not_the_fleet():
    client = _capture(rest_rows={STATUS_PATH: []})
    assert await client.get_network_devices(DeviceInventoryFilter(render_enabled=True)) == []
    assert not any(path == "dcim/devices/" for path, _ in client._calls)


# -- Fix 4: list_statuses null / unknown content type ------------------------


async def test_list_statuses_unknown_content_type_still_raises():
    # The SDK contract (pinned by the base suite) requires a hard failure.
    client = create_dcim_client("netbox", _SETTINGS)

    async def _options(method, path, *, params=None, json=None):
        return {}

    client.rest_request = _options
    with pytest.raises(DCIMInvalidDataError):
        await client.list_statuses("dcim.nonexistent")


# -- Fix 5: IPAM site scoping -------------------------------------------------


async def test_namespace_route_distinguishers_scopes_by_site():
    # Zone marker 'z' + a namespace-name tag 'ns1'; namespace_id is the name.
    tags = [{"name": "z", "slug": "z"}, {"name": "ns1", "slug": "ns1"}]
    rows = {"ipam/vrfs/": [{"id": 1, "rd": "65000:1", "tags": tags}]}
    client = _capture(rest_rows=rows)
    result = await client.get_namespace_route_distinguishers("SiteX", "z")
    path, params = client._calls[-1]
    assert path == "ipam/vrfs/"
    assert ("cf_nvcm_site", "SiteX") in params
    assert [r.namespace_id for r in result] == ["ns1"]


async def test_list_namespace_tags_scopes_by_location():
    client = _capture(rest_rows={"ipam/vrfs/": []})
    await client.list_namespace_tags("SiteX")
    _, params = client._calls[-1]
    assert ("cf_nvcm_site", "SiteX") in params


async def test_list_namespace_tags_no_location_is_unscoped():
    client = _capture(rest_rows={"ipam/vrfs/": []})
    await client.list_namespace_tags()
    _, params = client._calls[-1]
    assert params == []


# -- Fix 6: host alias custom field ------------------------------------------


def test_host_metadata_alias_from_custom_field():
    device = {
        "id": 1,
        "name": "dpu-01",
        "tenant": {"name": "NGC"},
        "custom_fields": {"alias": "web-01"},
        "interfaces": [],
    }
    assert host_metadata_from_gql(device).alias == "web-01"


def test_host_metadata_alias_absent_is_none():
    device = {"id": 1, "name": "dpu-01", "tenant": {"name": "NGC"}, "interfaces": []}
    assert host_metadata_from_gql(device).alias is None


# -- Fix 7: MAC canonicalization ---------------------------------------------


def test_canonical_mac_is_case_and_separator_insensitive():
    assert canonical_mac("AA-BB-CC-00-00-01") == "aa:bb:cc:00:00:01"
    assert canonical_mac("aa:bb:cc:0:0:1") == "aa:bb:cc:00:00:01"
    assert canonical_mac("AABB.CC00.0001") == "aa:bb:cc:00:00:01"


def test_canonical_mac_handles_empty_and_invalid():
    assert canonical_mac(None) is None
    assert canonical_mac("") is None
    # An unparseable value is returned unchanged rather than raising.
    assert canonical_mac("not-a-mac") == "not-a-mac"


async def test_set_primary_mac_reuses_across_separators():
    # NetBox holds the MAC dash-separated; the request supplies it colon-
    # separated. Canonical matching reuses it instead of creating a duplicate.
    record: list[tuple[str, str, object]] = []
    client = _client(
        rest_list={"dcim/mac-addresses/": [{"id": 7, "mac_address": "AA-BB-CC-00-00-01"}]},
        record=record,
    )
    await client._set_interface_primary_mac("11", "aa:bb:cc:00:00:01")
    assert not any(method == "POST" for method, _, _ in record), "must not create a duplicate MAC"
    assert ("PATCH", "dcim/interfaces/11/", {"primary_mac_address": 7}) in record


# -- Fix 8: managed-only scoping ---------------------------------------------


async def test_list_devices_managed_only_scopes_by_render_enabled_status():
    client = _capture(rest_rows={STATUS_PATH: [{"id": 1, "device": 2}]})
    await client.list_devices(DCIMDeviceSelectionFilter(managed_only=True))
    calls = dict((path, params) for path, params in client._calls)
    assert ("render_enabled", "true") in calls[STATUS_PATH]
    assert ("id", "2") in calls["dcim/devices/"]


async def test_list_devices_managed_only_without_status_records_is_empty():
    client = _capture(rest_rows={STATUS_PATH: []})
    assert await client.list_devices(DCIMDeviceSelectionFilter(managed_only=True)) == []
    assert not any(path == "dcim/devices/" for path, _ in client._calls)


async def test_list_tenants_managed_only_collects_distinct_device_tenants():
    # The devices are read over GraphQL asking only for the nested tenant/role,
    # rather than paginating every field of the whole managed fleet over REST.
    devices = [
        {"tenant": {"id": 5, "name": "NGC"}},
        {"tenant": {"id": 5, "name": "NGC"}},
        {"tenant": {"id": 6, "name": "ACME"}},
        {"tenant": None},
    ]
    client = _client(
        rest_list={STATUS_PATH: [{"id": i, "device": i} for i in (1, 2, 3, 4)]},
        gql={"device_list": devices},
    )
    result = await client.list_tenants(managed_only=True)
    assert sorted((s.id, s.name) for s in result) == [("5", "NGC"), ("6", "ACME")]


async def test_list_roles_managed_only_collects_distinct_device_roles():
    devices = [{"role": {"id": 2, "name": "Leaf"}}, {"role": {"id": 3, "name": "Spine"}}]
    client = _client(
        rest_list={STATUS_PATH: [{"id": 1, "device": 1}, {"id": 2, "device": 2}]},
        gql={"device_list": devices},
    )
    result = await client.list_roles(managed_only=True)
    assert sorted((s.id, s.name) for s in result) == [("2", "Leaf"), ("3", "Spine")]


async def test_managed_only_selections_without_status_records_query_nothing():
    client = _capture(rest_rows={STATUS_PATH: []})
    assert await client.list_roles(managed_only=True) == []
    assert not any(path == "dcim/devices/" for path, _ in client._calls)
