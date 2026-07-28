# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Render data assembly.

``get_render_data`` maps NetBox's native inventory and intent into the
provider-neutral :class:`RenderData` contract the template engine consumes:

* **identity** — stable placement values used to select templates (id, name,
  platform, role, model, location hierarchy, tags).
* **intent** — NetBox's rendered ``config_context`` for the device. NetBox
  merges region/site/location/role/platform-scoped contexts into this single
  payload, so location-scoped intent is already reflected here.
* **interfaces** — normalized NetBox interfaces (with VRF, VLANs, IPs, MACs).
* **inventory** — the fabric relationships not carried by a first-class SDK
  model: derived VRFs, BGP sessions (``netbox-bgp``), and the plugin's
  overlays / VXLANs / overlay-assignments.

BGP and the fabric overlay models are read over REST (neither exposes GraphQL in
this NetBox build); the device, its interfaces, and config-context come from a
single GraphQL query.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from nv_config_manager_dcim import (
    DCIMInvalidDataError,
    DCIMNotFoundError,
    DeviceRenderData,
    IntendedInterfaceNeighbor,
    IntendedNeighborDevice,
    LocationRenderData,
    RenderData,
    RenderDeviceIdentity,
    RenderLocation,
)

if TYPE_CHECKING:
    from nv_config_manager_dcim import RenderDataRequest

_RENDER_DEVICE_QUERY = """
query ($id: ID!) {
  device(id: $id) {
    id
    name
    serial
    role { name }
    platform { name }
    device_type { model manufacturer { name } }
    tenant { name }
    tags { name }
    config_context
    site {
      id
      name
      slug
      region { name }
    }
    location {
      id
      name
      parent { id name parent { id name } }
    }
    interfaces {
      id
      name
      enabled
      type
      mtu
      mgmt_only
      description
      mode
      mac_addresses { mac_address }
      vrf { name rd }
      untagged_vlan { vid name }
      tagged_vlans { vid name }
      ip_addresses { address }
      tags { name }
    }
  }
}
"""

_INTENDED_NEIGHBORS_QUERY = """
query ($id: ID!) {
  device(id: $id) {
    interfaces {
      name
      enabled
      tags { name }
      connected_endpoints {
        ... on InterfaceType {
          name
          primary_mac_address { mac_address }
          device {
            name
            serial
            position
            role { name }
            rack { name }
          }
        }
      }
    }
  }
}
"""


def _require(value: Any, description: str) -> Any:
    """Return a required render value or raise an SDK-level error."""
    if not value:
        raise DCIMInvalidDataError(f"NetBox device is missing required {description} for render")
    return value


def _tags(obj: dict[str, Any] | None) -> tuple[str, ...]:
    return tuple(tag["name"] for tag in (obj or {}).get("tags") or [] if tag.get("name"))


def _location_hierarchy(device: dict[str, Any]) -> tuple[RenderLocation, RenderLocation]:
    """Build the device location chain and its Site root.

    Returns ``(device_location, site_location)``. The device location is the
    innermost NetBox location (if any) nested within its Site; the Site is the
    root ancestor used by site-scoped template filters. NetBox always assigns a
    device to a Site, so the Site node is always present.
    """
    site = _require(device.get("site"), "site")
    region = site.get("region")
    site_location = RenderLocation(
        id=str(site["id"]) if site.get("id") is not None else None,
        name=str(site["name"]),
        kind="Site",
        parent=(
            RenderLocation(name=str(region["name"]), kind="Region")
            if isinstance(region, dict) and region.get("name")
            else None
        ),
    )

    # NetBox locations nest within a Site; build the chain from the outermost
    # parent down to the device's innermost location, rooted at the Site.
    chain: list[dict[str, Any]] = []
    node = device.get("location")
    while isinstance(node, dict):
        chain.append(node)
        node = node.get("parent")
    device_location = site_location
    for level in reversed(chain):
        device_location = RenderLocation(
            id=str(level["id"]) if level.get("id") is not None else None,
            name=str(level["name"]),
            kind="Location",
            parent=device_location,
        )
    return device_location, site_location


def _device_vrfs(interfaces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collect the distinct VRFs referenced by a device's interfaces."""
    vrfs: dict[str, dict[str, Any]] = {}
    for interface in interfaces:
        vrf = interface.get("vrf")
        if isinstance(vrf, dict) and vrf.get("name") and vrf["name"] not in vrfs:
            vrfs[vrf["name"]] = {"name": vrf["name"], "rd": vrf.get("rd")}
    return list(vrfs.values())


class NetBoxRenderMixin:
    """Normalized render-data assembly for the template engine."""

    async def _safe_list(self, path: str, params: list[tuple[str, str]] | None = None):
        """List a REST endpoint, treating a missing endpoint as no data.

        The fabric plugin and ``netbox-bgp`` are optional: if either is not
        installed the render still succeeds with those relationships empty.
        """
        try:
            return await self.rest_list(path, params)
        except DCIMNotFoundError:
            return []

    async def _device_overlay_assignments(
        self, device_id: str, interface_ids: set[str]
    ) -> list[dict[str, Any]]:
        """Return overlay assignments targeting this device or its interfaces.

        Fetches only the assignments for the relevant object ids (the device and
        its interfaces) instead of the whole table, then filters client-side by
        (assigned_object_type, assigned_object_id) so a generic-FK id collision
        between a device and an interface cannot leak an unrelated assignment.
        """
        object_ids = [str(device_id), *sorted(interface_ids)]
        results = await asyncio.gather(
            *(
                self._safe_list(self.ASSIGNMENTS_PATH, [("assigned_object_id", object_id)])
                for object_id in object_ids
            )
        )
        # Dedupe by assignment id in case a device and interface share a numeric id.
        assignments = {a["id"]: a for rows in results for a in rows}.values()
        out: list[dict[str, Any]] = []
        for assignment in assignments:
            content_type = assignment.get("assigned_object_type")
            assigned_id = str(assignment.get("assigned_object_id"))
            if content_type == "dcim.device" and assigned_id == str(device_id):
                out.append(assignment)
            elif content_type == "dcim.interface" and assigned_id in interface_ids:
                out.append(assignment)
        return out

    async def get_render_data(self, request: RenderDataRequest) -> RenderData:
        """Assemble the provider-neutral render payload for one device."""
        device_id = request.device_id
        data = await self.graphql_query(_RENDER_DEVICE_QUERY, {"id": device_id})
        device = _require(data.get("device"), "device")

        platform = _require(device.get("platform"), "platform")
        role = _require(device.get("role"), "role")
        device_type = _require(device.get("device_type"), "device type")
        interfaces = list(device.get("interfaces") or [])
        interface_ids = {str(interface["id"]) for interface in interfaces if interface.get("id")}

        device_location, site_location = _location_hierarchy(device)

        # Fabric + BGP relationships (REST; optional plugins degrade to empty).
        bgp_sessions = await self._safe_list(
            "plugins/bgp/session/", [("device_id", str(device_id))]
        )
        overlays = await self._safe_list(self.OVERLAYS_PATH)
        vxlans = await self._safe_list(self.VXLANS_PATH)
        overlay_assignments = await self._device_overlay_assignments(device_id, interface_ids)

        # Site-scoped inventory available natively (region/site/location-scoped
        # config context is already merged into the device intent above).
        site_id = str(site_location.id) if site_location.id else None
        site_params = [("site_id", site_id)] if site_id else None
        prefixes = await self._safe_list("ipam/prefixes/", site_params) if site_id else []
        vlans = await self._safe_list("ipam/vlans/", site_params) if site_id else []

        return RenderData(
            device=DeviceRenderData(
                identity=RenderDeviceIdentity(
                    id=str(device["id"]),
                    name=str(_require(device.get("name"), "name")),
                    platform=str(platform["name"]),
                    role=str(role["name"]),
                    model=str(_require(device_type.get("model"), "device model")),
                    location=device_location,
                    tags=_tags(device),
                ),
                interfaces=tuple(interfaces),
                inventory={
                    "serial": device.get("serial") or None,
                    "tenant": (device.get("tenant") or {}).get("name"),
                    "manufacturer": (device_type.get("manufacturer") or {}).get("name"),
                    "vrfs": _device_vrfs(interfaces),
                    "bgp_sessions": bgp_sessions,
                    "overlays": overlays,
                    "vxlans": vxlans,
                    "overlay_assignments": overlay_assignments,
                },
                intent=device.get("config_context") or {},
            ),
            location=LocationRenderData(
                location=site_location,
                inventory={"prefixes": prefixes, "vlans": vlans},
                intent={},
            ),
        )

    async def get_intended_interface_neighbors(
        self, device_id: str
    ) -> list[IntendedInterfaceNeighbor]:
        data = await self.graphql_query(_INTENDED_NEIGHBORS_QUERY, {"id": device_id})
        device = data.get("device")
        if not device:
            return []
        neighbors: list[IntendedInterfaceNeighbor] = []
        for interface in device.get("interfaces") or []:
            if not interface.get("enabled", True):
                continue
            tags = tuple(tag["name"] for tag in interface.get("tags") or [] if tag.get("name"))
            endpoint = next(iter(interface.get("connected_endpoints") or []), None)
            if not endpoint:
                neighbors.append(IntendedInterfaceNeighbor(name=interface["name"], tags=tags))
                continue
            peer_device = endpoint.get("device") or {}
            if not peer_device.get("name"):
                raise DCIMInvalidDataError(
                    f"NetBox connected endpoint for {interface['name']} has no device name"
                )
            neighbors.append(
                IntendedInterfaceNeighbor(
                    name=interface["name"],
                    tags=tags,
                    connected_interface_name=endpoint.get("name"),
                    connected_interface_mac=(endpoint.get("primary_mac_address") or {}).get(
                        "mac_address"
                    ),
                    connected_device=IntendedNeighborDevice(
                        name=peer_device["name"],
                        serial=peer_device.get("serial"),
                        role=(peer_device.get("role") or {}).get("name"),
                        rack=(peer_device.get("rack") or {}).get("name"),
                        position=peer_device.get("position"),
                    ),
                )
            )
        return neighbors
