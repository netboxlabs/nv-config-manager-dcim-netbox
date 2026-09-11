# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Behavioral tests for the Spectrum-X overlay/VRF methods (stubbed transport).

``_find_overlay`` (which resolves a site name to a Location id) and the stale-
assignment sweep are stubbed so each test isolates the target method's logic.
"""

from __future__ import annotations

import pytest
from nv_config_manager_dcim import DCIMConflictError, DCIMInvalidDataError

from tests._fake import fake_client

# Area marker for `make <area>` / `pytest -m <area>`.
pytestmark = pytest.mark.spectrumx

_VXLANS = "plugins/nv-config-manager/vxlans/"
_OVERLAYS = "plugins/nv-config-manager/overlays/"
_ASSIGN = "plugins/nv-config-manager/overlay-assignments/"


def _client(gql=None, rest_list=None, record=None, overlay="unset"):
    patch = {}
    if overlay != "unset":

        async def fake_find_overlay(name, location=None, *, scope_type=None):
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


async def test_get_spectrum_x_vrfs_namespace_skips_the_zone_marker():
    # provision_spectrum_x_vrf stamps `spectrumx` on every namespace VRF, and it
    # sorts before namespaces like `tenant-a`.
    gql = {
        "vrf_list": [
            {
                "id": "8",
                "name": "SpXTenant",
                "rd": "*:60000",
                "tags": [
                    {"name": "spectrumx", "slug": "spectrumx"},
                    {"name": "tenant-a", "slug": "tenant-a"},
                ],
                "interfaces": [],
            }
        ]
    }
    client = _client(gql=gql, rest_list={_VXLANS: [{"vrf": 8}]}, overlay={"id": 10})
    vrfs = await client.get_spectrum_x_vrfs("nvcm-spx", "DM-Akron")
    assert vrfs[0].namespace == "tenant-a"


async def test_get_spectrum_x_vrfs_namespace_prefers_the_tag_name_over_its_slug():
    gql = {
        "vrf_list": [
            {
                "id": "8",
                "name": "SpXTenant",
                "rd": "*:60000",
                "tags": [
                    {"name": "spectrumx", "slug": "spectrumx"},
                    {"name": "NS Blue", "slug": "ns-blue"},
                ],
                "interfaces": [],
            }
        ]
    }
    client = _client(gql=gql, rest_list={_VXLANS: [{"vrf": 8}]}, overlay={"id": 10})
    vrfs = await client.get_spectrum_x_vrfs("nvcm-spx", "DM-Akron")
    assert vrfs[0].namespace == "NS Blue"


async def test_provision_spectrum_x_vrf_creates_overlay_vrf_assignment_vxlan():
    """Happy path (overlay does not exist yet): create the overlay, then the VRF,
    assignment, and VXLAN, asserting each write's path + body."""
    record = []

    async def no_overlay(name, scope=None, *, scope_type=None):
        return None

    async def tenant_id(name):
        return 7

    async def resolve_scope(value, *, scope_type):
        assert scope_type == "dcim.site", scope_type
        return {"id": 3, "name": "SPO01", "slug": "spo01"}

    client = fake_client(
        record=record,
        patch={
            "_find_overlay": no_overlay,
            "_resolve_tenant_id": tenant_id,
            "_resolve_scope": resolve_scope,
        },
    )
    await client.provision_spectrum_x_vrf(
        namespaces=["ns1"],
        route_distinguisher="65001:1500",
        vnid=1500,
        overlay_name="SpX",
        site="rack-1",
        tenant="tenant-a",
    )
    posts = {path: body for method, path, body in record if method == "POST"}
    assert posts[_OVERLAYS]["name"] == "SpX"
    assert posts[_OVERLAYS]["scope_type"] == "dcim.site" and posts[_OVERLAYS]["scope_id"] == 3
    assert posts[_OVERLAYS]["tenant"] == 7
    # nvcm_site is the *resolved* Site pk, not the value NVCM passed in: that is
    # what the plugin's loader seeds on namespace zones, and what the RD allocator
    # filters on. Here `site="rack-1"` resolves to the stub Site with pk 3, the
    # same pk the overlay is scoped on above.
    assert posts["ipam/vrfs/"] == {
        "name": "SpXTenant1500",
        "rd": "65001:1500",
        "tenant": 7,
        "custom_fields": {"nvcm_site": 3},
    }
    assert posts[_ASSIGN]["assigned_object_type"] == "ipam.vrf"
    assert posts[_VXLANS]["vnid"] == 1500 and posts[_VXLANS]["name"] == "SpXTenant1500"


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

    created, removed = await client.reconcile_spectrum_x_overlay_assignments(
        "nvcm-spx", "DM-Akron", "1", ["11", "12"], ["11", "12", "13"]
    )
    assert (created, removed) == (3, 0)  # 1 device + 2 interfaces
    posts = [body for method, path, body in record if method == "POST" and path == _ASSIGN]
    assert len(posts) == 3


async def test_reconcile_with_no_overlay_removes_and_creates_nothing():
    """`overlay_name=None` is the remove case -- the form's "leave blank to remove"."""
    record = []
    client = _client(rest_list={_ASSIGN: []}, record=record)

    created, removed = await client.reconcile_spectrum_x_overlay_assignments(
        None, "DM-Akron", "1", ["11"], ["11"]
    )

    assert created == 0
    assert not [m for m, path, _ in record if m == "POST" and path == _ASSIGN]


async def test_provision_rollback_does_not_mask_the_original_failure():
    """If cleanup itself fails, the caller must still see the exception that caused
    the rollback -- not the cleanup error, which hides the real cause."""
    record = []

    async def no_overlay(name, scope=None, *, scope_type=None):
        return None

    async def tenant_id(name):
        return 7

    async def resolve_scope(value, *, scope_type):
        assert scope_type == "dcim.site", scope_type
        return {"id": 3, "name": "SPO01", "slug": "spo01"}

    client = fake_client(
        record=record,
        patch={
            "_find_overlay": no_overlay,
            "_resolve_tenant_id": tenant_id,
            "_resolve_scope": resolve_scope,
        },
    )
    original = DCIMInvalidDataError("VXLAN vnid already in use")

    async def failing_write(method, path, **kwargs):
        record.append((method, path, kwargs.get("json")))
        if method == "POST" and path == _VXLANS:
            raise original
        if method == "DELETE":
            raise DCIMConflictError("cannot delete: still referenced")
        return {"id": 99}

    client.rest_request = failing_write
    with pytest.raises(DCIMInvalidDataError) as caught:
        await client.provision_spectrum_x_vrf(
            namespaces=["ns1"],
            route_distinguisher="65001:1500",
            vnid=1500,
            overlay_name="SpX",
            site="rack-1",
            tenant="tenant-a",
        )
    assert caught.value is original
    # Rollback was still attempted for everything created, overlay last.
    deleted = [path for method, path, _ in record if method == "DELETE"]
    assert any(p.startswith(_ASSIGN) for p in deleted)
    assert any(p.startswith("ipam/vrfs/") for p in deleted)
    assert deleted[-1].startswith(_OVERLAYS)


async def test_delete_spectrum_x_vrf_matches_a_string_vrf_fk():
    """The VXLAN's VRF FK may serialize as a string or a nested object; an int
    comparison silently matched neither and leaked the VXLAN."""
    for vrf_field in ("8", {"id": 8}, 8):
        record = []
        client = _client(rest_list={_VXLANS: [{"id": 5, "vrf": vrf_field}]}, record=record)
        await client.delete_spectrum_x_vrf("8", 60000)
        assert (f"{_VXLANS}5/") in [path for method, path, _ in record if method == "DELETE"], (
            f"VXLAN not deleted for vrf={vrf_field!r}"
        )


async def test_find_overlay_scopes_by_type_as_well_as_id():
    # Overlay scope is a generic FK and Site/Location pks are independent, so
    # scope_id=5 alone matched a Location-scoped overlay for a caller wanting Site 5.
    seen: list[list[tuple[str, str]]] = []
    client = fake_client()

    async def rest_list(path, params=None, **kwargs):
        if path.endswith("overlays/"):
            seen.append(list(params or []))
        return []

    async def resolve_scope(value, *, scope_type):
        return {"id": 5, "name": "Site A", "slug": "site-a"}

    client.rest_list = rest_list
    client._resolve_scope = resolve_scope
    assert await client._find_overlay("nvcm-spx", "site-a", scope_type="dcim.site") is None
    assert ("scope_id", "5") in seen[0]
    assert ("scope_type", "dcim.site") in seen[0]


async def test_stale_assignment_cleanup_compares_overlay_ids_as_strings():
    # `"7" == 7` is False, so the self-exclusion guard deleted fresh assignments.
    own = {
        "id": 60,
        "overlay": "7",
        "assigned_object_type": "dcim.interface",
        "assigned_object_id": 11,
    }
    record: list[tuple[str, str, object]] = []
    client = fake_client(rest_list={_ASSIGN: [own]}, record=record)
    removed = await client._remove_stale_spx_interface_assignments(7, ["11"])
    assert removed == 0
    assert not [p for m, p, _ in record if m == "DELETE"]


async def test_typed_site_survives_the_vrf_site_fallback():
    """``SpectrumXVRF.site`` is a plain string in the SDK contract.

    NVCM sends a typed reference (spx_overlay builds one from site + site_type),
    and a VRF with no attached interfaces -- a freshly provisioned overlay, before
    the assignment workflow runs -- falls back to it, so it must be unwrapped.
    """
    from nv_config_manager_dcim import DCIMLocationReference

    async def _overlay(name, scope=None, *, scope_type):
        return {"id": 10, "name": name}

    client = fake_client(
        rest_list={
            "plugins/nv-config-manager/vxlans/": [{"id": 1, "vrf": 1, "overlay": 10}],
        },
        gql={
            "vrf_list": [
                {
                    "id": 1,
                    "name": "SpXTenant5000",
                    "rd": "*:5000",
                    "tags": [{"name": "spectrumx"}, {"name": "ns-a"}],
                    "interfaces": [],
                }
            ]
        },
        patch={"_find_overlay": _overlay},
    )

    vrfs = await client.get_spectrum_x_vrfs(
        "spx-overlay", DCIMLocationReference(id="1", location_type="Site")
    )

    assert [(v.name, v.site) for v in vrfs] == [("SpXTenant5000", "1")]
