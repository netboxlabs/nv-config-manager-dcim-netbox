# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Behavioral tests for the InfiniBand methods (stubbed transport + helpers).

The location/tenant/overlay/device resolution helpers are stubbed so each test
isolates the target method's own topology / partition / assignment logic.
"""

from __future__ import annotations

import pytest
from nv_config_manager_dcim import (
    DCIMInvalidDataError,
    IBHostSite,
    IBInterfaceGuid,
    IBPKeyAssignment,
    IBPKeyCleanup,
    IBPKeyContext,
    IBPKeyPartition,
    IBSwitchTopology,
)

from nv_config_manager_dcim_netbox.fabric_common import SITE_SCOPE_TYPE
from tests._fake import fake_client as _client

# Area marker for `make <area>` / `pytest -m <area>`.
pytestmark = pytest.mark.ib

_PKEYS = "plugins/nv-config-manager/ib-pkeys/"
_OVERLAYS = "plugins/nv-config-manager/overlays/"
_ASSIGN = "plugins/nv-config-manager/overlay-assignments/"


def _iface_assignment(assignment_id, interface_id, **extra):
    """An OverlayAssignment row as NetBox returns it.

    ``assigned_object_type`` matters: ``assigned_object_id`` is a GFK id, so a
    device and an interface can share a pk and the provider filters on the type.
    """
    return {
        "id": assignment_id,
        "assigned_object_type": "dcim.interface",
        "assigned_object_id": interface_id,
        **extra,
    }


# -- topology + GUIDs -----------------------------------------------------------


async def test_get_ib_switch_topology():
    gql = {
        "device_list": [
            {
                "id": "1",
                "name": "ibsw-01",
                "interfaces": [
                    {
                        "name": "ib1",
                        "connected_endpoints": [{"name": "ib9", "device": {"name": "host-01"}}],
                    }
                ],
            }
        ]
    }
    topo = await _client(gql=gql).get_ib_switch_topology(["1"])
    assert isinstance(topo, IBSwitchTopology)
    assert topo.switch_names == {"1": "ibsw-01"}
    assert topo.intended_neighbors["1"]["ib1"].device_name == "host-01"


async def test_get_ib_switch_topology_empty():
    topo = await _client().get_ib_switch_topology([])
    assert topo.switch_names == {} and topo.intended_neighbors == {}


async def test_get_ib_interface_guid():
    iface = {
        "id": 11,
        "name": "ib1",
        "device": {"name": "host-01"},
        "custom_fields": {"ib_guid": "0xGUID"},
    }
    guid = await _client(rest_get={"dcim/interfaces/11/": iface}).get_ib_interface_guid("11")
    assert isinstance(guid, IBInterfaceGuid)
    assert (guid.interface_name, guid.device_name, guid.guid) == ("ib1", "host-01", "0xGUID")


async def test_get_ib_interface_guids():
    gql = {
        "device_list": [
            {
                "name": "host-01",
                "interfaces": [{"id": 11, "name": "ib1", "custom_fields": {"ib_guid": "0xAA"}}],
            }
        ]
    }
    result = await _client(gql=gql).get_ib_interface_guids({("host-01", "ib1"), ("host-01", "ib2")})
    assert [(g.device_name, g.interface_name, g.guid) for g in result] == [
        ("host-01", "ib1", "0xAA")
    ]


async def test_set_ib_interface_guid():
    record = []
    await _client(record=record).set_ib_interface_guid("11", "0xBEEF")
    assert record == [("PATCH", "dcim/interfaces/11/", {"custom_fields": {"ib_guid": "0xBEEF"}})]


async def test_get_ib_interface_records():
    client = _client(
        rest_list={
            "dcim/interfaces/": [
                {
                    "id": 11,
                    "name": "ib1",
                    "device": {"name": "host-01"},
                    "custom_fields": {"ib_guid": "0xAA"},
                }
            ]
        }
    )
    result = await client.get_ib_interface_records([("host-01", "ib1")])
    assert [(g.interface_id, g.guid) for g in result] == [("11", "0xAA")]


async def test_find_ib_interfaces_by_guids():
    client = _client(
        rest_list={
            "dcim/interfaces/": [
                {
                    "id": 11,
                    "name": "ib1",
                    "device": {"name": "host-01"},
                    "custom_fields": {"ib_guid": "0xAA"},
                }
            ]
        }
    )
    result = await client.find_ib_interfaces_by_guids(["0xAA"])
    assert [g.interface_id for g in result] == ["11"]


# -- partitions -----------------------------------------------------------------


async def test_ensure_ib_pkey_partition_creates_overlay_and_pkey():
    record = []
    client = _client(
        rest_list={_PKEYS: []},  # no existing pkey -> create
        post={_OVERLAYS: {"id": 10}, _PKEYS: {"id": 20}},
        record=record,
    )

    async def resolve_scope(value, *, scope_type):
        assert scope_type == "dcim.location", scope_type
        return {"id": 5, "name": "Row 1", "slug": "row-1"}

    async def resolve_tenant(name):
        return None

    async def find_overlay(name, scope=None, *, scope_type=None):
        return None  # no existing overlay -> create

    client._resolve_scope = resolve_scope
    client._resolve_tenant_id = resolve_tenant
    client._find_overlay = find_overlay
    part = await client.ensure_ib_pkey_partition("0x7fff", "part-A", "Row 1", None, "full")
    assert isinstance(part, IBPKeyPartition)
    assert part.pkey == "0x7fff" and part.partition_id == "10" and part.pkey_id == "20"
    posts = {path for method, path, _ in record if method == "POST"}
    assert posts == {_OVERLAYS, _PKEYS}
    overlay_body = next(b for m, p, b in record if m == "POST" and p == _OVERLAYS)
    assert overlay_body["scope_type"] == "dcim.location" and overlay_body["scope_id"] == 5


async def test_ensure_orphan_ib_pkey_creates_when_absent():
    record = []
    client = _client(rest_list={_PKEYS: []}, post={_PKEYS: {"id": 20}}, record=record)
    part = await client.ensure_orphan_ib_pkey("0x7fff")
    assert part.pkey_id == "20" and part.pkey == "0x7fff"


async def test_cleanup_ib_pkey_partition_deletes_when_empty():
    record = []
    client = _client(rest_list={_ASSIGN: [], _PKEYS: []}, record=record)
    result = await client.cleanup_ib_pkey_partition(
        "10", "ib-pkey-overlay-0x7fff", "20", "0x7fff", ufm_partition_empty=True
    )
    assert isinstance(result, IBPKeyCleanup)
    assert result.pkey_deleted and result.overlay_deleted and result.partition_empty
    deletes = {path for method, path, _ in record if method == "DELETE"}
    assert f"{_PKEYS}20/" in deletes and f"{_OVERLAYS}10/" in deletes


async def test_cleanup_ib_pkey_partition_keeps_when_assigned():
    client = _client(rest_list={_ASSIGN: [{"id": 1}]})
    result = await client.cleanup_ib_pkey_partition(
        "10", "x", "20", "0x7fff", ufm_partition_empty=True
    )
    assert not result.pkey_deleted and result.remaining_assignments == 1


# -- assignments ----------------------------------------------------------------


async def test_ensure_ib_pkey_assignments_creates_new():
    record = []
    client = _client(rest_list={_ASSIGN: []}, post={_ASSIGN: {"id": 30}}, record=record)
    ids = await client.ensure_ib_pkey_assignments("10", [("11", "0xAA", "full")])
    assert ids == ["30"]
    assert any(m == "POST" and p == _ASSIGN for m, p, _ in record)


async def test_remove_ib_pkey_assignments():
    record = []
    client = _client(rest_list={_ASSIGN: [_iface_assignment(30, 11)]}, record=record)
    removed, unassigned = await client.remove_ib_pkey_assignments("10", ["11", "12"])
    # Contract: removed carries ASSIGNMENT ids (row 30); unassigned carries the
    # interface id (12) that had no assignment.
    assert removed == ["30"] and unassigned == ["12"]
    assert (f"{_ASSIGN}30/") in [p for m, p, _ in record if m == "DELETE"]


async def test_get_ib_pkey_assignments():
    rows = [
        {
            "id": 30,
            "assigned_object_type": "dcim.interface",
            "assigned_object_id": 11,
            "guid": "0xAA",
            "membership_type": "Full",
        }
    ]
    result = await _client(rest_list={_ASSIGN: rows}).get_ib_pkey_assignments("10")
    assert isinstance(result[0], IBPKeyAssignment)
    assert result[0].interface_id == "11" and result[0].membership_type == "full"


async def test_sync_ib_pkey_assignments():
    record = []
    # currently interface 11 assigned; desired = {12}. -> remove 11, add 12.
    rows = [
        {
            "id": 30,
            "assigned_object_type": "dcim.interface",
            "assigned_object_id": 11,
            "membership_type": "full",
        }
    ]
    client = _client(rest_list={_ASSIGN: rows}, post={_ASSIGN: {"id": 31}}, record=record)
    added, removed, unchanged = await client.sync_ib_pkey_assignments(
        "10", [("12", "0xBB", "full")]
    )
    # Contract: all three lists carry ASSIGNMENT ids -- new row 31 added, row 30 removed.
    assert added == ["31"] and removed == ["30"] and unchanged == []


async def test_sync_ib_pkey_assignments_patches_membership_change():
    """An interface that stays assigned but changes membership_type is PATCHed in
    place (the unchanged-but-membership-differs branch)."""
    record = []
    rows = [
        {
            "id": 30,
            "assigned_object_type": "dcim.interface",
            "assigned_object_id": 11,
            "membership_type": "full",
        }
    ]
    client = _client(rest_list={_ASSIGN: rows}, record=record)
    added, removed, unchanged = await client.sync_ib_pkey_assignments(
        "10", [("11", "0xAA", "limited")]
    )
    # unchanged carries the ASSIGNMENT id (row 30) that stayed, PATCHed in place.
    assert added == [] and removed == [] and unchanged == ["30"]
    patches = [(p, body) for m, p, body in record if m == "PATCH"]
    assert patches == [(f"{_ASSIGN}30/", {"membership_type": "limited"})]


# -- host / pkey-context resolution ---------------------------------------------


async def test_canonicalize_ib_host():
    client = _client()

    async def resolve(host):
        return {"id": "1", "name": "host-01", "primary_ip4": {"address": "10.0.0.5/24"}}

    client._resolve_ib_device = resolve
    assert await client.canonicalize_ib_host("host-01") == "10.0.0.5"


async def test_resolve_ib_device_ipv4_fallback():
    """When the host looks like an IPv4 and no device matches by name, resolve via
    the IP -> interface -> device fallback (the IPv4 branch of _resolve_ib_device)."""
    client = _client(
        rest_list={
            "ipam/ip-addresses/": [
                {
                    "address": "10.0.0.5/24",
                    "assigned_object_type": "dcim.interface",
                    "assigned_object": {"id": 11},
                }
            ],
            # The matching IPs' interfaces are resolved in one bulk query.
            "dcim/interfaces/": [{"id": 11, "device": {"name": "host-01"}}],
        },
    )

    async def gql(query, variables=None):
        names = (variables or {}).get("names") or []
        if names == ["host-01"]:  # resolved by IP -> interface -> device name
            return {
                "device_list": [
                    {
                        "id": "1",
                        "name": "host-01",
                        "role": {"name": "UFM"},
                        "primary_ip4": {"address": "10.0.0.5/24"},
                    }
                ]
            }
        return {"device_list": []}  # first lookup, by the IP-as-name: miss

    client.graphql_query = gql
    assert await client.canonicalize_ib_host("10.0.0.5") == "10.0.0.5"


async def test_resolve_ib_device_rejects_non_ufm_role():
    """A device answering to the host name but not carrying the UFM role is not the
    UFM appliance, so it must be rejected rather than driving UFM operations."""
    gql = {
        "device_list": [
            {
                "id": "1",
                "name": "host-01",
                "role": {"name": "Leaf Switch"},
                "primary_ip4": {"address": "10.0.0.5/24"},
            }
        ]
    }
    with pytest.raises(DCIMInvalidDataError, match="not assigned the UFM role"):
        await _client(gql=gql).canonicalize_ib_host("host-01")


async def test_resolve_ib_device_rejects_missing_role():
    gql = {"device_list": [{"id": "1", "name": "host-01", "primary_ip4": None}]}
    with pytest.raises(DCIMInvalidDataError, match="not assigned the UFM role"):
        await _client(gql=gql).canonicalize_ib_host("host-01")


async def test_resolve_ib_host_site():
    client = _client()

    async def resolve(host):
        return {
            "id": "1",
            "name": "host-01",
            "primary_ip4": {"address": "10.0.0.5/24"},
            "site": {"id": "2", "name": "DM-Akron"},
        }

    client._resolve_ib_device = resolve
    site = await client.resolve_ib_host_site("host-01")
    assert isinstance(site, IBHostSite)
    assert site.device_name == "host-01" and site.site_name == "DM-Akron"


async def test_resolve_ib_pkey_context():
    client = _client(
        rest_list={
            _OVERLAYS: [{"id": 10, "name": "ib-part"}],
            _PKEYS: [{"id": 20, "pkey": "0x7fff", "overlay": 10}],
        }
    )

    async def resolve(host):
        return {
            "id": "1",
            "name": "host-01",
            "site": {"id": "2", "name": "DM-Akron"},
            "location": {"id": 5},
        }

    client._resolve_ib_device = resolve
    ctx = await client.resolve_ib_pkey_context("host-01", "0x7fff")
    assert isinstance(ctx, IBPKeyContext)
    assert ctx.overlay_id == "10" and ctx.pkey_id == "20" and ctx.pkey == "0x7fff"


async def test_resolve_ib_pkey_context_falls_back_to_the_site():
    """Device.location is optional in NetBox; the Site is not.

    The reference provider's location chain is one typed tree, so its ancestor
    walk reaches the Site. Here the Site sits outside the Location tree, so it is
    searched as a second scope -- which is also what makes a Site-anchored
    partition reachable, and what stops a device with no Location from failing.
    """
    seen: list[tuple[str, list]] = []
    client = _client(
        rest_list={
            _OVERLAYS: [{"id": 10, "name": "site-part"}],
            _PKEYS: [{"id": 20, "pkey": "0x7fff", "overlay": 10}],
        }
    )
    inner = client.rest_list

    async def spy(path, params=None, **kwargs):
        seen.append((path, list(params or [])))
        return await inner(path, params, **kwargs)

    client.rest_list = spy

    async def resolve(host):
        return {"id": "1", "name": "host-01", "site": {"id": "2", "name": "DM-Akron"}}

    client._resolve_ib_device = resolve
    ctx = await client.resolve_ib_pkey_context("host-01", "0x7fff")

    assert ctx.overlay_id == "10"
    # Scoped to the Site, never unscoped -- an unscoped query would match every
    # ib_pkey overlay in the install, including another site's.
    overlay_queries = [params for path, params in seen if path == _OVERLAYS]
    assert overlay_queries
    for params in overlay_queries:
        assert ("scope_type", SITE_SCOPE_TYPE) in params
        assert ("scope_id", "2") in params


async def test_resolve_ib_pkey_context_needs_a_site_or_location():
    """With neither, the overlay query would be unscoped."""
    client = _client(rest_list={_OVERLAYS: [], _PKEYS: []})

    async def resolve(host):
        return {"id": "1", "name": "host-01"}

    client._resolve_ib_device = resolve
    with pytest.raises(DCIMInvalidDataError):
        await client.resolve_ib_pkey_context("host-01", "0x7fff")


async def test_host_site_round_trips_into_a_partition():
    """NVCM feeds the resolved site straight back as the partition's location.

    `ib_pkey_creation` builds `dcim_location_reference(location_id, location_type)`
    from the resolver output, so `IBHostSite.site_type` is what stops that Site pk
    being looked up among Locations -- where a colliding pk matches the wrong one.
    """
    from nv_config_manager_dcim import dcim_location_reference

    writes: list[tuple[str, str, dict]] = []
    client = _client(
        rest_list={
            # A Location and a Site both holding pk 1: the collision the type resolves.
            "dcim/sites/": [{"id": 1, "name": "TEST-SITE", "slug": "test-site"}],
            "dcim/locations/": [{"id": 1, "name": "Some Other Module", "slug": "other"}],
            "tenancy/tenants/": [{"id": 7, "name": "Demo Tenant", "slug": "demo-tenant"}],
            _OVERLAYS: [],
            _PKEYS: [],
        },
        record=writes,
    )

    async def resolve(host):
        return {
            "id": "1",
            "name": "gpu-01",
            "site": {"id": 1, "name": "TEST-SITE"},
            "location": {"id": 2, "name": "Module1", "parent": None},
        }

    client._resolve_ib_device = resolve
    host_site = await client.resolve_ib_host_site("gpu-01")

    assert host_site.site_type == "Site"

    await client.ensure_ib_pkey_partition(
        "0x7fff",
        "partition-a",
        dcim_location_reference(host_site.site_id, host_site.site_type),
        "Demo Tenant",
        "full",
    )

    overlay = next(body for _m, path, body in writes if path == _OVERLAYS)
    # Anchored on the Site it resolved from, not the Location of the same pk.
    assert overlay["scope_type"] == SITE_SCOPE_TYPE
    assert str(overlay["scope_id"]) == "1"
