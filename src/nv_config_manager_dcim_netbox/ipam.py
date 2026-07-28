# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""IPAM / VRF operations (native NetBox ``ipam`` + VRF assignment)."""

from __future__ import annotations

from nv_config_manager_dcim import DeviceVRF, NamespaceRouteDistinguisher

# NetBox has no first-class IPAM "namespace" object: NetBox models a namespace
# (an isolated zone of unique IP space) *as* a VRF. There is no grouping
# container above the VRF, so a namespace *zone* — a set of related VRFs that
# should hold unique route distinguishers — is emulated with a NetBox **tag**
# applied to those VRFs. ``namespace_tag`` selects that zone; each VRF in it is
# itself a namespace carrying one route distinguisher.
VRFS_PATH = "ipam/vrfs/"


def _vrf_tag_names(vrf: dict) -> set[str]:
    """Return the tag names and slugs on a VRF (match on either)."""
    names: set[str] = set()
    for tag in vrf.get("tags") or []:
        if isinstance(tag, dict):
            for key in ("name", "slug"):
                value = tag.get(key)
                if isinstance(value, str) and value:
                    names.add(value)
    return names


class NetBoxIPAMMixin:
    """VRF reads/writes. NetBox has no device-VRF link, so device VRFs are
    derived from the device's interfaces."""

    async def get_device_vrfs(self, device_id: str) -> list[DeviceVRF]:
        interfaces = await self.rest_list("dcim/interfaces/", [("device_id", str(device_id))])
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
        interfaces = await self.rest_list("dcim/interfaces/", [("device_id", str(device_id))])
        for interface in interfaces:
            current = interface.get("vrf")
            current_id = current.get("id") if isinstance(current, dict) else current
            if current_id == target:
                continue
            await self.rest_request(
                "PATCH", f"dcim/interfaces/{interface['id']}/", json={"vrf": target}
            )

    async def assign_vrf_to_interface(self, interface_id: str, vrf_id: str) -> None:
        await self.rest_request(
            "PATCH", f"dcim/interfaces/{interface_id}/", json={"vrf": int(vrf_id)}
        )

    async def get_namespace_route_distinguishers(
        self, site: str, namespace_tag: str
    ) -> list[NamespaceRouteDistinguisher]:
        """Return the route distinguishers in use in a namespace zone.

        The caller allocates a new, non-conflicting RD, so this returns every RD
        already used by a VRF (namespace) tagged ``namespace_tag``. ``site`` is
        accepted for contract parity but does not constrain the result — NetBox
        VRFs are global, so the tag alone defines the zone. Matches a VRF tag by
        either its name or slug.
        """
        vrfs = await self.rest_list(VRFS_PATH)
        return [
            NamespaceRouteDistinguisher(
                namespace_id=str(vrf["id"]),
                route_distinguishers=(vrf["rd"],) if vrf.get("rd") else (),
            )
            for vrf in vrfs
            if namespace_tag in _vrf_tag_names(vrf)
        ]
