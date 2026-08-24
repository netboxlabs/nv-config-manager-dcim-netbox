# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""IPAM / VRF operations (native NetBox ``ipam`` + VRF assignment)."""

from __future__ import annotations

import asyncio

from nv_config_manager_dcim import DeviceVRF, NamespaceRouteDistinguisher

from nv_config_manager_dcim_netbox._mappers import fk_id, tag_names
from nv_config_manager_dcim_netbox.fabric_common import INTERFACES_PATH, VRFS_PATH

# NetBox has no first-class IPAM "namespace" object: NetBox models a namespace
# (an isolated zone of unique IP space) *as* a VRF. There is no grouping
# container above the VRF, so a namespace *zone* — a set of related VRFs that
# should hold unique route distinguishers — is emulated with a NetBox **tag**
# applied to those VRFs. ``namespace_tag`` selects that zone; each VRF in it is
# itself a namespace carrying one route distinguisher.


class NetBoxIPAMMixin:
    """VRF reads/writes. NetBox has no device-VRF link, so device VRFs are
    derived from the device's interfaces."""

    async def get_device_vrfs(self, device_id: str) -> list[DeviceVRF]:
        # NetBox attaches VRFs to interfaces, never to devices, so a device's VRFs
        # are the union across its interfaces. That is also how
        # ``assign_vrf_to_device`` writes them back.
        interfaces = await self.rest_list(INTERFACES_PATH, [("device_id", str(device_id))])
        vrfs: dict[str, str] = {}
        for interface in interfaces:
            vrf = interface.get("vrf")
            if vrf and vrf.get("id") is not None:
                vrfs[str(vrf["id"])] = vrf.get("name") or ""
        return [DeviceVRF(vrf_id=vrf_id, vrf_name=name) for vrf_id, name in vrfs.items()]

    async def assign_vrf_to_device(self, device_id: str, vrf_id: str) -> None:
        """Assign a VRF to a device by setting it on the device's interfaces.

        NetBox has no device→VRF relationship (VRFs attach to interfaces /
        prefixes / IPs, never devices), so "the device's VRF" is represented as
        the VRF on its interfaces — which is exactly how ``get_device_vrfs``
        reads it back. Interfaces already in the target VRF are skipped so repeat
        calls don't emit redundant changes.
        """
        target = int(vrf_id)
        interfaces = await self.rest_list(INTERFACES_PATH, [("device_id", str(device_id))])
        stale = [
            interface
            for interface in interfaces
            # str-compare: the FK may arrive nested or bare, and as int or str.
            if str(fk_id(interface.get("vrf"))) != str(target)
        ]
        # Independent PATCHes: issue them together rather than one round trip per
        # interface (a spine can carry hundreds).
        await asyncio.gather(
            *(
                self.rest_request(
                    "PATCH", f"{INTERFACES_PATH}{interface['id']}/", json={"vrf": target}
                )
                for interface in stale
            )
        )

    async def assign_vrf_to_interface(self, interface_id: str, vrf_id: str | None) -> None:
        await self.rest_request(
            "PATCH",
            f"{INTERFACES_PATH}{interface_id}/",
            json={"vrf": int(vrf_id) if vrf_id is not None else None},
        )

    async def remove_unmapped_device_vrfs(
        self,
        device_id: str,
        vrf_ids: list[str],
    ) -> list[str]:
        """Return no removals because NetBox has no separate device/VRF relation.

        Interface VRF changes are the complete source of truth in NetBox, so
        there is no device-level association left to reconcile afterward.
        """
        return []

    async def get_namespace_route_distinguishers(
        self, site: str, namespace_tag: str
    ) -> list[NamespaceRouteDistinguisher]:
        """Return the route distinguishers in use in a namespace zone.

        The caller allocates a new, non-conflicting RD, so this returns every RD
        already used by a VRF (namespace) tagged ``namespace_tag``. ``site``
        scopes the candidate VRFs server-side via the ``nvcm_site`` VRF custom
        field (NetBox VRFs have no native site link), so multi-site deployments
        do not draw RD candidates from an unrelated site's namespaces. This is
        guarded on that field being defined; the ``namespace_tag`` (matched by
        VRF tag name or slug) still defines the zone within the site.

        ``site`` is resolved to the Site slug before filtering, because that is what
        ``cf_nvcm_site`` holds — both from the plugin's loader and from
        ``provision_spectrum_x_vrf``. NVCM sends the pk it got from
        ``list_locations``, which would match no zone.
        """
        params: list[tuple[str, str]] = []
        if site:
            params.append(("cf_nvcm_site", await self._site_field_value(site)))
        vrfs = await self.rest_list(VRFS_PATH, params)
        out: list[NamespaceRouteDistinguisher] = []
        for vrf in vrfs:
            if namespace_tag not in tag_names(vrf):
                continue
            # ``provision_spectrum_x_vrf`` re-applies namespace_id as a VRF tag
            # name, so it must be the tag name and not the VRF pk. Take the first
            # tag that is not the zone marker, which is how ``get_spectrum_x_vrfs``
            # reads it back.
            candidate_names = sorted(
                t["name"]
                for t in (vrf.get("tags") or [])
                if isinstance(t, dict)
                and t.get("name")
                and namespace_tag not in (t.get("name"), t.get("slug"))
            )
            ns_name = candidate_names[0] if candidate_names else ""
            out.append(
                NamespaceRouteDistinguisher(
                    namespace_id=ns_name,
                    route_distinguishers=(vrf["rd"],) if vrf.get("rd") else (),
                )
            )
        return out
