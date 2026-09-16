# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Namespace-as-VRF mapping tests (transport stubbed).

NetBox has no first-class IPAM namespace: a namespace is a VRF, and a namespace
*zone* (a set of VRFs that must hold unique RDs) is emulated with a tag on those
VRFs. These verify the tag grouping and RD collection without a live NetBox.
"""

from __future__ import annotations

import pytest
from nv_config_manager_dcim import create_dcim_client

# Area marker for `make <area>` / `pytest -m <area>`.
pytestmark = pytest.mark.ipam
_SETTINGS = {"server": "https://netbox.example", "token": "t"}

_VRFS = [
    {"id": 1, "name": "blue", "rd": "65000:1", "tags": [{"name": "zone-a", "slug": "zone-a"}]},
    {"id": 2, "name": "green", "rd": "65000:2", "tags": [{"name": "zone-a", "slug": "zone-a"}]},
    {"id": 3, "name": "red", "rd": "65000:9", "tags": [{"name": "zone-b", "slug": "zone-b"}]},
    {"id": 4, "name": "untagged", "rd": "65000:3", "tags": []},
    {"id": 5, "name": "no-rd", "rd": None, "tags": [{"name": "zone-a", "slug": "zone-a"}]},
]


_VXLANS = "plugins/nv-config-manager/vxlans/"


def _client_with_vrfs(vrfs, sites=None, vxlans=None):
    """A client whose VRF reads return *vrfs* and whose VXLAN reads return *vxlans*.

    ``sites`` answers the Site lookup that normalizes a site selection to the pk
    ``cf_nvcm_site`` holds; left empty, the value is filtered on unchanged. A VRF
    declaring ``custom_fields.nvcm_site`` answers only the matching site query;
    one without answers every query, which is what most fixtures want.
    """
    client = create_dcim_client("netbox", _SETTINGS)
    seen: list[tuple[str, dict]] = []

    async def fake_rest_list(path, params=None):
        p = dict(params or [])
        seen.append((path, p))
        if path == "dcim/sites/":
            return list(sites or [])
        if path == _VXLANS:
            return list(vxlans or [])
        assert path == "ipam/vrfs/"
        if "cf_nvcm_site" not in p:
            return vrfs
        return [
            v
            for v in vrfs
            if "custom_fields" not in v
            or str(v["custom_fields"].get("nvcm_site")) == p["cf_nvcm_site"]
        ]

    client.rest_list = fake_rest_list
    client._seen = seen
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
    # VRF pk), so provision re-tags overlay VRFs with the namespace name. The
    # untagged VRF is not a namespace, but its RD is still taken: VRF.rd is
    # unique across NetBox, so every namespace reports it.
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
    taken = ("65000:1", "65000:2", "65000:3")
    assert by_ns == {"ns-blue": taken, "ns-green": taken, "ns-nord": taken}
    # The caller flattens these to find the in-use RDs for allocation.
    in_use = {rd for rds in by_ns.values() for rd in rds}
    assert in_use == set(taken)


async def test_get_namespace_route_distinguishers_groups_tenant_vrfs_under_their_namespace():
    # provision_spectrum_x_vrf stamps each tenant VRF with the zone marker and the
    # namespace tag, so it matches the same filter as the zone VRF. It must fold
    # into that namespace's entry, not appear as a namespace of its own: the
    # caller provisions one VRF per entry and NetBox rejects a repeated RD.
    vrfs = [
        {"id": 1, "name": "NS-A", "rd": None, "tags": [_tag("spectrumx"), _tag("NS-A")]},
        {"id": 2, "name": "SpXTenant100", "rd": "*:100", "tags": [_tag("spectrumx"), _tag("NS-A")]},
        {"id": 3, "name": "SpXTenant200", "rd": "*:200", "tags": [_tag("spectrumx"), _tag("NS-A")]},
        {"id": 4, "name": "NS-B", "rd": None, "tags": [_tag("spectrumx"), _tag("NS-B")]},
    ]
    client = _client_with_vrfs(vrfs)
    try:
        result = await client.get_namespace_route_distinguishers("SiteX", "spectrumx")
    finally:
        await client.close()
    assert [r.namespace_id for r in result] == ["NS-A", "NS-B"]
    # RDs are unique install-wide, so NS-B is handed NS-A's RDs as taken too.
    assert result[0].route_distinguishers == ("*:100", "*:200")
    assert result[1].route_distinguishers == ("*:100", "*:200")


async def test_get_namespace_route_distinguishers_dedupes_a_repeated_rd():
    # Duplicate zone tags on a hand-edited VRF should not double-count its RD.
    vrfs = [
        {"id": 1, "name": "a", "rd": "*:100", "tags": [_tag("spectrumx"), _tag("NS-A")]},
        {"id": 2, "name": "b", "rd": "*:100", "tags": [_tag("spectrumx"), _tag("NS-A")]},
    ]
    client = _client_with_vrfs(vrfs, vxlans=[{"id": 1, "vnid": 100}])
    try:
        result = await client.get_namespace_route_distinguishers("SiteX", "spectrumx")
    finally:
        await client.close()
    assert [(r.namespace_id, r.route_distinguishers) for r in result] == [("NS-A", ("*:100",))]


async def test_get_namespace_route_distinguishers_taken_set_spans_sites_tags_and_vnis():
    """The taken set is global even though the namespace list is per site.

    NetBox makes VRF.rd and VXLAN.vnid unique install-wide, and NVCM's allocator
    subtracts only what this returns from [rd_min, rd_max]. A VRF at another site,
    an untagged VRF, and a VXLAN's vnid must all be reported, or the allocator
    hands back a value NetBox rejects on every retry.
    """
    vrfs = [
        {
            "id": 1,
            "name": "NS-A",
            "rd": None,
            "tags": [_tag("spectrumx"), _tag("NS-A")],
            "custom_fields": {"nvcm_site": 3},
        },
        {
            "id": 2,
            "name": "NS-B",
            "rd": "*:60000",
            "tags": [_tag("spectrumx"), _tag("NS-B")],
            "custom_fields": {"nvcm_site": 4},
        },
        {"id": 3, "name": "mgmt", "rd": "65000:7", "tags": [], "custom_fields": {"nvcm_site": 3}},
    ]
    client = _client_with_vrfs(
        vrfs,
        sites=[{"id": 3, "name": "SPO01", "slug": "spo01"}],
        vxlans=[{"id": 9, "vnid": 60001}, {"id": 10, "vnid": None}],
    )
    try:
        result = await client.get_namespace_route_distinguishers("3", "spectrumx")
    finally:
        await client.close()
    # One entry per namespace at the requested site only.
    assert [r.namespace_id for r in result] == ["NS-A"]
    assert result[0].route_distinguishers == ("*:60000", "65000:7", "*:60001")
    # The site read is still filtered; the global read is not.
    vrf_queries = [p for path, p in client._seen if path == "ipam/vrfs/"]
    assert {"cf_nvcm_site": "3"} in vrf_queries and {} in vrf_queries


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
