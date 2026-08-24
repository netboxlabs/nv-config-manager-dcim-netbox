# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Parity tests for the InfiniBand provider fixes.

Each test pins a behavior adapted from the reference DCIM provider to NetBox
idioms (custom fields, the Location parent tree, first-class Site, and the
overlay/assignment plugin contract). Transport and resolution helpers are
stubbed so each test isolates the fixed behavior.
"""

from __future__ import annotations

import pytest
from nv_config_manager_dcim import (
    DCIMConflictError,
    DCIMInvalidDataError,
    DCIMNotFoundError,
)

from nv_config_manager_dcim_netbox.fabric_common import normalize_pkey
from nv_config_manager_dcim_netbox.infiniband import NetBoxInfiniBandMixin
from tests._fake import fake_client as _client

# Area marker for `make <area>` / `pytest -m <area>`.
pytestmark = pytest.mark.ib

_PKEYS = "plugins/nv-config-manager/ib-pkeys/"
_OVERLAYS = "plugins/nv-config-manager/overlays/"
_ASSIGN = "plugins/nv-config-manager/overlay-assignments/"

# _adopt_orphan_pkey is handed the caller's own PKey query rather than repeating it,
# so the rows are passed in rather than stubbed on the transport.
_ORPHAN_ROWS = [{"id": 20, "pkey": "0x7fff", "overlay": None}]


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


# -- Fix 1: pkey validation -----------------------------------------------------


def test_normalize_pkey_canonicalizes_valid():
    assert normalize_pkey("0x1") == "0x0001"
    assert normalize_pkey("0X7FFF") == "0x7fff"
    assert normalize_pkey("0x8001") == "0x8001"


@pytest.mark.parametrize("bad", ["", "1234", "0xGGGG", "0x12345", "nope", "0x"])
def test_normalize_pkey_rejects_malformed(bad):
    with pytest.raises(DCIMInvalidDataError):
        normalize_pkey(bad)


# -- Fix 2: IB location-chain walk ----------------------------------------------


def test_ib_location_chain_walks_parents():
    chain = NetBoxInfiniBandMixin._ib_location_chain(
        {"id": 5, "parent": {"id": 9, "parent": {"id": 12}}}
    )
    assert [loc["id"] for loc in chain] == [5, 9, 12]


def test_ib_location_chain_handles_none():
    assert NetBoxInfiniBandMixin._ib_location_chain(None) == []


async def test_resolve_ib_pkey_context_matches_ancestor_overlay():
    """A PKey whose overlay is scoped to an ANCESTOR location still resolves.

    Via exact ``scope_id``, not the descendant-inclusive ``location_id``.
    """
    overlay = {"id": 10, "name": "ib-part", "location": 9}
    pkey_row = {"id": 20, "pkey": "0x7fff", "overlay": 10}
    seen: list[list[tuple[str, str]]] = []

    async def rest_list(path, params=None, **kwargs):
        params = params or []
        if path.endswith("overlays/"):
            seen.append(list(params))
            scope_ids = {value for key, value in params if key == "scope_id"}
            # Overlay lives at ancestor location 9; only returned when queried.
            return [overlay] if "9" in scope_ids else []
        if path.endswith("ib-pkeys/"):
            return [pkey_row]
        return []

    client = _client()
    client.rest_list = rest_list

    async def resolve(host):
        return {
            "id": "1",
            "name": "host-01",
            "site": {"id": "2", "name": "DM-Akron"},
            "location": {"id": 5, "parent": {"id": 9}},
        }

    client._resolve_ib_device = resolve
    ctx = await client.resolve_ib_pkey_context("host-01", "0x7fff")
    assert ctx.overlay_id == "10" and ctx.pkey_id == "20"
    # The exact ancestor chain, scoped to Locations so the GFK id cannot collide.
    assert {v for k, v in seen[0] if k == "scope_id"} == {"5", "9"}
    assert ("scope_type", "dcim.location") in seen[0]
    assert not any(key == "location_id" for key, _ in seen[0])


async def test_resolve_ib_pkey_context_ignores_a_sibling_locations_overlay():
    """The ancestor chain must not pull in the ancestor's other descendants.

    Host in pod1 (parent dc1), same PKey on an overlay in sibling pod2. Through
    ``location_id``, dc1 expanded to its whole subtree and pod2's overlay matched.
    """
    pod2_overlay = {"id": 11, "name": "ib-part-pod2"}
    pkey_row = {"id": 21, "pkey": "0x8001", "overlay": 11}

    async def rest_list(path, params=None, **kwargs):
        params = params or []
        if path.endswith("overlays/"):
            scope_ids = {value for key, value in params if key == "scope_id"}
            # pod2 is location 7; it is not in the host's ancestor chain (5, 9).
            return [pod2_overlay] if "7" in scope_ids else []
        if path.endswith("ib-pkeys/"):
            return [pkey_row]
        return []

    client = _client()
    client.rest_list = rest_list

    async def resolve(host):
        return {
            "id": "1",
            "name": "host-01",
            "site": {"id": "2", "name": "DM-Akron"},
            "location": {"id": 5, "parent": {"id": 9}},
        }

    client._resolve_ib_device = resolve
    with pytest.raises(DCIMNotFoundError):
        await client.resolve_ib_pkey_context("host-01", "0x8001")


async def test_resolve_ib_pkey_context_skips_malformed_stored_pkey():
    """A stored PKey row with a garbage value is skipped, not fatal, during the walk."""
    overlay = {"id": 10, "name": "ib-part"}

    async def rest_list(path, params=None, **kwargs):
        if path.endswith("overlays/"):
            return [overlay]
        if path.endswith("ib-pkeys/"):
            return [
                {"id": 19, "pkey": "garbage", "overlay": 10},
                {"id": 20, "pkey": "0x7fff", "overlay": 10},
            ]
        return []

    client = _client()
    client.rest_list = rest_list

    async def resolve(host):
        return {
            "id": "1",
            "name": "host-01",
            "site": {"id": "2", "name": "S"},
            "location": {"id": 5},
        }

    client._resolve_ib_device = resolve
    ctx = await client.resolve_ib_pkey_context("host-01", "0x7fff")
    assert ctx.pkey_id == "20"


# -- Fix 3: host IPv4 resolution on any interface -------------------------------


async def test_resolve_ib_device_matches_non_primary_interface_ip():
    """The host IP lives on a non-primary interface; resolution still succeeds."""
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
        if names == ["host-01"]:
            # Device's PRIMARY ipv4 differs from the queried host IP.
            return {
                "device_list": [
                    {
                        "id": "1",
                        "name": "host-01",
                        "role": {"name": "UFM"},
                        "primary_ip4": {"address": "192.168.1.1/24"},
                        "site": {"id": "2", "name": "S"},
                    }
                ]
            }
        return {"device_list": []}

    client.graphql_query = gql
    # Old behavior filtered on primary_ip4 == host and raised NotFound; now it
    # resolves via the non-primary interface IP and returns the primary address.
    assert await client.canonicalize_ib_host("10.0.0.5") == "192.168.1.1"


# -- Fix 4: orphan adoption guards ----------------------------------------------


async def test_adopt_orphan_requires_device_tenant():
    client = _client()
    device = {"id": "1", "name": "host-01"}  # no tenant
    with pytest.raises(DCIMInvalidDataError):
        await client._adopt_orphan_pkey("0x7fff", device, 5, _ORPHAN_ROWS)


async def test_adopt_orphan_rejects_multiple_orphans():
    client = _client()
    device = {"id": "1", "name": "host-01", "tenant": {"id": 7}}
    rows = [*_ORPHAN_ROWS, {"id": 21, "pkey": "0x7fff", "overlay": None}]
    with pytest.raises(DCIMConflictError):
        await client._adopt_orphan_pkey("0x7fff", device, 5, rows)


async def test_adopt_orphan_creates_overlay_with_tenant_and_links():
    record = []
    client = _client(
        rest_list={_OVERLAYS: []},
        post={_OVERLAYS: {"id": 10}},
        record=record,
    )

    async def find_overlay(name, location=None, *, scope_type=None):
        return None

    client._find_overlay = find_overlay
    device = {"id": "1", "name": "host-01", "tenant": {"id": 7}}
    overlay, pk = await client._adopt_orphan_pkey("0x7fff", device, 5, _ORPHAN_ROWS)
    assert overlay["id"] == 10 and pk["id"] == 20
    posts = [(path, body) for method, path, body in record if method == "POST"]
    assert posts and posts[0][1]["tenant"] == 7
    patches = [(path, body) for method, path, body in record if method == "PATCH"]
    assert patches == [(f"{_PKEYS}20/", {"overlay": 10})]


# -- Fix 5: GUID case-insensitive lookup ----------------------------------------


async def test_find_ib_interfaces_by_guids_uses_case_insensitive_lookup():
    captured = []

    async def rest_list(path, params=None, **kwargs):
        captured.append((path, params))
        return [
            {
                "id": 11,
                "name": "ib1",
                "device": {"name": "host-01"},
                "custom_fields": {"ib_guid": "0xaa"},
            }
        ]

    client = _client()
    client.rest_list = rest_list
    result = await client.find_ib_interfaces_by_guids(["0xAA"])
    assert result[0].interface_id == "11"
    # Mixed-case input matches the lowercase-stored GUID via NetBox's __ie lookup.
    assert ("dcim/interfaces/", [("cf_ib_guid", "0xAA")]) in captured


# -- Fix 6: return assignment ids (not interface ids) ---------------------------


async def test_remove_ib_pkey_assignments_returns_assignment_ids():
    client = _client(rest_list={_ASSIGN: [_iface_assignment(30, 11)]})
    removed, unassigned = await client.remove_ib_pkey_assignments("10", ["11", "12"])
    assert removed == ["30"]  # assignment id of the deleted row
    assert unassigned == ["12"]  # interface id that had no assignment


async def test_sync_ib_pkey_assignments_returns_assignment_ids():
    rows = [
        {
            "id": 30,
            "assigned_object_type": "dcim.interface",
            "assigned_object_id": 11,
            "membership_type": "full",
        }
    ]
    client = _client(rest_list={_ASSIGN: rows}, post={_ASSIGN: {"id": 31}})
    added, removed, unchanged = await client.sync_ib_pkey_assignments(
        "10", [("12", "0xBB", "full")]
    )
    assert added == ["31"] and removed == ["30"] and unchanged == []


# -- Fix 7: no-site guard -------------------------------------------------------


async def test_resolve_ib_host_site_requires_site():
    client = _client()

    async def resolve(host):
        return {"id": "1", "name": "host-01", "primary_ip4": {"address": "10.0.0.5/24"}}

    client._resolve_ib_device = resolve
    with pytest.raises(DCIMInvalidDataError):
        await client.resolve_ib_host_site("host-01")


async def test_resolve_ib_host_site_with_site():
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
    assert site.site_id == "2" and site.site_name == "DM-Akron"


async def test_resolve_ib_pkey_context_requires_site():
    async def rest_list(path, params=None, **kwargs):
        return []

    client = _client()
    client.rest_list = rest_list

    async def resolve(host):
        return {"id": "1", "name": "host-01", "location": {"id": 5}}  # no site

    client._resolve_ib_device = resolve
    with pytest.raises(DCIMInvalidDataError):
        await client.resolve_ib_pkey_context("host-01", "0x7fff")


# -- Fix 8: membership_type normalization ---------------------------------------


async def test_sync_membership_normalized_avoids_redundant_patch():
    record = []
    rows = [
        {
            "id": 30,
            "assigned_object_type": "dcim.interface",
            "assigned_object_id": 11,
            "membership_type": "Full",
        }
    ]
    client = _client(rest_list={_ASSIGN: rows}, record=record)
    added, removed, unchanged = await client.sync_ib_pkey_assignments(
        "10", [("11", "0xAA", " FULL ")]
    )
    assert added == [] and removed == [] and unchanged == ["30"]
    assert [m for m, _p, _b in record if m == "PATCH"] == []


async def test_ensure_membership_normalized_avoids_redundant_patch():
    record = []
    client = _client(
        rest_list={_ASSIGN: [_iface_assignment(30, 11, membership_type="Full")]},
        record=record,
    )
    ids = await client.ensure_ib_pkey_assignments("10", [("11", "0xAA", "full")])
    assert ids == ["30"]
    assert [m for m, _p, _b in record if m == "PATCH"] == []


async def test_ensure_membership_change_still_patches():
    record = []
    client = _client(
        rest_list={_ASSIGN: [_iface_assignment(30, 11, membership_type="full")]},
        record=record,
    )
    await client.ensure_ib_pkey_assignments("10", [("11", "0xAA", "limited")])
    patches = [(p, b) for m, p, b in record if m == "PATCH"]
    assert patches == [(f"{_ASSIGN}30/", {"membership_type": "limited"})]


# -- guard: a resolvable-but-missing PKey still raises NotFound ------------------


async def test_resolve_ib_pkey_context_missing_raises_not_found():
    async def rest_list(path, params=None, **kwargs):
        return []

    client = _client()
    client.rest_list = rest_list

    async def resolve(host):
        return {
            "id": "1",
            "name": "host-01",
            "site": {"id": "2", "name": "S"},
            "location": {"id": 5},
        }

    client._resolve_ib_device = resolve
    with pytest.raises(DCIMNotFoundError):
        await client.resolve_ib_pkey_context("host-01", "0x7fff")


async def test_ensure_assignments_reads_the_overlay_once_for_many_interfaces():
    # One REST list per interface meant a round trip per port.
    calls: list[list[tuple[str, str]]] = []
    client = _client(post={_ASSIGN: {"id": 99}})
    rows = [_iface_assignment(30 + i, 10 + i, membership_type="full") for i in range(4)]

    async def rest_list(path, params=None, **kwargs):
        calls.append(list(params or []))
        return rows if path == _ASSIGN else []

    client.rest_list = rest_list
    result = await client.ensure_ib_pkey_assignments(
        "10", [(str(10 + i), f"0x{i:02x}", "full") for i in range(4)]
    )
    assert result == ["30", "31", "32", "33"]
    assert len(calls) == 1
    assert calls[0] == [("overlay_id", "10")]


async def test_assignment_lookup_requires_the_interface_content_type():
    # assigned_object_id is a GFK id, so a device and an interface can share a pk.
    # A device-scoped row must not satisfy an interface lookup.
    device_row = {"id": 40, "assigned_object_type": "dcim.device", "assigned_object_id": 11}
    client = _client(rest_list={_ASSIGN: [device_row]}, post={_ASSIGN: {"id": 99}})
    result = await client.ensure_ib_pkey_assignments("10", [("11", "0xAA", "full")])
    assert result == ["99"], "must create, not adopt the device-scoped row"
