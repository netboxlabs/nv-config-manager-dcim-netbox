# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Namespace-as-VRF mapping tests (transport stubbed).

NetBox has no first-class IPAM namespace: a namespace is a VRF, and a namespace
*zone* (a set of VRFs that must hold unique RDs) is emulated with a tag on those
VRFs. These verify the tag grouping and RD collection without a live NetBox.
"""

from __future__ import annotations

from nv_config_manager_dcim import create_dcim_client

_SETTINGS = {"server": "https://netbox.example", "token": "t"}

_VRFS = [
    {"id": 1, "name": "blue", "rd": "65000:1", "tags": [{"name": "zone-a", "slug": "zone-a"}]},
    {"id": 2, "name": "green", "rd": "65000:2", "tags": [{"name": "zone-a", "slug": "zone-a"}]},
    {"id": 3, "name": "red", "rd": "65000:9", "tags": [{"name": "zone-b", "slug": "zone-b"}]},
    {"id": 4, "name": "untagged", "rd": "65000:3", "tags": []},
    {"id": 5, "name": "no-rd", "rd": None, "tags": [{"name": "zone-a", "slug": "zone-a"}]},
]


def _client_with_vrfs(vrfs):
    client = create_dcim_client("netbox", _SETTINGS)

    async def fake_rest_list(path, params=None):
        assert path == "ipam/vrfs/"
        return vrfs

    client.rest_list = fake_rest_list
    return client


async def test_list_namespace_tags_returns_distinct_sorted_names():
    client = _client_with_vrfs(_VRFS)
    try:
        tags = await client.list_namespace_tags()
    finally:
        await client.close()
    assert tags == ["zone-a", "zone-b"]


async def test_get_namespace_route_distinguishers_collects_zone_rds():
    client = _client_with_vrfs(_VRFS)
    try:
        result = await client.get_namespace_route_distinguishers("SiteX", "zone-a")
    finally:
        await client.close()
    # VRFs 1, 2 and 5 carry zone-a; VRF 5 has no RD (empty tuple).
    by_id = {r.namespace_id: r.route_distinguishers for r in result}
    assert by_id == {"1": ("65000:1",), "2": ("65000:2",), "5": ()}
    # The caller flattens these to find the in-use RDs for allocation.
    in_use = {rd for rds in by_id.values() for rd in rds}
    assert in_use == {"65000:1", "65000:2"}


async def test_get_namespace_route_distinguishers_matches_tag_slug():
    client = _client_with_vrfs(_VRFS)
    try:
        # Passing the slug resolves the same zone as the name.
        result = await client.get_namespace_route_distinguishers("SiteX", "zone-b")
    finally:
        await client.close()
    assert [r.namespace_id for r in result] == ["3"]
    assert result[0].route_distinguishers == ("65000:9",)


async def test_get_namespace_route_distinguishers_unknown_tag_is_empty():
    client = _client_with_vrfs(_VRFS)
    try:
        result = await client.get_namespace_route_distinguishers("SiteX", "no-such-zone")
    finally:
        await client.close()
    assert result == []
