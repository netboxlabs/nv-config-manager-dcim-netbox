# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Spectrum-X overlay operations (backed by the NetBox fabric plugin).

NetBox has no first-class IPAM namespace, so — consistent with the rest of this
provider (see ``ipam.py`` / ``reference.py``) — a namespace is emulated as a
**tag** on the VRF. ``provision_spectrum_x_vrf`` fans out one VRF (tagged with
the namespace), overlay-assignment, and VXLAN per namespace, and
``get_spectrum_x_vrfs`` reads the namespace back from the VRF's tag while
deriving the site from the VRF's attached-interface devices.

NOTE: NetBox enforces global uniqueness on ``ipam.VRF.rd`` and on the fabric
plugin's ``VXLAN.vnid`` (the upstream model scoped both by namespace). Multiple
namespaces sharing a single ``route_distinguisher`` / ``vnid`` therefore cannot
coexist in NetBox; distinct namespaces require distinct values.
"""

from __future__ import annotations

import asyncio
import logging

from nv_config_manager_dcim import DCIMConflictError, DCIMInvalidDataError, SpectrumXVRF

from nv_config_manager_dcim_netbox._mappers import tag_names
from nv_config_manager_dcim_netbox.fabric_common import FABRIC_STATUS, VRFS_PATH

logger = logging.getLogger(__name__)

_SPECTRUMX_VRFS_QUERY = """
query ($ids: [ID!]!) {
  vrf_list(filters: {id: {in_list: $ids}}) {
    id
    name
    rd
    tags { name slug }
    interfaces { name device { name site { name } } }
  }
}
"""

_SPECTRUMX_ISOLATION = "spectrum_x_vrf"
_L3_VNI = "l3"


class NetBoxSpectrumXMixin:
    """Spectrum-X VRF/overlay provisioning and reconciliation."""

    async def get_spectrum_x_vrfs(
        self, overlay_name: str, site: str, namespace: str | None = None
    ) -> list[SpectrumXVRF]:
        overlay = await self._find_overlay(overlay_name, site)
        if not overlay:
            return []
        vxlans = await self.rest_list(self.VXLANS_PATH, [("overlay_id", str(overlay["id"]))])
        vrf_ids = sorted({str(self._fk_id(v["vrf"])) for v in vxlans if v.get("vrf") is not None})
        if not vrf_ids:
            return []
        data = await self.graphql_query(_SPECTRUMX_VRFS_QUERY, {"ids": vrf_ids})
        out: list[SpectrumXVRF] = []
        for vrf in data.get("vrf_list") or []:
            # Namespace is emulated as a VRF tag; derive it from the data (and
            # apply the namespace filter against it) rather than echoing args.
            vrf_tags = tag_names(vrf)
            if namespace and namespace not in vrf_tags:
                continue
            vrf_namespace = namespace or next(iter(sorted(vrf_tags)), "")
            interface_rows = vrf.get("interfaces") or []
            interfaces = tuple(
                f"{(i.get('device') or {}).get('name')}:{i['name']}"
                for i in interface_rows
                if i.get("device")
            )
            # NetBox VRFs carry no location, so derive the site from an attached
            # interface's device, falling back to the overlay's site.
            vrf_site = next(
                (
                    ((i.get("device") or {}).get("site") or {}).get("name")
                    for i in interface_rows
                    if ((i.get("device") or {}).get("site") or {}).get("name")
                ),
                site,
            )
            out.append(
                SpectrumXVRF(
                    vrf_id=str(vrf["id"]),
                    name=vrf["name"],
                    namespace=vrf_namespace,
                    site=vrf_site or site,
                    route_distinguisher=vrf.get("rd") or "",
                    interfaces=interfaces,
                )
            )
        return out

    async def provision_spectrum_x_vrf(
        self,
        namespaces: list[str],
        route_distinguisher: str,
        vnid: int,
        overlay_name: str,
        site: str,
        tenant: str,
    ) -> None:
        tenant_id = await self._resolve_tenant_id(tenant)
        vrf_name = f"SpXTenant{vnid}"
        created_overlay_id: int | None = None
        # Track every object created across all namespaces for reverse-order rollback.
        created: list[tuple[str, int]] = []

        overlay = await self._find_overlay(overlay_name, site)
        if overlay:
            # Guard against cross-tenant contamination: reusing an existing
            # overlay is only safe when it already belongs to this tenant.
            existing_tenant = self._fk_id(overlay.get("tenant"))
            if str(existing_tenant or "") != str(tenant_id or ""):
                raise DCIMConflictError(
                    f"Spectrum-X overlay {overlay_name!r} already belongs to tenant "
                    f"{existing_tenant!r}, expected {tenant_id!r}"
                )
            overlay_id = overlay["id"]
        else:
            location_id = await self._resolve_location_id(site)
            if location_id is None:
                raise DCIMInvalidDataError(
                    f"NetBox has no Location named {site!r} for the Spectrum-X overlay"
                )
            row = await self.rest_request(
                "POST",
                self.OVERLAYS_PATH,
                json={
                    "name": overlay_name,
                    "location": location_id,
                    "tenant": tenant_id,
                    "isolation_type": _SPECTRUMX_ISOLATION,
                    "status": FABRIC_STATUS,
                },
            )
            overlay_id = created_overlay_id = row["id"]

        try:
            # Fan out one VRF + assignment + VXLAN per namespace (mirrors the
            # reference provider). See the module NOTE: shared rd/vnid values can
            # back only one namespace under NetBox's global uniqueness rules.
            for namespace in namespaces:
                vrf = await self.rest_request(
                    "POST",
                    VRFS_PATH,
                    json={"name": vrf_name, "rd": route_distinguisher, "tenant": tenant_id},
                )
                created.append((VRFS_PATH, vrf["id"]))
                if namespace:
                    # Emulate the namespace as a VRF tag (NetBox has no IPAM
                    # namespace). Applied as a follow-up PATCH so the create
                    # payload stays minimal. NOTE: the tag is expected to pre-exist.
                    await self.rest_request(
                        "PATCH",
                        f"{VRFS_PATH}{vrf['id']}/",
                        json={"tags": [{"name": namespace}]},
                    )
                assignment = await self._create_overlay_assignment(
                    overlay_id, "ipam.vrf", vrf["id"]
                )
                created.append((self.ASSIGNMENTS_PATH, assignment["id"]))
                vxlan = await self.rest_request(
                    "POST",
                    self.VXLANS_PATH,
                    json={
                        "vnid": vnid,
                        "name": vrf_name,
                        "vni_type": _L3_VNI,
                        "vrf": vrf["id"],
                        "overlay": overlay_id,
                        "status": FABRIC_STATUS,
                    },
                )
                created.append((self.VXLANS_PATH, vxlan["id"]))
        except Exception:
            # Roll back in reverse creation order, overlay last (only if created).
            # A rollback failure must never replace the exception that caused it —
            # the operator needs the original cause, plus a warning that cleanup
            # left objects behind.
            rollback = [*reversed(created)]
            if created_overlay_id is not None:
                rollback.append((self.OVERLAYS_PATH, created_overlay_id))
            for path, obj_id in rollback:
                if obj_id is None:
                    continue
                try:
                    await self._safe_delete(f"{path}{obj_id}/")
                except Exception:  # noqa: BLE001 - never mask the original failure
                    logger.exception(
                        "Spectrum-X rollback could not delete %s%s; it may need removing by hand",
                        path,
                        obj_id,
                    )
            raise

    async def delete_spectrum_x_vrf(self, vrf_id: str, vnid: int) -> None:
        vxlans = await self.rest_list(self.VXLANS_PATH, [("vnid", str(vnid))])
        for vxlan in vxlans:
            # str-compare: the VRF FK may arrive nested or bare, int or str. An
            # int() comparison silently matched nothing for the string shapes,
            # leaking the VXLAN.
            if str(self._fk_id(vxlan.get("vrf"))) == str(vrf_id):
                await self._safe_delete(f"{self.VXLANS_PATH}{vxlan['id']}/")
        assignments = await self.rest_list(
            self.ASSIGNMENTS_PATH, [("assigned_object_id", str(vrf_id))]
        )
        for assignment in assignments:
            if str(assignment.get("assigned_object_type")) == "ipam.vrf":
                await self._safe_delete(f"{self.ASSIGNMENTS_PATH}{assignment['id']}/")
        await self._safe_delete(f"{VRFS_PATH}{vrf_id}/")

    async def delete_spectrum_x_overlay_if_unused(self, overlay_name: str, site: str) -> bool:
        overlay = await self._find_overlay(overlay_name, site)
        if not overlay:
            return False
        vxlans = await self.rest_list(self.VXLANS_PATH, [("overlay_id", str(overlay["id"]))])
        if vxlans:
            return False
        await self._safe_delete(f"{self.OVERLAYS_PATH}{overlay['id']}/")
        return True

    async def reconcile_spectrum_x_overlay_assignments(
        self, overlay_name: str, site: str, device_id: str, interface_ids: list[str]
    ) -> tuple[int, int]:
        overlay = await self._find_overlay(overlay_name, site)
        if not overlay:
            raise DCIMInvalidDataError(f"Spectrum-X overlay {overlay_name!r} not found")
        if overlay.get("isolation_type") != _SPECTRUMX_ISOLATION:
            raise DCIMInvalidDataError(f"Overlay {overlay_name!r} is not a Spectrum-X overlay")
        overlay_id = overlay["id"]
        existing = await self.rest_list(self.ASSIGNMENTS_PATH, [("overlay_id", str(overlay_id))])
        present = {
            (str(a.get("assigned_object_type")), str(a["assigned_object_id"])) for a in existing
        }
        missing: list[tuple[str, str]] = []
        if ("dcim.device", str(device_id)) not in present:
            missing.append(("dcim.device", device_id))
        missing.extend(
            ("dcim.interface", interface_id)
            for interface_id in interface_ids
            if ("dcim.interface", str(interface_id)) not in present
        )
        await asyncio.gather(
            *(
                self._create_overlay_assignment(overlay_id, object_type, object_id)
                for object_type, object_id in missing
            )
        )
        removed = await self._remove_stale_spx_interface_assignments(overlay_id, interface_ids)
        return len(missing), removed

    # -- helpers -------------------------------------------------------------

    async def _remove_stale_spx_interface_assignments(self, overlay_id, interface_ids) -> int:
        removed = 0
        isolation_cache: dict[int, str | None] = {}
        # Fetch each interface's assignments concurrently rather than serially.
        rows_per_interface = await asyncio.gather(
            *(
                self.rest_list(self.ASSIGNMENTS_PATH, [("assigned_object_id", str(interface_id))])
                for interface_id in interface_ids
            )
        )
        for rows in rows_per_interface:
            for row in rows:
                other_overlay_id = self._fk_id(row.get("overlay"))
                if (
                    str(row.get("assigned_object_type")) != "dcim.interface"
                    or other_overlay_id == overlay_id
                ):
                    continue
                if (
                    await self._overlay_isolation(other_overlay_id, isolation_cache)
                    == _SPECTRUMX_ISOLATION
                ):
                    await self._safe_delete(f"{self.ASSIGNMENTS_PATH}{row['id']}/")
                    removed += 1
        return removed

    async def _overlay_isolation(self, overlay_id, cache: dict) -> str | None:
        if overlay_id not in cache:
            overlay = await self.rest_request("GET", f"{self.OVERLAYS_PATH}{overlay_id}/")
            cache[overlay_id] = (overlay or {}).get("isolation_type")
        return cache[overlay_id]
