# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Core inventory / DCIM operations (native NetBox ``dcim``)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, TypeVar

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
    status_slug,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from nv_config_manager_dcim import DCIMDeviceSelectionFilter, DeviceMetadata
    from nv_config_manager_dcim.workflow_models import (
        DeviceInventoryFilter,
        HostDeviceData,
        InterfaceData,
        NetworkDeviceData,
    )

logger = logging.getLogger(__name__)

# Named-workflow-model rows returned by ``_map_unique_by_name`` (both have ``name``).
_T = TypeVar("_T", "NetworkDeviceData", "HostDeviceData")

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
        # Read failures propagate. ``None`` means "NetBox has no such device" and
        # nothing else, so reference validation can report an unknown device
        # without conflating it with a transient GraphQL or connectivity failure.
        # NetBox reports the missing singular object as an error, which the
        # transport classifies as DCIMNotFoundError.
        try:
            data = await self.graphql_query(_DEVICE_METADATA_QUERY, {"id": device_id})
        except DCIMNotFoundError:
            logger.warning("Device %s not found in NetBox", device_id)
            return None
        device = data.get("device")
        if not device:
            logger.warning("Device %s not found in NetBox", device_id)
            return None
        return device_metadata_from_gql(device, self.get_device_ui_url(device_id))

    async def get_managed_device_metadata(self, page_size: int = 100) -> list[DeviceMetadata]:
        # Fail-closed by design: "managed" is scoped to the ``nvcm_render_enabled``
        # custom field. If that field is not defined on the NetBox instance,
        # NetBox rejects the ``cf_nvcm_render_enabled`` filter with HTTP 400
        # rather than ignoring it, so this never silently returns the whole fleet.
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
        # Every value is coerced to str: aiohttp requires string query params (see
        # ``rest_request``), and the SDK's filter fields are only str by convention.
        params: list[tuple[str, str]] = [("has_primary_ip", "true")]
        for key, values in (
            ("site", filters.sites),
            ("status", filters.statuses),
            ("role", filters.roles),
            ("tenant", filters.tenants),
            ("device_type_id", filters.device_type_ids),
            ("manufacturer", filters.manufacturers),
            ("platform", filters.platforms),
        ):
            # ``status`` must be the NetBox slug (lowercase); workflows pass the
            # display label ("Active"). See ``status_slug``.
            coerce = status_slug if key == "status" else str
            params.extend((key, coerce(value)) for value in values or ())
        if filters.managed_only:
            # "Managed" is modeled by the ``nvcm_render_enabled`` device custom
            # field (the same proxy ``get_managed_device_metadata`` uses). Guarded
            # on that field: NetBox 400s if it is not defined rather than widening
            # the result set.
            params.append(("cf_nvcm_render_enabled", "true"))
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
        return self._map_unique_by_name(devices, network_device_from_rest)

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

    @staticmethod
    def _map_unique_by_name(devices: list[dict], mapper: Callable[[dict], _T]) -> list[_T]:
        """Map device rows, skipping unmappable ones and rejecting duplicate names.

        A device that cannot be mapped — no platform, or no name (``Device.name``
        is nullable in NetBox) — must not fail the whole listing; it is logged and
        skipped. The no-platform skip is a deliberate NetBox improvement over the
        reference provider, which has no such notion.

        A *duplicate* name is different: it is a data-integrity error. The
        workflow keys devices by name, so two rows sharing one name is ambiguous
        and must not be silently collapsed (parity with the reference provider's
        de-duplication guard).
        """
        seen: set[str] = set()
        out: list[_T] = []
        for device in devices:
            try:
                mapped = mapper(device)
            except DCIMInvalidDataError as exc:
                logger.warning(
                    "Skipping NetBox device %s from inventory: %s", device.get("id"), exc
                )
                continue
            if mapped.name in seen:
                raise DCIMInvalidDataError(
                    f"Duplicate device name in NetBox inventory: {mapped.name!r}"
                )
            seen.add(mapped.name)
            out.append(mapped)
        return out

    async def _host_devices_by_ids(self, ids: list[str]) -> list[HostDeviceData]:
        if not ids:
            return []
        data = await self.graphql_query(_HOST_DEVICES_BY_IDS_QUERY, {"ids": ids})
        return self._map_unique_by_name(data.get("device_list") or [], host_device_from_gql)

    async def _device_filter_params(
        self, filters: DeviceInventoryFilter
    ) -> list[tuple[str, str]] | None:
        """Build ``dcim/devices`` query params, or ``None`` if nothing can match.

        Returns ``None`` when a MAC-address filter was requested but matched no
        interface — callers must treat that as an empty result rather than an
        unfiltered (whole-fleet) query. Raises ``DCIMInvalidDataError`` when no
        filter of any kind was supplied, so an empty filter can never degrade
        into a full-fleet listing (parity with the reference provider's
        "must apply at least one filter" guard).
        """
        params: list[tuple[str, str]] = []
        if filters.site:
            params.append(("site", str(filters.site)))
        if filters.tenant:
            params.append(("tenant", str(filters.tenant)))
        for key, values in (
            ("role", filters.roles),
            ("status", filters.statuses),
            ("device_type_id", filters.device_type_ids),
            ("id", filters.device_ids),
        ):
            # ``status`` must be the NetBox slug (lowercase); workflows pass the
            # display label ("Active"). See ``status_slug``.
            coerce = status_slug if key == "status" else str
            params.extend((key, coerce(value)) for value in values or ())
        for platform in filters.platforms or []:
            params.append(("platform", str(platform.value)))
        # NVCM status-flag filters map to boolean device custom fields. ``managed``
        # reuses ``nvcm_render_enabled`` as its proxy (the same field
        # ``get_managed_device_metadata`` uses), so only fold it in when an
        # explicit render filter was not already supplied. Each is guarded on the
        # field being defined: NetBox 400s on an unknown ``cf_*`` filter rather
        # than widening the result.
        render_enabled = filters.render_enabled
        if filters.managed_only and render_enabled is None:
            render_enabled = True
        status_flags = {
            "nvcm_render_enabled": render_enabled,
            "nvcm_deploy_enabled": filters.deploy_enabled,
            "nvcm_backup_enabled": filters.backup_enabled,
            "nvcm_ztp_enabled": filters.ztp_enabled,
        }
        for field, value in status_flags.items():
            if value is not None:
                params.append((f"cf_{field}", str(value).lower()))
        # Reject an entirely unfiltered request before it becomes a whole-fleet
        # query. A MAC-only filter is legitimate: it is resolved below.
        if not params and not filters.mac_addresses:
            raise DCIMInvalidDataError("At least one device inventory filter must be supplied")
        # MAC filtering on devices is done via the interfaces endpoint in NetBox.
        # A requested MAC that matches no interface must exclude everything, not
        # fall through to an unfiltered query.
        if filters.mac_addresses:
            mac_device_ids = await self._device_ids_for_macs(filters.mac_addresses)
            if not mac_device_ids:
                return None
            for device_id in mac_device_ids:
                params.append(("id", device_id))
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
