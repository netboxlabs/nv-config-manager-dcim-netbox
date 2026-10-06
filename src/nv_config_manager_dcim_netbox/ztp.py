# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""ZTP operations (native NetBox ``dcim`` + config context)."""

from __future__ import annotations

import asyncio

from nv_config_manager_dcim import DCIMInvalidDataError, DCIMNotFoundError, ZTPDevice

from nv_config_manager_dcim_netbox._mappers import require_device_name, strip_mask
from nv_config_manager_dcim_netbox.fabric_common import INTENDED_PATH as _INTENDED_PATH
from nv_config_manager_dcim_netbox.fabric_common import STATUS_PATH as _STATUS_PATH

# ``_INTENDED_PATH`` holds the per-device intended-config records written by the
# render pipeline; each carries the config_store_instance the device's rendered
# files live under.

# NetBox has no "provisioned" device status out of the box (stock statuses:
# offline, active, planned, staged, failed, inventory, decommissioning). NVCM
# adds one via netbox_nv_config_manager's FIELD_CHOICES (an extensible
# `dcim.Device.status+` choice). Writing "provisioned" makes the device status
# field a real ZTP signal -- distinct from the pre-ZTP "provisioning"/"active"
# state -- instead of every device looking "active" forever. This REQUIRES the
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
        # ZTP serves managed devices only: like the reference's
        # ``config_manager_device`` lookup, a device with no DeviceConfigStatus
        # record is "not found" here even though the device exists.
        data, status, intended = await asyncio.gather(
            self.graphql_query(_ZTP_DEVICE_QUERY, {"id": device_id}),
            self.rest_list(_STATUS_PATH, [("device_id", str(device_id))]),
            self.rest_list(_INTENDED_PATH, [("device_id", str(device_id))]),
        )
        device = data.get("device")
        if not device or not status:
            raise DCIMNotFoundError(f"No managed NetBox device {device_id} for ZTP")
        platform_name = (device.get("platform") or {}).get("name")
        if not platform_name:
            raise DCIMInvalidDataError(f"NetBox device {device_id} has no platform")
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
            # The render pipeline records the config-store instance on the
            # intended-config; passed through verbatim, as the reference does.
            # ZTP's load_file only requires it non-None.
            config_store_instance = intended[0].get("config_store_instance") if intended else None
            return ZTPDevice(
                # Keys the config-store lookup for the boot-script/config files.
                device_id=str(device["id"]),
                # ZTPDevice.name is a required str and Device.name is nullable in
                # NetBox; the pydantic error for None is a ValueError, which the
                # handler below does not catch.
                name=require_device_name(device),
                addresses=addresses,
                platform_name=platform_name,
                firmware_version=firmware,
                config_store_instance=config_store_instance,
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
