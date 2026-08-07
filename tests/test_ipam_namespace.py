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


def _tag(name, slug=None):
    return {"name": name, "slug": slug or name}


async def test_list_namespace_tags_returns_distinct_sorted_names():
    client = _client_with_vrfs(_VRFS)
    try:
        tags = await client.list_namespace_tags()
    finally:
        await client.close()
    assert tags == ["zone-a", "zone-b"]


async def test_get_namespace_route_distinguishers_collects_zone_rds():
    # A namespace zone is marked by the query tag ('spectrumx'); each zone VRF
    # also carries a namespace-name tag. namespace_id returns that name (NOT the
    # VRF pk), so provision re-tags overlay VRFs with the namespace name.
    vrfs = [
        {"id": 1, "name": "blue", "rd": "65000:1", "tags": [_tag("spectrumx"), _tag("ns-blue")]},
        {"id": 2, "name": "green", "rd": "65000:2", "tags": [_tag("spectrumx"), _tag("ns-green")]},
        {"id": 5, "name": "no-rd", "rd": None, "tags": [_tag("spectrumx"), _tag("ns-nord")]},
        {"id": 4, "name": "untagged", "rd": "65000:3", "tags": []},
    ]
    client = _client_with_vrfs(vrfs)
    try:
        result = await client.get_namespace_route_distinguishers("SiteX", "spectrumx")
    finally:
        await client.close()
    by_ns = {r.namespace_id: r.route_distinguishers for r in result}
    assert by_ns == {"ns-blue": ("65000:1",), "ns-green": ("65000:2",), "ns-nord": ()}
    # The caller flattens these to find the in-use RDs for allocation.
    in_use = {rd for rds in by_ns.values() for rd in rds}
    assert in_use == {"65000:1", "65000:2"}


async def test_get_namespace_route_distinguishers_matches_zone_marker_by_slug():
    # The namespace_tag ('spectrumx') matches the zone marker by tag name OR slug;
    # the returned namespace_id is the *other* tag (the namespace name).
    marker = _tag("SpectrumX", "spectrumx")  # zone marker: name != slug
    vrfs = [{"id": 3, "name": "red", "rd": "65000:9", "tags": [marker, _tag("ns-red")]}]
    client = _client_with_vrfs(vrfs)
    try:
        result = await client.get_namespace_route_distinguishers("SiteX", "spectrumx")
    finally:
        await client.close()
    assert [r.namespace_id for r in result] == ["ns-red"]
    assert result[0].route_distinguishers == ("65000:9",)


async def test_get_namespace_route_distinguishers_unknown_tag_is_empty():
    client = _client_with_vrfs(_VRFS)
    try:
        result = await client.get_namespace_route_distinguishers("SiteX", "no-such-zone")
    finally:
        await client.close()
    assert result == []
