# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Parity tests for the Spectrum-X and firmware provider fixes.

Each test pins a behavior adapted from the reference DCIM provider to NetBox
idioms (namespace-as-VRF-tag, first-class Site, site-scoped config-context, and
raising on missing firmware data). Transport and resolution helpers are stubbed
so each test isolates the fixed behavior.
"""

from __future__ import annotations

import pytest
from nv_config_manager_dcim import (
    DCIMConflictError,
    DCIMInvalidDataError,
    DCIMNotFoundError,
)

from tests._fake import fake_client

# Area marker for `make <area>` / `pytest -m <area>`.
pytestmark = [pytest.mark.spectrumx, pytest.mark.firmware]

_VXLANS = "plugins/nv-config-manager/vxlans/"
_OVERLAYS = "plugins/nv-config-manager/overlays/"
_ASSIGN = "plugins/nv-config-manager/overlay-assignments/"
_VRFS = "ipam/vrfs/"
_DEVICE = "dcim/devices/1/"


def _spx_client(*, gql=None, rest_list=None, record=None, overlay="unset", tenant=7, location=3):
    patch = {}
    if overlay != "unset":

        async def fake_find_overlay(name, scope=None):
            return overlay

        patch["_find_overlay"] = fake_find_overlay

    async def fake_tenant(name):
        return tenant

    async def fake_resolve_scope(value):
        # ``location`` here is the resolved scope PK; anchor as a Site scope.
        return "dcim.site", location

    patch["_resolve_tenant_id"] = fake_tenant
    patch["_resolve_scope"] = fake_resolve_scope
    return fake_client(gql=gql, rest_list=rest_list, record=record, patch=patch)


# -- Fix 1: SpectrumX overlay tenant-ownership guard ---------------------------


async def test_provision_rejects_overlay_owned_by_other_tenant():
    client = _spx_client(overlay={"id": 10, "tenant": {"id": 99}}, tenant=7)
    with pytest.raises(DCIMConflictError):
        await client.provision_spectrum_x_vrf(
            namespaces=["ns1"],
            route_distinguisher="65001:1",
            vnid=1500,
            overlay_name="SpX",
            site="rack-1",
            tenant="tenant-a",
        )


async def test_provision_reuses_overlay_of_same_tenant():
    record = []
    client = _spx_client(overlay={"id": 10, "tenant": {"id": 7}}, tenant=7, record=record)
    await client.provision_spectrum_x_vrf(
        namespaces=["ns1"],
        route_distinguisher="65001:1",
        vnid=1500,
        overlay_name="SpX",
        site="rack-1",
        tenant="tenant-a",
    )
    posts = [path for method, path, _ in record if method == "POST"]
    # Existing overlay reused (no overlay POST); VRF/assignment/VXLAN created.
    assert _OVERLAYS not in posts
    assert _VRFS in posts and _ASSIGN in posts and _VXLANS in posts


# -- Fix 3: multi-namespace fan-out --------------------------------------------


async def test_provision_fans_out_per_namespace_and_tags_vrf():
    record = []
    client = _spx_client(overlay=None, tenant=7, record=record)
    await client.provision_spectrum_x_vrf(
        namespaces=["ns1", "ns2"],
        route_distinguisher="65001:2",
        vnid=1600,
        overlay_name="SpX",
        site="rack-1",
        tenant="tenant-a",
    )
    vrf_posts = [b for m, p, b in record if m == "POST" and p == _VRFS]
    vxlan_posts = [b for m, p, b in record if m == "POST" and p == _VXLANS]
    assign_posts = [b for m, p, b in record if m == "POST" and p == _ASSIGN]
    # One VRF + assignment + VXLAN per namespace.
    assert len(vrf_posts) == len(vxlan_posts) == len(assign_posts) == 2
    # Namespace is applied as a VRF tag via a follow-up PATCH (not in the POST body),
    # alongside the `spectrumx` zone tag the RD allocator selects on.
    assert "tags" not in vrf_posts[0]
    tag_patches = [b for m, p, b in record if m == "PATCH" and p.startswith(_VRFS)]
    assert {t["name"] for b in tag_patches for t in b["tags"]} == {"ns1", "ns2", "spectrumx"}
    assert all({t["name"] for t in b["tags"]} >= {"spectrumx"} for b in tag_patches)
    # And scoped to the site, the other half of that read's filter.
    assert all(b["custom_fields"] == {"nvcm_site": "rack-1"} for b in vrf_posts)


async def test_provisioned_vrf_is_visible_to_the_rd_allocator():
    """The VRF provision writes must satisfy get_namespace_route_distinguishers.

    That read filters on cf_nvcm_site and requires the zone tag, so a VRF missing
    either is invisible and its RD gets reallocated.
    """
    record = []
    client = _spx_client(overlay=None, tenant=7, record=record)
    await client.provision_spectrum_x_vrf(
        namespaces=["ns1"],
        route_distinguisher="65001:3",
        vnid=1700,
        overlay_name="SpX",
        site="dm-akron",
        tenant="tenant-a",
    )
    vrf_post = next(b for m, p, b in record if m == "POST" and p == _VRFS)
    vrf_tags = next(b for m, p, b in record if m == "PATCH" and p.startswith(_VRFS))["tags"]

    # Replay the written VRF through the reader that allocates the next RD.
    written = {
        "id": 99,
        "name": vrf_post["name"],
        "rd": vrf_post["rd"],
        "tags": [{"name": t["name"], "slug": t["name"]} for t in vrf_tags],
        "custom_fields": vrf_post["custom_fields"],
    }
    seen_params: list[tuple[str, str]] = []

    async def rest_list(path, params=None, **kwargs):
        seen_params.extend(params or [])
        site = dict(params or []).get("cf_nvcm_site")
        return [written] if site == written["custom_fields"]["nvcm_site"] else []

    client.rest_list = rest_list
    result = await client.get_namespace_route_distinguishers("dm-akron", "spectrumx")
    assert ("cf_nvcm_site", "dm-akron") in seen_params
    assert {r.namespace_id: r.route_distinguishers for r in result} == {"ns1": ("65001:3",)}


async def test_provision_rolls_back_created_objects_on_failure():
    record = []

    async def failing_rest_request(method, path, **kwargs):
        record.append((method, path, kwargs.get("json")))
        if method == "POST" and path == _VXLANS:
            raise DCIMInvalidDataError("vnid already exists")
        if method == "GET":
            return None
        return {"id": 42}

    client = _spx_client(overlay=None, tenant=7)
    client.rest_request = failing_rest_request
    with pytest.raises(DCIMInvalidDataError):
        await client.provision_spectrum_x_vrf(
            namespaces=["ns1"],
            route_distinguisher="65001:3",
            vnid=1700,
            overlay_name="SpX",
            site="rack-1",
            tenant="tenant-a",
        )
    # The VRF and assignment created before the VXLAN failure are deleted, and
    # so is the overlay we created for this provisioning run.
    deletes = {path for method, path, _ in record if method == "DELETE"}
    assert f"{_VRFS}42/" in deletes
    assert f"{_ASSIGN}42/" in deletes
    assert f"{_OVERLAYS}42/" in deletes


# -- Fix 2: namespace/site derivation ------------------------------------------


async def test_get_spectrum_x_vrfs_derives_namespace_and_site_from_data():
    gql = {
        "vrf_list": [
            {
                "id": "8",
                "name": "SpXTenant1500",
                "rd": "65001:1",
                "tags": [{"name": "ns1", "slug": "ns1"}],
                "interfaces": [
                    {"name": "swp1", "device": {"name": "leaf-01", "site": {"name": "DM-Real"}}}
                ],
            }
        ]
    }
    client = _spx_client(gql=gql, rest_list={_VXLANS: [{"vrf": 8}]}, overlay={"id": 10})
    vrfs = await client.get_spectrum_x_vrfs("SpX", "DM-Requested")
    assert len(vrfs) == 1
    # Namespace derived from the VRF tag, not echoed; site derived from the
    # attached interface's device, not from the caller's argument.
    assert vrfs[0].namespace == "ns1"
    assert vrfs[0].site == "DM-Real"
    assert vrfs[0].interfaces == ("leaf-01:swp1",)


async def test_get_spectrum_x_vrfs_filters_by_namespace_tag():
    gql = {
        "vrf_list": [
            {"id": "8", "name": "a", "rd": "1:1", "tags": [{"name": "ns1"}], "interfaces": []},
            {"id": "9", "name": "b", "rd": "1:2", "tags": [{"name": "ns2"}], "interfaces": []},
        ]
    }
    client = _spx_client(gql=gql, rest_list={_VXLANS: [{"vrf": 8}, {"vrf": 9}]}, overlay={"id": 10})
    vrfs = await client.get_spectrum_x_vrfs("SpX", "DM-Akron", namespace="ns2")
    assert [v.vrf_id for v in vrfs] == ["9"]
    # Site falls back to the overlay's site when no interface device is present.
    assert vrfs[0].site == "DM-Akron"


# -- Fix 4: firmware desired-target resolution (site-scoped) --------------------


async def test_get_os_image_versions_scopes_targets_to_device_site():
    device = {
        "id": 1,
        "role": {"slug": "leaf"},
        "platform": {"slug": "cumulus-linux"},
        "site": {"name": "DM-Akron"},
        "config_context": {
            "intended-firmware": {"version": "5.1.0"},
            "ztp": {"ipv4": ["10.0.0.9"]},
        },
    }
    seen = {}

    async def targets(name, site=None):
        seen["site"] = site
        return [{"data": {"firmware-targets": {"leaf": {"cumulus-linux": "5.2.0"}}}}]

    client = fake_client(rest_get={_DEVICE: device}, patch={"config_contexts_for_profile": targets})
    assert await client.get_os_image_versions("1") == ("5.1.0", "5.2.0", "10.0.0.9")
    # The config-context lookup is scoped to the device's site.
    assert seen["site"] == "DM-Akron"


def test_context_scoped_to_site_matches_and_defaults_global():
    from nv_config_manager_dcim_netbox.client import _context_scoped_to_site

    unscoped = {}
    assert _context_scoped_to_site(unscoped, "DM-Akron") is True  # global applies everywhere
    matching = {"locations": [{"name": "DM-Akron", "slug": "dm-akron"}]}
    assert _context_scoped_to_site(matching, "DM-Akron") is True
    assert _context_scoped_to_site(matching, "dm-akron") is True  # slug match
    other = {"sites": [{"name": "Elsewhere", "slug": "elsewhere"}]}
    assert _context_scoped_to_site(other, "DM-Akron") is False


# -- Fix 5: firmware bundle missing-data raises --------------------------------


async def test_get_firmware_bundle_raises_when_no_bundle_assigned():
    client = fake_client(rest_get={_DEVICE: {"config_context": {}}})
    with pytest.raises(DCIMNotFoundError):
        await client.get_firmware_bundle("1")


async def test_get_firmware_bundle_raises_when_version_missing():
    device = {"config_context": {"firmware_bundle_version": "b9", "firmware_bundles": {"b1": {}}}}
    client = fake_client(rest_get={_DEVICE: device})
    with pytest.raises(DCIMNotFoundError):
        await client.get_firmware_bundle("1")


async def test_get_firmware_bundle_raises_on_invalid_components():
    device = {
        "config_context": {
            "firmware_bundle_version": "b1",
            "firmware_bundles": {"b1": {"firmware": "not-a-dict"}},
        }
    }
    client = fake_client(rest_get={_DEVICE: device})
    with pytest.raises(DCIMInvalidDataError):
        await client.get_firmware_bundle("1")
