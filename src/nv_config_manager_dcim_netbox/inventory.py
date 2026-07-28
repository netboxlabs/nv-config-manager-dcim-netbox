# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Core inventory / DCIM operations (native NetBox ``dcim``)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from nv_config_manager_dcim import (
    DCIMConflictError,
    DCIMDeviceSelection,
    DCIMInvalidDataError,
    DCIMNotFoundError,
)

from nv_config_manager_dcim_netbox._mappers import (
    device_metadata_from_gql,
    device_metadata_from_rest,
    host_device_from_gql,
    interface_from_rest,
    network_device_from_rest,
)

if TYPE_CHECKING:
    from nv_config_manager_dcim import DeviceMetadata
    from nv_config_manager_dcim.workflow_models import (
        DeviceInventoryFilter,
        HostDeviceData,
        InterfaceData,
        NetworkDeviceData,
    )

    from nv_config_manager_dcim_netbox.models import DCIMDeviceSelectionFilter

logger = logging.getLogger(__name__)

_DEVICE_METADATA_QUERY = """
query ($id: ID!) {
  device(id: $id) {
    id
    name
    role { name }
    platform { name }
    rack { name }
    site { name }
    primary_ip4 { address }
  }
}
"""

_DEVICE_SERIAL_QUERY = """
query ($id: ID!) { device(id: $id) { serial } }
"""

_DEVICE_BY_NAME_QUERY = """
query ($name: String!) {
  device_list(filters: {name: {exact: $name}}) {
    id
    name
    platform { slug }
  }
}
"""

_HOST_DEVICE_FIELDS = """
    id
    name
    serial
    position
    role { slug }
    site { name }
    rack { name }
    device_type { slug }
    devicebays { id name installed_device { id } }
    interfaces {
      id
      name
      primary_mac_address { mac_address }
      vrf { id }
    }
"""

_HOST_DEVICE_QUERY = f"""
query ($id: ID!) {{
  device(id: $id) {{{_HOST_DEVICE_FIELDS}}}
}}
"""

_HOST_DEVICES_BY_IDS_QUERY = f"""
query ($ids: [ID!]!) {{
  device_list(filters: {{id: {{in_list: $ids}}}}) {{{_HOST_DEVICE_FIELDS}}}
}}
"""


class NetBoxInventoryMixin:
    """Device and interface inventory reads."""

    async def get_device_metadata(self, device_id: str) -> DeviceMetadata | None:
        # Degrades to None on any failure so metadata caching stays resilient.
        try:
            data = await self.graphql_query(_DEVICE_METADATA_QUERY, {"id": device_id})
            device = data.get("device")
            if not device:
                return None
            return device_metadata_from_gql(device, self.get_device_ui_url(device_id))
        except Exception:  # noqa: BLE001 - metadata read is best-effort
            logger.exception("Failed to read NetBox metadata for device %s", device_id)
            return None

    async def get_managed_device_metadata(self, page_size: int = 100) -> list[DeviceMetadata]:
        devices = await self.rest_list(
            "dcim/devices/", [("cf_nvcm_render_enabled", "true")], page_size=page_size
        )
        return [device_metadata_from_rest(d, self.get_device_ui_url(str(d["id"]))) for d in devices]

    async def get_device_serial(self, device_id: str) -> str:
        data = await self.graphql_query(_DEVICE_SERIAL_QUERY, {"id": device_id})
        device = data.get("device")
        serial = device.get("serial") if device else None
        if not serial:
            raise DCIMNotFoundError(f"NetBox device {device_id} has no serial")
        return str(serial)

    async def get_device_selection_by_name(self, name: str) -> DCIMDeviceSelection:
        data = await self.graphql_query(_DEVICE_BY_NAME_QUERY, {"name": name})
        devices = data.get("device_list") or []
        if not devices:
            raise DCIMNotFoundError(f"No NetBox device named {name!r}")
        if len(devices) > 1:
            raise DCIMConflictError(f"Multiple NetBox devices named {name!r}")
        device = devices[0]
        return DCIMDeviceSelection(
            id=str(device["id"]),
            name=device["name"],
            platform=(device.get("platform") or {}).get("slug"),
        )

    async def list_devices(self, filters: DCIMDeviceSelectionFilter) -> list[DCIMDeviceSelection]:
        params: list[tuple[str, str]] = [("has_primary_ip", "true")]
        for site in filters.sites:
            params.append(("site", site))
        for status in filters.statuses:
            params.append(("status", status))
        for role in filters.roles:
            params.append(("role", role))
        for tenant in filters.tenants:
            params.append(("tenant", tenant))
        for device_type_id in filters.device_type_ids:
            params.append(("device_type_id", device_type_id))
        for manufacturer in filters.manufacturers:
            params.append(("manufacturer", manufacturer))
        for platform in filters.platforms:
            params.append(("platform", platform))
        # NOTE: filters.managed_only relies on the NVCM state custom field (not yet
        # present in NetBox); it is intentionally not applied here.
        devices = await self.rest_list("dcim/devices/", params)
        selections = []
        for device in devices:
            if not device.get("name"):
                continue
            selections.append(
                DCIMDeviceSelection(
                    id=str(device["id"]),
                    name=device["name"],
                    platform=(device.get("platform") or {}).get("slug"),
                )
            )
        return selections

    async def get_network_device(self, device_id: str) -> NetworkDeviceData:
        device = await self.rest_request("GET", f"dcim/devices/{device_id}/")
        if not device:
            raise DCIMNotFoundError(f"NetBox device {device_id} not found")
        return network_device_from_rest(device)

    async def get_network_devices(self, filters: DeviceInventoryFilter) -> list[NetworkDeviceData]:
        params = await self._device_filter_params(filters)
        if params is None:
            return []
        devices = await self.rest_list("dcim/devices/", params)
        seen: set[str] = set()
        out: list[NetworkDeviceData] = []
        for device in devices:
            if device["name"] in seen:
                continue
            try:
                mapped = network_device_from_rest(device)
            except DCIMInvalidDataError as exc:
                # One un-mappable device (e.g. no platform) must not fail the
                # whole listing; skip it and keep the rest.
                logger.warning(
                    "Skipping NetBox device %s from inventory: %s", device.get("id"), exc
                )
                continue
            seen.add(device["name"])
            out.append(mapped)
        return out

    async def get_host_device(self, device_id: str) -> HostDeviceData:
        data = await self.graphql_query(_HOST_DEVICE_QUERY, {"id": device_id})
        device = data.get("device")
        if not device:
            raise DCIMNotFoundError(f"NetBox device {device_id} not found")
        return host_device_from_gql(device)

    async def get_host_devices(self, filters: DeviceInventoryFilter) -> list[HostDeviceData]:
        params = await self._device_filter_params(filters)
        if params is None:
            return []
        devices = await self.rest_list("dcim/devices/", params)
        ids = [str(d["id"]) for d in devices]
        return await self._host_devices_by_ids(ids)

    async def get_device_interfaces(self, device_id: str) -> list[InterfaceData]:
        interfaces = await self.rest_list("dcim/interfaces/", [("device_id", str(device_id))])
        return [interface_from_rest(i) for i in interfaces]

    # -- shared helpers ------------------------------------------------------

    async def _host_devices_by_ids(self, ids: list[str]) -> list[HostDeviceData]:
        if not ids:
            return []
        data = await self.graphql_query(_HOST_DEVICES_BY_IDS_QUERY, {"ids": ids})
        seen: set[str] = set()
        out: list[HostDeviceData] = []
        for device in data.get("device_list") or []:
            if device["name"] in seen:
                continue
            seen.add(device["name"])
            out.append(host_device_from_gql(device))
        return out

    async def _device_filter_params(
        self, filters: DeviceInventoryFilter
    ) -> list[tuple[str, str]] | None:
        """Build ``dcim/devices`` query params, or ``None`` if nothing can match.

        Returns ``None`` when a MAC-address filter was requested but matched no
        interface — callers must treat that as an empty result rather than an
        unfiltered (whole-fleet) query.
        """
        params: list[tuple[str, str]] = []
        if filters.site:
            params.append(("site", filters.site))
        for role in filters.roles or []:
            params.append(("role", role))
        for status in filters.statuses or []:
            params.append(("status", status))
        if filters.tenant:
            params.append(("tenant", filters.tenant))
        for device_type_id in filters.device_type_ids or []:
            params.append(("device_type_id", device_type_id))
        for platform in filters.platforms or []:
            params.append(("platform", platform.value))
        for device_id in filters.device_ids or []:
            params.append(("id", str(device_id)))
        # MAC filtering on devices is done via the interfaces endpoint in NetBox.
        # A requested MAC that matches no interface must exclude everything, not
        # fall through to an unfiltered query.
        if filters.mac_addresses:
            mac_device_ids = await self._device_ids_for_macs(filters.mac_addresses)
            if not mac_device_ids:
                return None
            for device_id in mac_device_ids:
                params.append(("id", device_id))
        # NOTE: render/deploy/backup/ztp_enabled and managed_only depend on the
        # NVCM state custom field (not yet present); those filters are not applied.
        return params

    async def _interfaces_for_macs(self, mac_addresses: list[str] | None) -> list[dict]:
        """List interfaces carrying any of ``mac_addresses`` (empty if none given)."""
        if not mac_addresses:
            return []
        params = [("mac_address", mac) for mac in mac_addresses]
        return await self.rest_list("dcim/interfaces/", params)

    async def _device_ids_for_macs(self, mac_addresses: list[str] | None) -> list[str]:
        interfaces = await self._interfaces_for_macs(mac_addresses)
        return sorted({str(i["device"]["id"]) for i in interfaces if i.get("device")})
