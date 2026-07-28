# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Spectrum-X overlay operations (backed by the NetBox fabric plugin).

NetBox has no IPAM namespace, so a Spectrum-X tenant is one VRF (the upstream
model created one VRF per namespace); the ``namespace`` argument is retained on
the SDK models but not used to scope objects here.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from nv_config_manager_dcim import DCIMInvalidDataError, SpectrumXVRF

from nv_config_manager_dcim_netbox.fabric_common import FABRIC_STATUS

if TYPE_CHECKING:
    pass

_SPECTRUMX_VRFS_QUERY = """
query ($ids: [ID!]!) {
  vrf_list(filters: {id: {in_list: $ids}}) {
    id
    name
    rd
    interfaces { name device { name } }
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
            interfaces = tuple(
                f"{(i.get('device') or {}).get('name')}:{i['name']}"
                for i in vrf.get("interfaces") or []
                if i.get("device")
            )
            out.append(
                SpectrumXVRF(
                    vrf_id=str(vrf["id"]),
                    name=vrf["name"],
                    namespace=namespace or "",
                    site=site,
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
        created_vxlan_id: int | None = None
        created_assignment_id: int | None = None
        created_vrf_id: int | None = None
        created_overlay_id: int | None = None

        overlay = await self._find_overlay(overlay_name, site)
        if overlay:
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
            vrf = await self.rest_request(
                "POST",
                "ipam/vrfs/",
                json={"name": vrf_name, "rd": route_distinguisher, "tenant": tenant_id},
            )
            created_vrf_id = vrf["id"]
            assignment = await self.rest_request(
                "POST",
                self.ASSIGNMENTS_PATH,
                json={
                    "overlay": overlay_id,
                    "assigned_object_type": "ipam.vrf",
                    "assigned_object_id": created_vrf_id,
                    "status": FABRIC_STATUS,
                },
            )
            created_assignment_id = assignment["id"]
            vxlan = await self.rest_request(
                "POST",
                self.VXLANS_PATH,
                json={
                    "vnid": vnid,
                    "name": vrf_name,
                    "vni_type": _L3_VNI,
                    "vrf": created_vrf_id,
                    "overlay": overlay_id,
                    "status": FABRIC_STATUS,
                },
            )
            created_vxlan_id = vxlan["id"]
        except Exception:
            # Roll back in reverse creation order.
            for path, obj_id in (
                (self.VXLANS_PATH, created_vxlan_id),
                (self.ASSIGNMENTS_PATH, created_assignment_id),
                ("ipam/vrfs/", created_vrf_id),
                (self.OVERLAYS_PATH, created_overlay_id),
            ):
                if obj_id is not None:
                    await self._safe_delete(f"{path}{obj_id}/")
            raise

    async def delete_spectrum_x_vrf(self, vrf_id: str, vnid: int) -> None:
        vxlans = await self.rest_list(self.VXLANS_PATH, [("vnid", str(vnid))])
        for vxlan in vxlans:
            if self._fk_id(vxlan.get("vrf")) == int(vrf_id):
                await self._safe_delete(f"{self.VXLANS_PATH}{vxlan['id']}/")
        assignments = await self.rest_list(
            self.ASSIGNMENTS_PATH, [("assigned_object_id", str(vrf_id))]
        )
        for assignment in assignments:
            if str(assignment.get("assigned_object_type")) == "ipam.vrf":
                await self._safe_delete(f"{self.ASSIGNMENTS_PATH}{assignment['id']}/")
        await self._safe_delete(f"ipam/vrfs/{vrf_id}/")

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
        created = 0
        if ("dcim.device", str(device_id)) not in present:
            await self._create_assignment(overlay_id, "dcim.device", device_id)
            created += 1
        for interface_id in interface_ids:
            if ("dcim.interface", str(interface_id)) not in present:
                await self._create_assignment(overlay_id, "dcim.interface", interface_id)
                created += 1
        removed = await self._remove_stale_spx_interface_assignments(overlay_id, interface_ids)
        return created, removed

    # -- helpers -------------------------------------------------------------

    async def _create_assignment(self, overlay_id, object_type, object_id):
        await self.rest_request(
            "POST",
            self.ASSIGNMENTS_PATH,
            json={
                "overlay": overlay_id,
                "assigned_object_type": object_type,
                "assigned_object_id": int(object_id),
                "status": FABRIC_STATUS,
            },
        )

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
