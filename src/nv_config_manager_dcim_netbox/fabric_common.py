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

import asyncio
import re
from collections.abc import Iterable, Iterator

from nv_config_manager_dcim import (
    DCIMConflictError,
    DCIMInvalidDataError,
    DCIMNotFoundError,
)

from nv_config_manager_dcim_netbox._mappers import device_pk, fk_id

# Default fabric status value (plugin uses a choice field, not a status object).
FABRIC_STATUS = "active"

# Companion-plugin (netbox_nv_config_manager) REST paths.
STATUS_PATH = "plugins/nv-config-manager/device-config-statuses/"
INTENDED_PATH = "plugins/nv-config-manager/intended-configs/"
BACKUP_PATH = "plugins/nv-config-manager/backup-configs/"
NVLINK_DOMAINS_PATH = "plugins/nv-config-manager/nvlink-domains/"

# Core NetBox paths shared across more than one mixin.
DEVICES_PATH = "dcim/devices/"
VRFS_PATH = "ipam/vrfs/"
VLANS_PATH = "ipam/vlans/"
PREFIXES_PATH = "ipam/prefixes/"
INTERFACES_PATH = "dcim/interfaces/"
IP_ADDRESSES_PATH = "ipam/ip-addresses/"

# netbox-bgp exposes no GraphQL, so BGP is read over REST.
BGP_SESSIONS_PATH = "plugins/bgp/session/"
# Peer groups are fetched separately: a session nests only a brief peer_group
# (id/name/description/url), without the extra_attributes carrying the TTL.
BGP_PEER_GROUPS_PATH = "plugins/bgp/peer-group/"

_PKEY_RE = re.compile(r"\A0[xX]([0-9a-fA-F]{1,4})\Z")

# Device ids per repeated-``id`` (or ``device_id``) query. Repeating the param for
# a whole fleet builds a query string long enough for a proxy to reject with 414,
# so the reads that filter by many ids walk the fleet in chunks.
ID_CHUNK = 100


def chunked(values: list[str], size: int = ID_CHUNK) -> Iterator[list[str]]:
    """Yield ``values`` in fixed-size chunks."""
    for start in range(0, len(values), size):
        yield values[start : start + size]


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

    # -- NVCM per-device management flags ------------------------------------
    #
    # The flags live on the plugin's DeviceConfigStatus model, not on device custom
    # fields. Never query them as cf_nvcm_* filters on dcim/devices: no such field
    # exists, and NetBox drops unknown params rather than rejecting them, so the
    # filter widens the read to the whole fleet instead of failing closed.

    async def status_flag_device_ids(self, **flags: bool | None) -> set[str] | None:
        """Return the device PKs whose ``DeviceConfigStatus`` matches every flag.

        ``flags`` are ``DeviceConfigStatus`` field names; a ``None`` value is
        dropped. Returns ``None`` when nothing was constrained (so callers can tell
        "no filter" from "no match"), else the matching ids.
        """
        params = [
            (field, str(value).lower()) for field, value in flags.items() if value is not None
        ]
        if not params:
            return None
        rows = await self.rest_list(STATUS_PATH, params)
        return {pk for row in rows if (pk := device_pk(row))}

    async def device_config_status_map(self, device_ids: Iterable[str]) -> dict[str, dict]:
        """Return ``{device_pk: DeviceConfigStatus row}``; devices with none are absent.

        Populates the ``*_enabled`` flags on the device workflow models.
        """
        ids = sorted({str(i) for i in device_ids})
        if not ids:
            return {}
        pages = await asyncio.gather(
            *(
                self.rest_list(STATUS_PATH, [("device_id", i) for i in chunk])
                for chunk in chunked(ids)
            )
        )
        return {pk: row for page in pages for row in page if (pk := device_pk(row))}

    async def devices_by_ids(
        self,
        device_ids: Iterable[str],
        extra_params: list[tuple[str, str]] | None = None,
        *,
        page_size: int = 250,
    ) -> list[dict]:
        """Fetch ``dcim/devices`` rows for ``device_ids``, ANDed with ``extra_params``.

        Empty ``device_ids`` returns nothing, never an unfiltered listing. Chunked
        because the id set can be fleet-sized (see ``ID_CHUNK``).
        """
        ids = sorted({str(i) for i in device_ids})
        if not ids:
            return []
        base = list(extra_params or [])
        pages = await asyncio.gather(
            *(
                self.rest_list(DEVICES_PATH, base + [("id", i) for i in chunk], page_size=page_size)
                for chunk in chunked(ids)
            )
        )
        return [row for page in pages for row in page]

    async def _find_overlay(self, name: str, scope: str | None = None) -> dict | None:
        params = [("name", name)]
        if scope is not None:
            scope_type, scope_id = await self._resolve_scope(scope)
            if scope_id is None:
                # No NetBox Site/Location by that name -> no overlay can match it.
                return None
            # scope_type as well as scope_id: the overlay scope is a generic FK, and
            # Site and Location pks are independent sequences, so scope_id=5 alone
            # matches a Location-scoped overlay when the caller resolved Site 5.
            params.append(("scope_id", str(scope_id)))
            if scope_type:
                params.append(("scope_type", scope_type))
        rows = await self.rest_list(self.OVERLAYS_PATH, params)
        if len(rows) > 1:
            raise DCIMConflictError(f"Multiple overlays named {name!r}")
        return rows[0] if rows else None

    async def _resolve_scope(self, value: str | int | None) -> tuple[str | None, int | None]:
        """Resolve an overlay anchor to ``(scope_type, scope_id)`` for the plugin's
        ``CachedScopeMixin`` scope GFK.

        NVCM passes a single site/location value; the Overlay scope may be a
        ``dcim.site`` or a ``dcim.location`` (SpectrumX anchors at a Site,
        InfiniBand at a sub-Site Location). Resolve by trying a Site first, then a
        Location, matching either the slug or the name — so the same value works
        whether the caller passes the site slug (what ``cf_nvcm_site`` stores) or a
        Location name. Returns ``(None, None)`` when unresolved.
        """
        if value is None or value == "":
            return None, None
        val = str(value)
        candidates = (("dcim/sites/", "dcim.site"), ("dcim/locations/", "dcim.location"))
        for path, scope_type in candidates:
            for key in ("slug", "name"):
                rows = await self.rest_list(path, [(key, val)])
                if len(rows) > 1:
                    raise DCIMConflictError(f"Multiple {path} entries with {key}={val!r}")
                if rows:
                    return scope_type, rows[0]["id"]
        return None, None

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
