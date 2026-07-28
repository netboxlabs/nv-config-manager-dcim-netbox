# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Firmware / OS-image operations.

Firmware/OS intent lives in config context: reads use the device's rendered
``config_context`` (plus a ``location-firmware-targets`` profile for the desired
version per role/platform); writes merge into the device's ``local_context_data``.
"""

from __future__ import annotations

from nv_config_manager_dcim import FirmwareBundle
from nv_config_manager_dcim.models import FirmwareComponent

_FIRMWARE_TARGETS_PROFILE = "location-firmware-targets"


def _deep_merge(base: dict, updates: dict) -> None:
    """Recursively merge ``updates`` into ``base`` in place.

    Nested dicts are merged key-by-key so sibling keys are preserved; every
    other value (scalars, lists, non-dict types) overwrites.
    """
    for key, value in updates.items():
        existing = base.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            _deep_merge(existing, value)
        else:
            base[key] = value


class NetBoxFirmwareMixin:
    """OS image version reads and firmware-intent writes."""

    async def get_os_image_versions(self, device_id: str) -> tuple[str, str, str]:
        device = await self.rest_request("GET", f"dcim/devices/{device_id}/")
        cc = (device or {}).get("config_context") or {}
        intended = (cc.get("intended-firmware") or {}).get("version") or ""
        ztp_ipv4_list = (cc.get("ztp") or {}).get("ipv4") or []
        ztp_ipv4 = ztp_ipv4_list[0] if ztp_ipv4_list else ""
        role = (
            (device.get("role") or {}).get("slug") if isinstance(device.get("role"), dict) else None
        )
        platform = (
            (device.get("platform") or {}).get("slug")
            if isinstance(device.get("platform"), dict)
            else None
        )
        desired = ""
        targets = await self._firmware_targets()
        if role and platform:
            desired = (targets.get(role) or {}).get(platform) or ""
        return intended, desired, ztp_ipv4

    async def set_intended_os_image(self, device_id: str, desired_firmware: str) -> None:
        await self._merge_local_context(
            device_id, {"intended-firmware": {"version": desired_firmware}}
        )

    async def get_firmware_bundle(
        self, device_id: str, bundle_version: str | None = None
    ) -> FirmwareBundle:
        device = await self.rest_request("GET", f"dcim/devices/{device_id}/")
        cc = (device or {}).get("config_context") or {}
        bundles = cc.get("firmware_bundles") or {}
        version = bundle_version or cc.get("firmware_bundle_version") or ""
        bundle = bundles.get(version) or {}
        components = {
            str(name).lower(): FirmwareComponent(
                reported_version=component.get("reported_version"),
                file_name=component.get("file"),
                source_path=component.get("s3_path"),
            )
            for name, component in (bundle.get("firmware") or {}).items()
        }
        desired_os_version = (bundle.get("nv_os") or {}).get("version") or ""
        return FirmwareBundle(
            version=version, desired_os_version=desired_os_version, components=components
        )

    async def set_device_firmware_intent(
        self, device_id: str, bundle_version: str, desired_os_version: str
    ) -> None:
        await self._merge_local_context(
            device_id,
            {
                "firmware_bundle_version": bundle_version,
                "intended-firmware": {"version": desired_os_version},
            },
        )

    # -- helpers -------------------------------------------------------------

    async def _firmware_targets(self) -> dict:
        contexts = await self.config_contexts_for_profile(_FIRMWARE_TARGETS_PROFILE)
        if not contexts:
            return {}
        return (contexts[0].get("data") or {}).get("firmware-targets") or {}

    async def _merge_local_context(self, device_id: str, data: dict) -> None:
        device = await self.rest_request("GET", f"dcim/devices/{device_id}/")
        local = (device or {}).get("local_context_data") or {}
        # Deep-merge so nested objects (e.g. ``intended-firmware``) keep their
        # sibling keys instead of being wholesale replaced.
        _deep_merge(local, data)
        await self.rest_request(
            "PATCH", f"dcim/devices/{device_id}/", json={"local_context_data": local}
        )
