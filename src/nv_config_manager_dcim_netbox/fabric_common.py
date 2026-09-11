# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Shared helpers for the fabric (overlay) plugin endpoints.

Mixed into the composed client so the Spectrum-X and InfiniBand mixins share
overlay lookup, id resolution, and the plugin REST paths.

This module is also the single home for the companion plugin's REST paths -- the
lifecycle-state, ZTP, and DHCP mixins import them from here rather than each
redeclaring the same string.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterable, Iterator

from nv_config_manager_dcim import (
    DCIMConflictError,
    DCIMInvalidDataError,
    DCIMLocationIdentifier,
    DCIMLocationReference,
    DCIMNotFoundError,
    dcim_location_id,
    dcim_location_type,
)

from nv_config_manager_dcim_netbox._mappers import device_pk, fk_id
from nv_config_manager_dcim_netbox.client import is_netbox_pk

logger = logging.getLogger(__name__)

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

# The four models an Overlay's ``CachedScopeMixin`` scope GFK can point at
# (dcim.LOCATION_SCOPE_TYPES), and where to look each one up. SpectrumX anchors
# at a Site and InfiniBand at a Location, but the plugin's form accepts any of
# the four, so a read must be able to resolve all of them.
REGION_SCOPE_TYPE = "dcim.region"
SITEGROUP_SCOPE_TYPE = "dcim.sitegroup"
SITE_SCOPE_TYPE = "dcim.site"
LOCATION_SCOPE_TYPE = "dcim.location"
SCOPE_PATHS = {
    REGION_SCOPE_TYPE: "dcim/regions/",
    SITEGROUP_SCOPE_TYPE: "dcim/site-groups/",
    SITE_SCOPE_TYPE: "dcim/sites/",
    LOCATION_SCOPE_TYPE: "dcim/locations/",
}

# SDK ``location_type`` -> the NetBox model holding that id. Only the levels with
# a model of their own are named; every other type (Module, Rack, Pod, Row, ...)
# is a NetBox Location distinguished by its ``nvcm_location_type`` custom field.
# ``Provider`` is a Region here: the topology loader maps that source level onto
# one, and Regions carry no type custom field to tell them apart afterwards.
_LOCATION_TYPE_SCOPES = {
    "region": REGION_SCOPE_TYPE,
    "provider": REGION_SCOPE_TYPE,
    "sitegroup": SITEGROUP_SCOPE_TYPE,
    "site_group": SITEGROUP_SCOPE_TYPE,
    "site": SITE_SCOPE_TYPE,
}

# Tried in turn when a location arrives with no type. Sites first: that is what
# NVCM's own /parameter/site endpoint hands out.
_UNTYPED_SCOPE_ORDER = (
    SITE_SCOPE_TYPE,
    LOCATION_SCOPE_TYPE,
    REGION_SCOPE_TYPE,
    SITEGROUP_SCOPE_TYPE,
)


def scope_type_for(location_type: str | None) -> str | None:
    """Return the NetBox scope type an SDK ``location_type`` names, if any."""
    if not location_type:
        return None
    key = location_type.strip().lower().replace(" ", "_")
    return _LOCATION_TYPE_SCOPES.get(key, LOCATION_SCOPE_TYPE)


# netbox-bgp exposes no GraphQL, so BGP is read over REST.
BGP_SESSIONS_PATH = "plugins/bgp/session/"
# Peer groups are fetched separately: a session nests only a brief peer_group
# (id/name/description/url), without the extra_attributes carrying the TTL.
BGP_PEER_GROUPS_PATH = "plugins/bgp/peer-group/"
# The device's own ASN, as an ipam.asn reference. NetBox has no device ASN field,
# and the loader's other copy (config_context.bgp.asn) is computed and therefore
# unfilterable, so this is what resolves an ASN back to the devices using it.
BGP_ASN_FIELD = "nvcm_bgp_asn"

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

    @property
    def _scope_cache(self) -> dict[tuple[str, str], dict | None]:
        """Resolved Site/Location rows, for this client's lifetime.

        A scope is resolved several times per operation -- once for `scope_id`,
        again for the site slug -- and Sites and Locations do not move underneath
        a running workflow.
        """
        cache = getattr(self, "_scope_cache_store", None)
        if cache is None:
            cache = {}
            self._scope_cache_store = cache
        return cache

    # -- NVCM per-device management flags ------------------------------------
    #
    # The flags live on the plugin's DeviceConfigStatus model, not on device custom
    # fields. Never query them as cf_nvcm_* filters on dcim/devices: no such field
    # exists, and NetBox drops unknown params rather than rejecting them, so the
    # filter widens the read to the whole fleet instead of failing closed.

    # -- reads that tolerate a NetBox missing the thing being filtered on -----

    async def _safe_list(self, path: str, params: list[tuple[str, str]] | None = None):
        """List a REST endpoint, treating a missing endpoint (optional plugin) as no data."""
        try:
            return await self.rest_list(path, params)
        except DCIMNotFoundError:
            return []

    async def _optional_list(self, path: str, params: list[tuple[str, str]] | None = None):
        """List an endpoint whose *filter values* may not exist in this NetBox.

        Like ``_safe_list``, but also degrades on 400: NetBox validates slug-keyed
        filters (``tag``, ``role``, ``platform``, ``status``) against what exists,
        so an undefined value fails the whole query instead of matching nothing.
        """
        try:
            return await self.rest_list(path, params)
        except DCIMNotFoundError:
            return []
        except DCIMInvalidDataError as exc:
            logger.warning(
                "NetBox rejected the optional %s selection %s (%s); treating it as empty",
                path,
                params,
                exc,
            )
            return []

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

    async def _find_overlay(
        self, name: str, scope: DCIMLocationIdentifier | None = None, *, scope_type: str
    ) -> dict | None:
        params = [("name", name)]
        if scope is not None:
            row = await self._resolve_scope(scope, scope_type=scope_type)
            if row is None:
                # No such Site/Location -> no overlay can be scoped to it.
                return None
            # scope_type as well as scope_id: the overlay scope is a generic FK, and
            # Site and Location pks are independent sequences, so scope_id=5 alone
            # matches a Location-scoped overlay when the caller meant Site 5.
            params.append(("scope_id", str(row["id"])))
            # The row may have resolved in a different model than the caller's
            # default when the value carried its own type.
            params.append(("scope_type", scope_type_for(dcim_location_type(scope)) or scope_type))
        rows = await self.rest_list(self.OVERLAYS_PATH, params)
        if len(rows) > 1:
            raise DCIMConflictError(f"Multiple overlays named {name!r}")
        return rows[0] if rows else None

    async def _resolve_scope(
        self, value: DCIMLocationIdentifier | int | None, *, scope_type: str
    ) -> dict | None:
        """Resolve a selection value to the NetBox row it names.

        *scope_type* picks the model. A typed ``DCIMLocationReference`` overrides
        it: the caller's default says which model the operation expects, while the
        reference says which one the id actually came from, and PKs are per-model
        so guessing wrong silently matches the wrong object.

        The pk is the contract -- NVCM round-trips the id ``list_locations`` gave
        out, and NetBox anchors the Overlay on a GFK, so it is resolved to a row
        here. Slug and name are also accepted, for a Location named rather than
        selected.

        Returns the row, so one lookup serves both ``scope_id`` and ``cf_nvcm_site``.
        """
        if value is None or value == "":
            return None
        scope_type = scope_type_for(dcim_location_type(value)) or scope_type
        value = dcim_location_id(value) if isinstance(value, DCIMLocationReference) else value
        try:
            path = SCOPE_PATHS[scope_type]
        except KeyError:
            raise DCIMInvalidDataError(f"Unknown overlay scope type {scope_type!r}") from None
        val = str(value)
        cache_key = (scope_type, val)
        if cache_key in self._scope_cache:
            return self._scope_cache[cache_key]

        # Only a canonical PK is worth an id lookup; NetBox rejects a non-numeric
        # `id` outright rather than simply not matching it.
        keys = ("id", "slug", "name") if is_netbox_pk(val) else ("slug", "name")
        # Concurrently: the lookups are independent, and the serial version cost
        # three round-trips for a value matched by the last key tried.
        pages = await asyncio.gather(*(self.rest_list(path, [(key, val)]) for key in keys))
        found = None
        for key, rows in zip(keys, pages, strict=True):
            if len(rows) > 1:
                raise DCIMConflictError(f"Multiple {path} entries with {key}={val!r}")
            if rows and found is None:
                found = rows[0]
        self._scope_cache[cache_key] = found
        return found

    async def _site_field_value(self, site: DCIMLocationIdentifier) -> str:
        """Normalize a site selection to what ``cf_nvcm_site`` stores: the Site pk.

        NVCM sends a pk, name or slug depending on the caller. A value resolving to
        no Site is passed through unchanged, so filtering on an unknown site
        returns no rows rather than raising.
        """
        row = await self._resolve_scope(site, scope_type=SITE_SCOPE_TYPE)
        return str(row["id"]) if row else str(dcim_location_id(site))

    async def _resolve_any_scope(
        self, value: DCIMLocationIdentifier | int | None
    ) -> tuple[str | None, dict | None]:
        """Resolve a value to ``(scope_type, row)`` when the model is not known.

        For reads that span every kind of overlay -- ``list_overlays`` is filtered
        by isolation type, which may be scoped at any of the four levels. A typed
        reference names its own model and is resolved directly; an untyped one is
        tried against each level in turn.
        """
        typed = scope_type_for(dcim_location_type(value))
        if typed is not None:
            row = await self._resolve_scope(value, scope_type=typed)
            return (typed, row) if row is not None else (None, None)
        rows = await asyncio.gather(
            *(self._resolve_scope(value, scope_type=t) for t in _UNTYPED_SCOPE_ORDER)
        )
        for scope_type, row in zip(_UNTYPED_SCOPE_ORDER, rows, strict=True):
            if row is not None:
                return scope_type, row
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

    async def _overlay_assignments(self, overlay_id) -> list[dict]:
        """Every assignment on an overlay, in one query."""
        return await self.rest_list(self.ASSIGNMENTS_PATH, [("overlay_id", str(overlay_id))])

    @staticmethod
    def _assignments_by_object(rows: list[dict], object_type: str) -> dict[str, dict]:
        """Index assignment rows of one content type by assigned object id."""
        return {
            str(row["assigned_object_id"]): row
            for row in rows
            if str(row.get("assigned_object_type")) == object_type
        }

    async def _delete_assignments(self, rows: list[dict]) -> list[str]:
        """Delete assignment rows concurrently. Returns the ids deleted."""
        ids = [str(row["id"]) for row in rows]
        await asyncio.gather(*(self._safe_delete(f"{self.ASSIGNMENTS_PATH}{i}/") for i in ids))
        return ids

    async def _safe_delete(self, path: str) -> None:
        """DELETE, treating "already gone" as success -- these paths are idempotent."""
        try:
            await self.rest_request("DELETE", path)
        except DCIMNotFoundError:
            pass
