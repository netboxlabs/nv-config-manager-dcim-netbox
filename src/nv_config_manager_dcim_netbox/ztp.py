# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""ZTP operations (native NetBox ``dcim`` + config context)."""

from __future__ import annotations

from nv_config_manager_dcim import DCIMInvalidDataError, DCIMNotFoundError, ZTPDevice

from nv_config_manager_dcim_netbox._mappers import strip_mask

# NetBox has no "provisioned" device status out of the box (stock statuses:
# offline, active, planned, staged, failed, inventory, decommissioning). NVCM
# adds one via netbox_nv_config_manager's FIELD_CHOICES (an extensible
# `dcim.Device.status+` choice). Writing "provisioned" makes the device status
# field a real ZTP signal — distinct from the pre-ZTP "provisioning"/"active"
# state — instead of every device looking "active" forever. This REQUIRES the
# target NetBox to register that choice (see the plugin's field_choices.py); a
# NetBox without it will reject the status write with a 400.
ZTP_PROVISIONED_STATUS = "provisioned"

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
