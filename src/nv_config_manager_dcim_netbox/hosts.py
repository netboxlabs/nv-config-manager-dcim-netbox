# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Host / MAC correlation operations (native NetBox interfaces + MAC)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from nv_config_manager_dcim import DCIMNotFoundError

from nv_config_manager_dcim_netbox._mappers import (
    canonical_mac,
    host_metadata_from_gql,
    interface_from_rest,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from nv_config_manager_dcim import HostMetadata
    from nv_config_manager_dcim.workflow_models import (
        HostDeviceData,
        InterfaceData,
        NetworkDeviceData,
    )

_CONNECTED_PORT_QUERY = """
query ($mac: String!) {
  mac_address_list(filters: {mac_address: {i_exact: $mac}}) {
    assigned_object {
      ... on InterfaceType {
        connected_endpoints {
          ... on InterfaceType {
            name
            device { id }
          }
        }
      }
    }
  }
}
"""

_HOST_METADATA_FIELDS = """
    id
    name
    tenant { name }
    custom_fields
    interfaces {
      name
      primary_mac_address { mac_address }
      mac_addresses { mac_address }
    }
"""

_HOST_METADATA_BY_NAMES_QUERY = f"""
query ($names: [String!]!) {{
  device_list(filters: {{name: {{in_list: $names}}}}) {{{_HOST_METADATA_FIELDS}}}
}}
"""

_HOST_METADATA_BY_IDS_QUERY = f"""
query ($ids: [ID!]!) {{
  device_list(filters: {{id: {{in_list: $ids}}}}) {{{_HOST_METADATA_FIELDS}}}
}}
"""


class NetBoxHostsMixin:
    """Correlate hosts, interfaces, and switch ports by MAC address."""

    async def find_host_devices_by_mac(self, mac_address: str) -> list[HostDeviceData]:
        ids = await self._device_ids_for_macs([mac_address])
        return await self._host_devices_by_ids(ids)

    async def update_dpu_device_inventory(
        self, device_id: str, serial: str, interface_macs: Mapping[str, str]
    ) -> HostDeviceData:
        """Update a DPU's serial and interface MACs, returning the refreshed host.

        In NetBox 4.x a MAC is a first-class ``MACAddress`` object assigned to an
        interface, not a scalar field. Each interface MAC is therefore upserted
        (reused if already present on the interface) and set as the interface's
        ``primary_mac_address``.

        NOTE: this is several writes with no transaction -- NetBox's REST API offers
        none across objects. A failure part-way leaves the serial applied and some
        MACs unset; the operation is idempotent, so the fix is to re-run it.
        """
        await self.rest_request("PATCH", f"dcim/devices/{device_id}/", json={"serial": serial})
        # Each interface's upsert is independent (2-3 round trips apiece), so run
        # them concurrently rather than serially per interface.
        await asyncio.gather(
            *(
                self._set_interface_primary_mac(str(interface_id), mac_address)
                for interface_id, mac_address in interface_macs.items()
            )
        )
        return await self.get_host_device(str(device_id))

    async def _set_interface_primary_mac(self, interface_id: str, mac_address: str) -> None:
        """Upsert ``mac_address`` on the interface and make it the primary MAC.

        Matching is canonical (case- and separator-insensitive) so an existing
        MAC recorded in any format is reused rather than duplicated; the value
        posted for a new MAC is preserved exactly as supplied.
        """
        normalized = canonical_mac(mac_address)
        existing = await self.rest_list("dcim/mac-addresses/", [("interface_id", interface_id)])
        match = next(
            (m for m in existing if canonical_mac(m.get("mac_address")) == normalized),
            None,
        )
        if match:
            mac_id = match["id"]
        else:
            created = await self.rest_request(
                "POST",
                "dcim/mac-addresses/",
                json={
                    "mac_address": mac_address,
                    "assigned_object_type": "dcim.interface",
                    "assigned_object_id": int(interface_id),
                },
            )
            mac_id = created["id"]
        await self.rest_request(
            "PATCH", f"dcim/interfaces/{interface_id}/", json={"primary_mac_address": mac_id}
        )

    async def get_interface_hosts_by_mac(self, mac_addresses: list[str]) -> list[InterfaceData]:
        interfaces = await self._interfaces_for_macs(mac_addresses)
        return [interface_from_rest(i) for i in interfaces]

    async def get_host_metadata_by_macs(self, mac_addresses: list[str]) -> list[HostMetadata]:
        ids = await self._device_ids_for_macs(mac_addresses)
        if not ids:
            return []
        data = await self.graphql_query(_HOST_METADATA_BY_IDS_QUERY, {"ids": ids})
        return [host_metadata_from_gql(d) for d in data.get("device_list") or []]

    async def get_host_metadata_by_names(self, device_names: list[str]) -> list[HostMetadata]:
        if not device_names:
            return []
        data = await self.graphql_query(
            _HOST_METADATA_BY_NAMES_QUERY, {"names": list(device_names)}
        )
        return [host_metadata_from_gql(d) for d in data.get("device_list") or []]

    async def get_connected_switch_port_by_remote_mac(
        self, mac_address: str
    ) -> tuple[NetworkDeviceData, str]:
        data = await self.graphql_query(_CONNECTED_PORT_QUERY, {"mac": mac_address})
        for entry in data.get("mac_address_list") or []:
            assigned = entry.get("assigned_object") or {}
            for endpoint in assigned.get("connected_endpoints") or []:
                device = endpoint.get("device") or {}
                if device.get("id") and endpoint.get("name"):
                    switch = await self.get_network_device(str(device["id"]))
                    return switch, endpoint["name"]
        raise DCIMNotFoundError(f"No connected switch port found for MAC {mac_address}")
