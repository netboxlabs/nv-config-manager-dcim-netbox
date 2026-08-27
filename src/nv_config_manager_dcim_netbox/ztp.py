# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""ZTP operations (native NetBox ``dcim`` + config context)."""

from __future__ import annotations

import logging
import re

from nv_config_manager_dcim import DCIMInvalidDataError, DCIMNotFoundError, ZTPDevice

from nv_config_manager_dcim_netbox._mappers import require_device_name, strip_mask
from nv_config_manager_dcim_netbox.fabric_common import INTENDED_PATH as _INTENDED_PATH

logger = logging.getLogger(__name__)

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

# The config store's mTLS API is the same host as its UI with the leading `ui`
# label swapped for `api-mtls`. Anchored on that label: an unanchored replace also
# rewrites `ui` inside later labels, producing a host that does not resolve.
_UI_LABEL_RE = re.compile(r"(?<=//)ui(?=\.)")
_MTLS_LABEL = "api-mtls"


def _mtls_endpoint(config_store_instance: str) -> str:
    """Return the mTLS API endpoint for a config-store UI URL."""
    endpoint, replaced = _UI_LABEL_RE.subn(_MTLS_LABEL, config_store_instance, count=1)
    if not replaced:
        logger.warning(
            "config_store_instance %r has no leading 'ui' host label; ZTP will use it "
            "unchanged, which may be the browser UI rather than the mTLS API",
            config_store_instance,
        )
    return endpoint


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
            # The render pipeline records the config-store instance on the
            # intended-config. ZTP's load_file requires it non-None, and uses it as
            # the endpoint for external mTLS.
            config_store_instance = None
            intended = await self.rest_list(_INTENDED_PATH, [("device_id", str(device_id))])
            if intended and intended[0].get("config_store_instance"):
                config_store_instance = _mtls_endpoint(intended[0]["config_store_instance"])
            return ZTPDevice(
                # Keys the config-store lookup for the boot-script/config files.
                device_id=str(device["id"]),
                # ZTPDevice.name is a required str and Device.name is nullable in
                # NetBox; the pydantic error for None is a ValueError, which the
                # handler below does not catch.
                name=require_device_name(device),
                addresses=addresses,
                platform_name=(device.get("platform") or {}).get("name") or "",
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
