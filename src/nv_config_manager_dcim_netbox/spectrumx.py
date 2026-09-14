# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Spectrum-X overlay operations (backed by the NetBox fabric plugin).

NetBox has no first-class IPAM namespace, so -- consistent with the rest of this
provider (see ``ipam.py`` / ``reference.py``) -- a namespace is emulated as a
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
from nv_config_manager_dcim_netbox.fabric_common import (
    FABRIC_STATUS,
    SITE_SCOPE_TYPE,
    VRFS_PATH,
)

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

# Zone marker on every namespace VRF, seeded by the plugin's nvcm_load_bootstrap and
# passed back by the workflows as ``namespace_tag``. A provisioned VRF needs it (and
# ``nvcm_site``) to be visible to the RD allocator.
_SPECTRUMX_ZONE_TAG = "spectrumx"
# VRF custom field holding the owning site slug; NetBox VRFs have no native site link.
_SITE_FIELD = "nvcm_site"


def _namespace_tag_name(vrf: dict) -> str:
    """Return the VRF's namespace tag name, ignoring the zone marker.

    Tag names only, not ``tag_names`` (which also yields slugs and the
    ``spectrumx`` marker). Matches ``get_namespace_route_distinguishers``.
    """
    candidates = sorted(
        t["name"]
        for t in (vrf.get("tags") or [])
        if isinstance(t, dict)
        and t.get("name")
        and _SPECTRUMX_ZONE_TAG not in (t.get("name"), t.get("slug"))
    )
    return candidates[0] if candidates else ""


class NetBoxSpectrumXMixin:
    """Spectrum-X VRF/overlay provisioning and reconciliation."""

    async def get_spectrum_x_vrfs(
        self, overlay_name: str, site: str, namespace: str | None = None
    ) -> list[SpectrumXVRF]:
        overlay = await self._find_overlay(overlay_name, site, scope_type=SITE_SCOPE_TYPE)
        if not overlay:
            return []
        vxlans = await self.rest_list(self.VXLANS_PATH, [("overlay_id", str(overlay["id"]))])
        vrf_ids = sorted({str(self._fk_id(v["vrf"])) for v in vxlans if v.get("vrf") is not None})
        if not vrf_ids:
            return []
        data = await self.graphql_query(_SPECTRUMX_VRFS_QUERY, {"ids": vrf_ids})
        # NVCM passes the Site pk; the workflow shows ``site`` to a person, so a
        # VRF with no interface to derive it from reports the Site's name.
        site_row = await self._resolve_scope(site, scope_type=SITE_SCOPE_TYPE)
        site_name = (site_row or {}).get("name") or str(site)
        out: list[SpectrumXVRF] = []
        for vrf in data.get("vrf_list") or []:
            # Namespace is emulated as a VRF tag; derive it from the data (and
            # apply the namespace filter against it) rather than echoing args.
            vrf_tags = tag_names(vrf)
            if namespace and namespace not in vrf_tags:
                continue
            vrf_namespace = namespace or _namespace_tag_name(vrf)
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
                site_name,
            )
            out.append(
                SpectrumXVRF(
                    vrf_id=str(vrf["id"]),
                    name=vrf["name"],
                    namespace=vrf_namespace,
                    site=vrf_site,
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
        vrf_name = f"SpXTenant{vnid}"
        created_overlay_id: int | None = None
        # Track every object created across all namespaces for reverse-order rollback.
        created: list[tuple[str, int]] = []

        # Resolved once, and before the overlay lookup so an unknown site is
        # reported whether or not the overlay already exists. The pk serves both the
        # overlay's scope and the VRFs' cf_nvcm_site.
        site_row = await self._resolve_scope(site, scope_type=SITE_SCOPE_TYPE)
        if site_row is None:
            raise DCIMInvalidDataError(f"NetBox has no Site {site!r} for the Spectrum-X overlay")
        site_pk = site_row["id"]
        tenant_id = await self._resolve_tenant_id(tenant)

        overlay = await self._find_overlay(overlay_name, site, scope_type=SITE_SCOPE_TYPE)
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
            row = await self.rest_request(
                "POST",
                self.OVERLAYS_PATH,
                json={
                    "name": overlay_name,
                    "scope_type": SITE_SCOPE_TYPE,
                    "scope_id": site_row["id"],
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
                    json={
                        "name": vrf_name,
                        "rd": route_distinguisher,
                        "tenant": tenant_id,
                        # An object field: the Site pk, matching what the plugin's
                        # loader seeds on namespace zones.
                        "custom_fields": {_SITE_FIELD: site_pk},
                    },
                )
                created.append((VRFS_PATH, vrf["id"]))
                # Emulate the namespace as a VRF tag (NetBox has no IPAM namespace),
                # alongside the zone tag the RD allocator selects on. Applied as a
                # follow-up PATCH so the create payload stays minimal. Both tags are
                # expected to pre-exist.
                tags = [{"name": _SPECTRUMX_ZONE_TAG}]
                if namespace:
                    tags.append({"name": namespace})
                await self.rest_request("PATCH", f"{VRFS_PATH}{vrf['id']}/", json={"tags": tags})
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
            # Reverse creation order, overlay last. A rollback failure must not
            # replace the original exception; warn that objects were left behind.
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
        overlay = await self._find_overlay(overlay_name, site, scope_type=SITE_SCOPE_TYPE)
        if not overlay:
            return False
        vxlans = await self.rest_list(self.VXLANS_PATH, [("overlay_id", str(overlay["id"]))])
        if vxlans:
            return False
        await self._safe_delete(f"{self.OVERLAYS_PATH}{overlay['id']}/")
        return True

    async def reconcile_spectrum_x_overlay_assignments(
        self,
        overlay_name: str | None,
        site: str,
        device_id: str,
        interface_ids: list[str],
        device_interface_ids: list[str],
    ) -> tuple[int, int]:
        """Make the device's Spectrum-X assignments match intent.

        ``overlay_name`` of ``None`` means remove: the selected ports leave
        whatever Spectrum-X overlay they are in and none is joined.

        ``device_interface_ids`` is every interface on the device, not just the
        selected ones. The device-level assignment is what carries the overlay's
        VRF, so it may only be dropped once no interface still uses that overlay
        -- which needs the whole device's state, and lets a retry clean up after
        a partial attempt.
        """
        target_overlay_id: str | None = None
        if overlay_name is not None:
            overlay = await self._find_overlay(overlay_name, site, scope_type=SITE_SCOPE_TYPE)
            if not overlay:
                raise DCIMInvalidDataError(f"Spectrum-X overlay {overlay_name!r} not found")
            if overlay.get("isolation_type") != _SPECTRUMX_ISOLATION:
                raise DCIMInvalidDataError(f"Overlay {overlay_name!r} is not a Spectrum-X overlay")
            target_overlay_id = str(overlay["id"])

        created = 0
        removed = 0
        isolation_cache: dict[int, str | None] = {}

        # One query per object; the device and its interfaces are independent.
        # ``assigned_object_id`` is a GFK id shared by devices, interfaces and
        # VRFs, so every read below also matches on ``assigned_object_type``.
        selected = [str(i) for i in interface_ids]
        every = list(dict.fromkeys([*selected, *(str(i) for i in device_interface_ids)]))
        device_rows, *interface_rows = await asyncio.gather(
            self.rest_list(self.ASSIGNMENTS_PATH, [("assigned_object_id", str(device_id))]),
            *(self.rest_list(self.ASSIGNMENTS_PATH, [("assigned_object_id", i)]) for i in every),
        )
        by_interface = dict(zip(every, interface_rows, strict=True))

        if target_overlay_id is not None:
            if not any(
                str(self._fk_id(r.get("overlay"))) == target_overlay_id
                for r in device_rows
                if str(r.get("assigned_object_type")) == "dcim.device"
            ):
                await self._create_overlay_assignment(target_overlay_id, "dcim.device", device_id)
                created += 1
            for interface_id in selected:
                rows = by_interface.setdefault(interface_id, [])
                if not any(
                    str(self._fk_id(r.get("overlay"))) == target_overlay_id
                    for r in rows
                    if str(r.get("assigned_object_type")) == "dcim.interface"
                ):
                    await self._create_overlay_assignment(
                        target_overlay_id, "dcim.interface", interface_id
                    )
                    created += 1
                    rows.append(
                        {
                            "id": "created",
                            "overlay": target_overlay_id,
                            "assigned_object_type": "dcim.interface",
                        }
                    )

        # Selected ports leave every other Spectrum-X overlay. With no target
        # that is the whole operation.
        stale = []
        for interface_id in selected:
            for row in by_interface.get(interface_id, []):
                other = self._fk_id(row.get("overlay"))
                if str(row.get("assigned_object_type")) != "dcim.interface":
                    continue
                if target_overlay_id is not None and str(other) == target_overlay_id:
                    continue
                if await self._overlay_isolation(other, isolation_cache) == _SPECTRUMX_ISOLATION:
                    stale.append(row)
        stale_ids = {str(r["id"]) for r in stale}
        removed += len(await self._delete_assignments(stale))
        for interface_id in selected:
            by_interface[interface_id] = [
                r for r in by_interface.get(interface_id, []) if str(r.get("id")) not in stale_ids
            ]

        # A device assignment survives only while some interface still uses it.
        active = {
            str(overlay)
            for rows in by_interface.values()
            for row in rows
            if str(row.get("assigned_object_type")) == "dcim.interface"
            and (overlay := self._fk_id(row.get("overlay"))) is not None
        }
        orphaned = []
        for row in device_rows:
            if str(row.get("assigned_object_type")) != "dcim.device":
                continue
            overlay = self._fk_id(row.get("overlay"))
            if overlay is None or str(overlay) in active:
                continue
            if await self._overlay_isolation(overlay, isolation_cache) == _SPECTRUMX_ISOLATION:
                orphaned.append(row)
        removed += len(await self._delete_assignments(orphaned))

        return created, removed

    # -- helpers -------------------------------------------------------------

    async def _remove_stale_spx_interface_assignments(self, overlay_id, interface_ids) -> int:
        """Drop these interfaces from every *other* Spectrum-X overlay.

        An interface belongs to one Spectrum-X overlay at a time, so joining this
        one leaves any earlier membership stale.
        """
        isolation_cache: dict[int, str | None] = {}
        rows_per_interface = await asyncio.gather(
            *(
                self.rest_list(self.ASSIGNMENTS_PATH, [("assigned_object_id", str(interface_id))])
                for interface_id in interface_ids
            )
        )
        candidates = [
            row
            for rows in rows_per_interface
            for row in rows
            # str() both sides: the plugin may render the FK as a string PK while
            # the overlays endpoint returns an int, and `"7" == 7` is False -- the
            # self-exclusion guard would then delete what was just created.
            if str(row.get("assigned_object_type")) == "dcim.interface"
            and str(self._fk_id(row.get("overlay"))) != str(overlay_id)
        ]
        stale = [
            row
            for row in candidates
            if await self._overlay_isolation(self._fk_id(row.get("overlay")), isolation_cache)
            == _SPECTRUMX_ISOLATION
        ]
        return len(await self._delete_assignments(stale))

    async def _overlay_isolation(self, overlay_id, cache: dict) -> str | None:
        if overlay_id not in cache:
            overlay = await self.rest_request("GET", f"{self.OVERLAYS_PATH}{overlay_id}/")
            cache[overlay_id] = (overlay or {}).get("isolation_type")
        return cache[overlay_id]
