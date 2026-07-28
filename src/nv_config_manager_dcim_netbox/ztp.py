# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""ZTP operations (native NetBox ``dcim`` + config context)."""

from __future__ import annotations

from nv_config_manager_dcim import DCIMInvalidDataError, DCIMNotFoundError, ZTPDevice

from nv_config_manager_dcim_netbox._mappers import strip_mask

# NetBox has no "provisioned" status by default. Stock
# NetBox statuses are: offline, active, planned, staged, failed, inventory,
# decommissioning. "active" is the closest post-provisioning state; make this a
# configured status if a dedicated "provisioned" choice is added.
ZTP_PROVISIONED_STATUS = "active"

_ZTP_DEVICE_QUERY = """
query ($id: ID!) {
  device(id: $id) {
    id
    name
    platform { name }
    config_context
    interfaces {
      ip_addresses { address }
    }
  }
}
"""


class NetBoxZTPMixin:
    """Zero-touch-provisioning device reads and status writes."""

    async def get_ztp_device(self, device_id: str) -> ZTPDevice:
        data = await self.graphql_query(_ZTP_DEVICE_QUERY, {"id": device_id})
        device = data.get("device")
        if not device:
            raise DCIMNotFoundError(f"NetBox device {device_id} not found")
        try:
            addresses = sorted(
                {
                    host
                    for interface in device.get("interfaces") or []
                    for ip in interface.get("ip_addresses") or []
                    if (host := strip_mask(ip.get("address")))
                }
            )
            config_context = device.get("config_context") or {}
            firmware = (config_context.get("intended-firmware") or {}).get("version")
            return ZTPDevice(
                device_id=str(device["id"]),
                name=device["name"],
                addresses=addresses,
                platform_name=(device.get("platform") or {}).get("name") or "",
                firmware_version=firmware,
                # config_store_instance comes from NVCM intended-config state
                # (a plugin/custom-field addition); unavailable natively.
                config_store_instance=None,
            )
        except (KeyError, TypeError) as exc:
            raise DCIMInvalidDataError(
                f"Malformed NetBox ZTP data for device {device_id}: {exc}"
            ) from exc

    async def mark_ztp_device_provisioned(self, device_id: str) -> None:
        await self.rest_request(
            "PATCH",
            f"dcim/devices/{device_id}/",
            json={"status": ZTP_PROVISIONED_STATUS},
        )
