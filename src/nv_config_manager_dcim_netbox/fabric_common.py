# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Shared helpers for the fabric (overlay) plugin endpoints.

Mixed into the composed client so the Spectrum-X and InfiniBand mixins share
overlay lookup, id resolution, and the plugin REST paths.

This module is also the single home for the companion plugin's REST paths — the
lifecycle-state, ZTP, and DHCP mixins import them from here rather than each
redeclaring the same string.
"""

from __future__ import annotations

import re

from nv_config_manager_dcim import (
    DCIMConflictError,
    DCIMInvalidDataError,
    DCIMNotFoundError,
)

from nv_config_manager_dcim_netbox._mappers import fk_id

# Default fabric status value (plugin uses a choice field, not a status object).
FABRIC_STATUS = "active"

# Companion-plugin (netbox_nv_config_manager) REST paths.
STATUS_PATH = "plugins/nv-config-manager/device-config-statuses/"
INTENDED_PATH = "plugins/nv-config-manager/intended-configs/"
BACKUP_PATH = "plugins/nv-config-manager/backup-configs/"
NVLINK_DOMAINS_PATH = "plugins/nv-config-manager/nvlink-domains/"

# Core NetBox paths shared across more than one mixin.
VRFS_PATH = "ipam/vrfs/"
VLANS_PATH = "ipam/vlans/"
PREFIXES_PATH = "ipam/prefixes/"
INTERFACES_PATH = "dcim/interfaces/"

# netbox-bgp exposes no GraphQL, so BGP is read over REST.
BGP_SESSIONS_PATH = "plugins/bgp/session/"

_PKEY_RE = re.compile(r"\A0[xX]([0-9a-fA-F]{1,4})\Z")


def normalize_pkey(value: str) -> str:
    """Return a pkey in canonical ``0x%04x`` form.

    Raises :class:`DCIMInvalidDataError` when *value* is empty or does not match
    the required ``0x`` + 1-4 hex-digit format. The reference DCIM provider
    rejects malformed PKeys up front rather than silently passing them through,
    which otherwise lets bad input propagate into overlay/pkey records.
    """
    match = _PKEY_RE.match(value.strip()) if value else None
    if not match:
        raise DCIMInvalidDataError(f"pkey {value!r} does not match required format (e.g. '0x8001')")
    return f"0x{int(match.group(1), 16):04x}"


class FabricCommonMixin:
    OVERLAYS_PATH = "plugins/nv-config-manager/overlays/"
    VXLANS_PATH = "plugins/nv-config-manager/vxlans/"
    ASSIGNMENTS_PATH = "plugins/nv-config-manager/overlay-assignments/"
    PKEYS_PATH = "plugins/nv-config-manager/ib-pkeys/"

    async def _find_overlay(self, name: str, location: str | None = None) -> dict | None:
        params = [("name", name)]
        if location is not None:
            location_id = await self._resolve_location_id(location)
            if location_id is None:
                # No NetBox Location by that name -> no overlay can match it.
                return None
            params.append(("location_id", str(location_id)))
        rows = await self.rest_list(self.OVERLAYS_PATH, params)
        if len(rows) > 1:
            raise DCIMConflictError(f"Multiple overlays named {name!r}")
        return rows[0] if rows else None

    async def _resolve_location_id(self, location: str | int | None) -> int | None:
        """Resolve a NetBox Location given as a PK or a name to its integer id.

        NVCM passes a site/location *name*; NetBox's ``location_id`` filter and the
        Overlay ``location`` FK need a numeric PK. A digit is used as-is; a name is
        looked up against ``dcim/locations/``. Returns ``None`` when unresolved.
        """
        if location is None:
            return None
        if str(location).isdigit():
            return int(location)
        return await self._lookup_id("dcim/locations/", str(location))

    async def _lookup_id(self, path: str, name: str) -> int | None:
        rows = await self.rest_list(path, [("name", name)])
        if len(rows) > 1:
            raise DCIMConflictError(f"Multiple {path} entries named {name!r}")
        return rows[0]["id"] if rows else None

    async def _resolve_tenant_id(self, tenant: str | None) -> int | None:
        if not tenant:
            return None
        if str(tenant).isdigit():
            return int(tenant)
        return await self._lookup_id("tenancy/tenants/", tenant)

    # Kept as a method for the mixins' ``self._fk_id(...)`` call sites; the
    # implementation is the shared one in ``_mappers``.
    _fk_id = staticmethod(fk_id)

    async def _create_overlay_assignment(
        self, overlay_id, object_type: str, object_id, **extra
    ) -> dict:
        """Create one overlay assignment; ``extra`` carries per-caller fields.

        Shared by the Spectrum-X (device/interface/VRF) and InfiniBand (interface
        + ``guid``/``membership_type``) assignment paths.
        """
        return await self.rest_request(
            "POST",
            self.ASSIGNMENTS_PATH,
            json={
                "overlay": overlay_id,
                "assigned_object_type": object_type,
                "assigned_object_id": int(object_id),
                "status": FABRIC_STATUS,
                **extra,
            },
        )

    async def _safe_delete(self, path: str) -> None:
        try:
            await self.rest_request("DELETE", path)
        except DCIMNotFoundError:
            pass
