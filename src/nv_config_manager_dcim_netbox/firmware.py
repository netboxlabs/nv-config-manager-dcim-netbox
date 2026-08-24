# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Firmware / OS-image operations.

Firmware/OS intent lives in config context: reads use the device's rendered
``config_context`` (plus a ``location-firmware-targets`` profile for the desired
version per role/platform); writes merge into the device's ``local_context_data``.
"""

from __future__ import annotations

from nv_config_manager_dcim import (
    DCIMInvalidDataError,
    DCIMNotFoundError,
    FirmwareBundle,
    OSImageVersions,
)
from nv_config_manager_dcim.models import FirmwareComponent

from nv_config_manager_dcim_netbox._mappers import name_of, slug_of

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

    async def get_os_image_versions(self, device_id: str) -> OSImageVersions:
        device = await self.rest_request("GET", f"dcim/devices/{device_id}/")
        cc = (device or {}).get("config_context") or {}
        intended = (cc.get("intended-firmware") or {}).get("version") or ""
        ztp_ipv4_list = (cc.get("ztp") or {}).get("ipv4") or []
        ztp_ipv4 = ztp_ipv4_list[0] if ztp_ipv4_list else ""
        # Key the firmware-targets table by role + platform *slug* on both sides
        # (NetBox's canonical, stable identifiers) so lookups don't silently miss.
        role = slug_of(device.get("role"))
        platform = slug_of(device.get("platform"))
        # NetBox devices carry a direct site FK; scope the targets table to the
        # device's site so per-site desired versions don't leak across sites.
        site = name_of(device.get("site"))
        desired = ""
        if role and platform:
            targets = await self._firmware_targets(site)
            desired = (targets.get(role) or {}).get(platform) or ""
        # The SDK's OSImageVersions, not a bare tuple: it is a NamedTuple and
        # NVCM reads it by attribute (`versions.intended_firmware` in
        # temporal/ngc/activities/os.py). A plain tuple carries the same three
        # values in the same order and fails only at that attribute access,
        # inside the activity, as an AttributeError with no mention of the DCIM.
        return OSImageVersions(
            intended_firmware=intended, desired_firmware=desired, ztp_address=ztp_ipv4
        )

    async def set_intended_os_image(self, device_id: str, desired_firmware: str) -> None:
        await self._merge_local_context(
            device_id, {"intended-firmware": {"version": desired_firmware}}
        )

    async def get_firmware_bundle(
        self, device_id: str, bundle_version: str | None = None
    ) -> FirmwareBundle:
        device = await self.rest_request("GET", f"dcim/devices/{device_id}/")
        cc = (device or {}).get("config_context") or {}
        bundles = cc.get("firmware_bundles")
        version = bundle_version or cc.get("firmware_bundle_version")
        # Raise on missing data rather than returning a hollow bundle: callers
        # cannot tell an empty bundle from "no bundle assigned" otherwise.
        if not isinstance(bundles, dict) or not version:
            raise DCIMNotFoundError("No firmware bundle is assigned to the device")
        bundle = bundles.get(version)
        if not isinstance(bundle, dict):
            raise DCIMNotFoundError(f"Firmware bundle {version!r} was not found")
        raw_components = bundle.get("firmware")
        if not isinstance(raw_components, dict):
            raise DCIMInvalidDataError("NetBox returned invalid firmware bundle components")
        components = {
            str(name).lower(): FirmwareComponent(
                reported_version=component.get("reported_version")
                if isinstance(component, dict)
                else None,
                file_name=component.get("file") if isinstance(component, dict) else None,
                source_path=component.get("s3_path") if isinstance(component, dict) else None,
            )
            for name, component in raw_components.items()
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

    async def _firmware_targets(self, site: str | None = None) -> dict:
        # Scope the targets config-context to the device's site when known; the
        # callee already treats a falsy site as unscoped.
        contexts = await self.config_contexts_for_profile(_FIRMWARE_TARGETS_PROFILE, site or None)
        if not contexts:
            return {}
        return (contexts[0].get("data") or {}).get("firmware-targets") or {}

    async def _merge_local_context(self, device_id: str, data: dict) -> None:
        # Read-modify-write: NetBox has no conditional update for
        # local_context_data, so concurrent writes to one device could lose an
        # update. NVCM drives these per device, so they do not overlap today.
        device = await self.rest_request("GET", f"dcim/devices/{device_id}/")
        local = (device or {}).get("local_context_data") or {}
        # Deep-merge so nested objects (e.g. ``intended-firmware``) keep their
        # sibling keys instead of being wholesale replaced.
        _deep_merge(local, data)
        await self.rest_request(
            "PATCH", f"dcim/devices/{device_id}/", json={"local_context_data": local}
        )
