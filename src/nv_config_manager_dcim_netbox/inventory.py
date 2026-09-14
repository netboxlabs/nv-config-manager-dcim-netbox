# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Core inventory / DCIM operations (native NetBox ``dcim``)."""

from __future__ import annotations

import asyncio
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
    slug_or_id,
    status_slug,
)
from nv_config_manager_dcim_netbox._mappers import device_pk as _device_pk
from nv_config_manager_dcim_netbox.fabric_common import (
    DEVICES_PATH,
    INTERFACES_PATH,
    STATUS_PATH,
    chunked,
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
        # ``None`` means "no such device" and nothing else, so reference validation
        # can report an unknown device without masking a transient failure. NetBox
        # reports the miss as an error, which the transport maps to DCIMNotFoundError.
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

    async def managed_device_ids(self) -> set[str]:
        """Device PKs that have a ``DeviceConfigStatus`` record.

        "Managed" means the record exists, whatever its flags -- the reference
        provider's ``config_manager_devices`` / ``nv_config_manager_device_status``
        filters. A device with ``render_enabled=False`` is still managed; the
        render gate is a separate filter.
        """
        rows = await self.rest_list(STATUS_PATH)
        return {pk for row in rows if (pk := _device_pk(row))}

    async def get_managed_device_metadata(self, page_size: int = 100) -> list[DeviceMetadata]:
        managed_ids = await self.managed_device_ids()
        devices = await self.devices_by_ids(managed_ids, page_size=page_size)
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
        #
        # Not an SDK filter field, but hardcoded in the reference's own
        # `ListDeviceSelections` query -- dropping it offers devices its pickers
        # never show.
        params: list[tuple[str, str]] = [("has_primary_ip", "true")]
        for key, values in (
            ("site", filters.sites),
            ("status", filters.statuses),
            ("role", filters.roles),
            ("tenant", filters.tenants),
            ("device_type_id", filters.device_type_ids),
            ("manufacturer", filters.manufacturers),
        ):
            for value in values or ():
                if key == "status":
                    # Slug (lowercase); workflows pass the display label ("Active").
                    params.append((key, status_slug(value)))
                elif key == "device_type_id":
                    params.append((key, str(value)))
                else:
                    # Name-or-id: NetBox keys these by slug, with the id under
                    # `<key>_id`. Workflows send either. See ``slug_or_id``.
                    params.append(slug_or_id(key, value))
        platform_params = await self._platform_params(filters.platforms)
        if platform_params is None:
            # No device can carry a platform NetBox does not define.
            return []
        params.extend(platform_params)
        if filters.managed_only:
            # Managed ids, ANDed with the rest of the filter.
            managed_ids = await self.managed_device_ids()
            if not managed_ids:
                return []
            devices = await self.devices_by_ids(managed_ids, params)
        else:
            devices = await self.rest_list(DEVICES_PATH, params)
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
        device, statuses = await asyncio.gather(
            self.rest_request("GET", f"{DEVICES_PATH}{device_id}/"),
            self.device_config_status_map([device_id]),
        )
        if not device:
            raise DCIMNotFoundError(f"NetBox device {device_id} not found")
        return network_device_from_rest(device, statuses.get(str(device_id)))

    async def get_network_devices(self, filters: DeviceInventoryFilter) -> list[NetworkDeviceData]:
        batches = await self._device_filter_batches(filters)
        if batches is None:
            return []
        devices = await self._list_device_batches(batches)
        # The *_enabled flags come from DeviceConfigStatus, not the device row.
        statuses = await self.device_config_status_map(str(d["id"]) for d in devices)
        return self._map_unique_by_name(
            devices, lambda d: network_device_from_rest(d, statuses.get(str(d["id"])))
        )

    async def get_host_device(self, device_id: str) -> HostDeviceData:
        data = await self.graphql_query(_HOST_DEVICE_QUERY, {"id": device_id})
        device = data.get("device")
        if not device:
            raise DCIMNotFoundError(f"NetBox device {device_id} not found")
        return host_device_from_gql(device)

    async def get_host_devices(self, filters: DeviceInventoryFilter) -> list[HostDeviceData]:
        batches = await self._device_filter_batches(filters)
        if batches is None:
            return []
        devices = await self._list_device_batches(batches)
        ids = [str(d["id"]) for d in devices]
        return await self._host_devices_by_ids(ids)

    async def get_device_interfaces(self, device_id: str) -> list[InterfaceData]:
        interfaces = await self.rest_list(INTERFACES_PATH, [("device_id", str(device_id))])
        return [interface_from_rest(i) for i in interfaces]

    # -- shared helpers ------------------------------------------------------

    async def _platform_params(self, platforms) -> list[tuple[str, str]] | None:
        """Query params for a platform filter, dropping platforms NetBox lacks.

        ``None`` means every requested platform is absent, so nothing can match;
        callers must turn that into an empty result, never an unfiltered query.

        NetBox ORs a repeated param, so the platforms that do exist still select
        exactly the devices they should. See :meth:`_known_platform_slugs` for why
        the check is needed for platforms and not for site/tenant/role.
        """
        pairs = [slug_or_id("platform", getattr(p, "value", p)) for p in platforms or ()]
        if not pairs:
            return []
        known = await self._known_platform_slugs()
        if not known:
            # Nothing to check against. Forward the filter and let NetBox answer,
            # rather than concluding that nothing matches: an empty set means
            # either a NetBox with no platforms at all -- where a platform filter
            # cannot match anyway -- or a platform_list read that returned nothing,
            # and in the second case failing closed would silently empty the result
            # of every platform-scoped workflow.
            logger.debug("NetBox reported no platforms; forwarding the filter unchecked")
            return pairs
        # An id is not a slug and cannot be checked against this list; pass it
        # through and let NetBox judge it.
        usable = [(key, value) for key, value in pairs if key != "platform" or value in known]
        missing = [value for key, value in pairs if key == "platform" and value not in known]
        if missing:
            logger.info("Ignoring platform(s) NetBox does not define: %r", missing)
        return usable or None

    async def _known_platform_slugs(self) -> set[str]:
        """The platform slugs this NetBox actually defines.

        ``platform`` is a choice field over existing slugs, so an unknown value
        fails the whole device query with HTTP 400. It is the only selection
        filter where that is reachable: site, tenant and role values come from
        our own list endpoints, while ``Platform`` is a fixed SDK enum a workflow
        sends regardless of what the DCIM holds.

        Cached for the client's lifetime -- reference data on every selection.
        """
        if self._platform_slugs is None:
            data = await self.graphql_query("query { platform_list { slug } }")
            self._platform_slugs = {
                row["slug"] for row in (data.get("platform_list") or []) if row.get("slug")
            }
        return self._platform_slugs

    @staticmethod
    def _map_unique_by_name(devices: list[dict], mapper: Callable[[dict], _T]) -> list[_T]:
        """Map device rows, skipping unmappable ones and rejecting duplicate names.

        A device that cannot be mapped -- no platform, or no name (``Device.name``
        is nullable in NetBox) -- must not fail the whole listing; it is logged and
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

    async def _list_device_batches(self, batches: list[list[tuple[str, str]]]) -> list[dict]:
        """Run one ``dcim/devices`` listing per param batch and concatenate them.

        The batches are disjoint by construction (each carries a distinct slice of
        the selected device ids), so no de-duplication is needed here.
        """
        pages = await asyncio.gather(*(self.rest_list(DEVICES_PATH, b) for b in batches))
        return [row for page in pages for row in page]

    async def _device_filter_batches(
        self, filters: DeviceInventoryFilter
    ) -> list[list[tuple[str, str]]] | None:
        """Build ``dcim/devices`` query-param batches, or ``None`` if nothing can match.

        Returns ``None`` when an id-valued filter (MAC addresses, NVCM status flags)
        was requested but selected no device; callers must treat that as an empty
        result, not an unfiltered query. Raises ``DCIMInvalidDataError`` when no
        filter of any kind was supplied, so an empty filter can never degrade into a
        full-fleet listing (parity with the reference provider's guard).

        Id-valued filters are intersected in Python and emitted as one ``id``
        selection: NetBox ORs a repeated param, so appending each separately yields
        their union. Chunked because the selection can be fleet-sized (``ID_CHUNK``).
        """
        params: list[tuple[str, str]] = []
        if filters.site:
            params.append(slug_or_id("site", filters.site))
        if filters.tenant:
            params.append(slug_or_id("tenant", filters.tenant))
        for key, values in (
            ("role", filters.roles),
            ("status", filters.statuses),
            ("device_type_id", filters.device_type_ids),
        ):
            for value in values or ():
                if key == "status":
                    # Slug (lowercase); workflows pass the display label ("Active").
                    params.append((key, status_slug(value)))
                elif key == "device_type_id":
                    params.append((key, str(value)))
                else:
                    params.append(slug_or_id(key, value))
        platform_params = await self._platform_params(filters.platforms)
        if platform_params is None:
            return None
        params.extend(platform_params)

        # Every filter that narrows to a set of device ids, collected for
        # intersection.
        id_sets: list[set[str]] = []
        if filters.device_ids:
            id_sets.append({str(value) for value in filters.device_ids})
        if filters.managed_only:
            id_sets.append(await self.managed_device_ids())
        status_ids = await self.status_flag_device_ids(
            render_enabled=filters.render_enabled,
            deploy_enabled=filters.deploy_enabled,
            backup_enabled=filters.backup_enabled,
            ztp_enabled=filters.ztp_enabled,
        )
        if status_ids is not None:
            id_sets.append(status_ids)
        # MAC filtering on devices is done via the interfaces endpoint in NetBox.
        if filters.mac_addresses:
            id_sets.append(set(await self._device_ids_for_macs(filters.mac_addresses)))

        # Reject an entirely unfiltered request before it becomes a whole-fleet
        # query. An id-valued-only filter (e.g. MAC alone) is legitimate.
        if not params and not id_sets:
            raise DCIMInvalidDataError("At least one device inventory filter must be supplied")
        if not id_sets:
            return [params]
        selected = set.intersection(*id_sets)
        if not selected:
            return None
        return [
            [*params, *(("id", device_id) for device_id in chunk)]
            for chunk in chunked(sorted(selected))
        ]

    async def _interfaces_for_macs(self, mac_addresses: list[str] | None) -> list[dict]:
        """List interfaces carrying any of ``mac_addresses`` (empty if none given)."""
        if not mac_addresses:
            return []
        params = [("mac_address", mac) for mac in mac_addresses]
        return await self.rest_list(INTERFACES_PATH, params)

    async def _device_ids_for_macs(self, mac_addresses: list[str] | None) -> list[str]:
        interfaces = await self._interfaces_for_macs(mac_addresses)
        return sorted({str(i["device"]["id"]) for i in interfaces if i.get("device")})
