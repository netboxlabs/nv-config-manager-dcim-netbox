# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""IPAM / VRF operations (native NetBox ``ipam`` + VRF assignment)."""

from __future__ import annotations

import asyncio

from nv_config_manager_dcim import DeviceVRF, NamespaceRouteDistinguisher

from nv_config_manager_dcim_netbox._mappers import tag_names
from nv_config_manager_dcim_netbox.fabric_common import INTERFACES_PATH, VRFS_PATH

# NetBox has no first-class IPAM "namespace" object: NetBox models a namespace
# (an isolated zone of unique IP space) *as* a VRF. There is no grouping
# container above the VRF, so a namespace *zone* -- a set of related VRFs that
# should hold unique route distinguishers -- is emulated with a NetBox **tag**
# applied to those VRFs. ``namespace_tag`` selects that zone; within it a
# second tag names the namespace, and every VRF carrying that tag contributes
# its route distinguisher to the namespace.


class NetBoxIPAMMixin:
    """VRF reads/writes. NetBox has no device-VRF link, so device VRFs are
    derived from the device's interfaces."""

    async def get_device_vrfs(self, device_id: str) -> list[DeviceVRF]:
        # NetBox attaches VRFs to interfaces, never to devices, so a device's VRFs
        # are the union across its interfaces.
        interfaces = await self.rest_list(INTERFACES_PATH, [("device_id", str(device_id))])
        vrfs: dict[str, str] = {}
        for interface in interfaces:
            vrf = interface.get("vrf")
            if vrf and vrf.get("id") is not None:
                vrfs[str(vrf["id"])] = vrf.get("name") or ""
        return [DeviceVRF(vrf_id=vrf_id, vrf_name=name) for vrf_id, name in vrfs.items()]

    async def assign_vrf_to_device(self, device_id: str, vrf_id: str) -> None:
        """No-op: NetBox has no device/VRF relation.

        The reference provider inserts a device-level assignment row and touches
        no interfaces; the ports stage that follows is what puts the selected
        ports in the VRF. Writing the VRF onto every interface here would move
        ports belonging to other tenants on a shared switch.
        """

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

        The reference provider reconciles a device-level assignment table here;
        NetBox has no such model, so the interface VRFs are the whole association.
        The workflow still deploys the full configuration when ports are unassigned
        or leave an overlay, which is what strips a dead VRF from the switch.
        """
        return []

    async def get_namespace_route_distinguishers(
        self, site: str, namespace_tag: str
    ) -> list[NamespaceRouteDistinguisher]:
        """Return the namespaces at ``site`` and every route distinguisher NetBox
        will refuse for a new VRF there.

        The namespaces are the VRFs tagged ``namespace_tag`` at ``site``. NetBox
        VRFs have no native site link, so ``site`` filters on the ``nvcm_site``
        custom field, resolved to the Site pk first: that is what ``cf_nvcm_site``
        holds, while NVCM sends the pk from ``list_locations``. One entry per
        namespace: the zone VRF and every tenant VRF ``provision_spectrum_x_vrf``
        created in it carry the same namespace tag. ``namespace_id`` is that tag
        name (not the VRF pk), which is what provision re-applies to the new VRF.
        The caller provisions one VRF per entry.

        The ``route_distinguishers`` are deliberately *not* scoped to the site or
        the tag. A Nautobot namespace scopes RD and VNI uniqueness, so listing the
        RDs of its own VRFs is the whole taken set there; NetBox makes
        ``ipam.VRF.rd`` and the plugin's ``VXLAN.vnid`` unique across the install.
        NVCM's allocator only subtracts the RDs it is handed from
        ``[rd_min, rd_max]``, so a taken set narrower than NetBox's uniqueness rule
        hands back a value another site already holds, the VRF POST fails, and
        every retry recomputes the same value. Every namespace therefore reports
        the RDs of all VRFs (any site, any tag) plus ``*:<vnid>`` for every plugin
        VXLAN, so a VNI another overlay uses is excluded as well.
        """
        params: list[tuple[str, str]] = []
        if site:
            params.append(("cf_nvcm_site", await self._site_field_value(site)))
        reads = [self.rest_list(VRFS_PATH), self.rest_list(self.VXLANS_PATH)]
        if params:
            reads.append(self.rest_list(VRFS_PATH, params))
        all_vrfs, vxlans, *site_vrfs = await asyncio.gather(*reads)
        zone_vrfs = site_vrfs[0] if site_vrfs else all_vrfs
        taken = dict.fromkeys(vrf["rd"] for vrf in all_vrfs if vrf.get("rd"))
        taken.update(dict.fromkeys(f"*:{v['vnid']}" for v in vxlans if v.get("vnid") is not None))
        route_distinguishers = tuple(taken)

        namespaces: dict[str, None] = {}
        for vrf in zone_vrfs:
            if namespace_tag not in tag_names(vrf):
                continue
            # The first tag that is not the zone marker, which is how
            # ``get_spectrum_x_vrfs`` reads the namespace back.
            candidate_names = sorted(
                t["name"]
                for t in (vrf.get("tags") or [])
                if isinstance(t, dict)
                and t.get("name")
                and namespace_tag not in (t.get("name"), t.get("slug"))
            )
            namespaces.setdefault(candidate_names[0] if candidate_names else "")
        return [
            NamespaceRouteDistinguisher(
                namespace_id=ns_name, route_distinguishers=route_distinguishers
            )
            for ns_name in namespaces
        ]
