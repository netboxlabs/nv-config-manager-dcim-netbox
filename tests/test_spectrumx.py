# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Behavioral tests for the Spectrum-X overlay/VRF methods (stubbed transport).

``_find_overlay`` (which resolves a site name to a Location id) and the stale-
assignment sweep are stubbed so each test isolates the target method's logic.
"""

from __future__ import annotations

from tests._fake import fake_client

_VXLANS = "plugins/nv-config-manager/vxlans/"
_OVERLAYS = "plugins/nv-config-manager/overlays/"
_ASSIGN = "plugins/nv-config-manager/overlay-assignments/"


def _client(gql=None, rest_list=None, record=None, overlay="unset"):
    patch = {}
    if overlay != "unset":

        async def fake_find_overlay(name, location=None):
            return overlay

        patch["_find_overlay"] = fake_find_overlay
    return fake_client(gql=gql, rest_list=rest_list, record=record, patch=patch)


async def test_get_spectrum_x_vrfs():
    gql = {
        "vrf_list": [
            {
                "id": "8",
                "name": "SpXTenant",
                "rd": "*:60000",
                "interfaces": [{"name": "swp1", "device": {"name": "leaf-01"}}],
            }
        ]
    }
    client = _client(gql=gql, rest_list={_VXLANS: [{"vrf": 8}]}, overlay={"id": 10})
    vrfs = await client.get_spectrum_x_vrfs("nvcm-spx", "DM-Akron")
    assert len(vrfs) == 1
    assert vrfs[0].vrf_id == "8" and vrfs[0].name == "SpXTenant"
    assert vrfs[0].interfaces == ("leaf-01:swp1",)


async def test_get_spectrum_x_vrfs_no_overlay():
    assert await _client(overlay=None).get_spectrum_x_vrfs("x", "DM-Akron") == []


async def test_delete_spectrum_x_vrf():
    record = []
    client = _client(
        rest_list={
            _VXLANS: [{"id": 5, "vrf": 8}],
            _ASSIGN: [{"id": 1, "assigned_object_type": "ipam.vrf", "assigned_object_id": 8}],
        },
        record=record,
    )
    await client.delete_spectrum_x_vrf("8", 60000)
    deletes = [path for method, path, _ in record if method == "DELETE"]
    assert f"{_VXLANS}5/" in deletes
    assert f"{_ASSIGN}1/" in deletes
    assert "ipam/vrfs/8/" in deletes


async def test_delete_overlay_if_unused_true_when_empty():
    record = []
    client = _client(rest_list={_VXLANS: []}, record=record, overlay={"id": 10})
    assert await client.delete_spectrum_x_overlay_if_unused("nvcm-spx", "DM-Akron") is True
    assert f"{_OVERLAYS}10/" in [p for m, p, _ in record if m == "DELETE"]


async def test_delete_overlay_if_unused_false_when_in_use():
    client = _client(rest_list={_VXLANS: [{"id": 5}]}, overlay={"id": 10})
    assert await client.delete_spectrum_x_overlay_if_unused("nvcm-spx", "DM-Akron") is False


async def test_reconcile_creates_missing_assignments():
    record = []
    client = _client(
        rest_list={_ASSIGN: []},  # nothing assigned yet
        record=record,
        overlay={"id": 10, "isolation_type": "spectrum_x_vrf"},
    )

    async def no_stale(overlay_id, interface_ids):
        return 0

    client._remove_stale_spx_interface_assignments = no_stale
    created, removed = await client.reconcile_spectrum_x_overlay_assignments(
        "nvcm-spx", "DM-Akron", "1", ["11", "12"]
    )
    assert (created, removed) == (3, 0)  # 1 device + 2 interfaces
    posts = [body for method, path, body in record if method == "POST" and path == _ASSIGN]
    assert len(posts) == 3
