# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Reference / selection lists (locations, tenants, roles, statuses, overlays)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from nv_config_manager_dcim import DCIMInvalidDataError, DCIMNotFoundError, DCIMSelection

from nv_config_manager_dcim_netbox.fabric_common import VRFS_PATH, chunked

logger = logging.getLogger(__name__)

# NVCM asks for locations by type ("Site" for the site dropdown). NetBox has no
# per-location type field; its hierarchy *is* the type, one model per level. So a
# requested type selects the model to query rather than filtering one list.
#
# Devices attach to a Site, and the mock-topology loader maps a blueprint's
# Provider/Region levels onto Regions and its Site level onto a Site, creating no
# NetBox Location objects at all. Querying `location_list` therefore returned
# nothing and left NVCM's site dropdown empty.
_LOCATION_MODELS = {
    "site": ("site_list", "site"),
    "region": ("region_list", "region"),
    "sitegroup": ("site_group_list", "site_group"),
    "site_group": ("site_group_list", "site_group"),
    "location": ("location_list", "location"),
}

# With no type given, return what devices can actually be scoped to: a Site, or
# one of its optional Location sub-groupings. Sites first, because ids are
# per-model in NetBox -- a Site and a Location can hold the same PK, and only one
# of them can be offered under that id (see list_locations).
_DEFAULT_LOCATION_MODELS = ("site", "location")

# Order in which a bare PK is resolved back to an object. NVCM stores a location
# id as the plain integer NetBox uses (`is_valid_location_id` rejects anything
# else), so the id alone cannot say which model it came from and this fixes which
# one wins. Every model is listed, or an id from an explicitly-typed
# `list_locations(("region",))` call would resolve to None.
_LOCATION_RESOLUTION_ORDER = ("site", "location", "region", "site_group")
_TENANTS_QUERY = "query { tenant_list { id name } }"

# Only the two nested objects the managed-only dropdowns need, for the given ids.
_MANAGED_DEVICE_SELECTIONS_QUERY = """
query ($ids: [ID!]!) {
  device_list(filters: {id: {in_list: $ids}}) {
    tenant { id name }
    role { id name }
  }
}
"""
_DEVICE_ROLES_QUERY = "query { device_role_list { id name } }"

# NetBox has no first-class Status object: statuses are per-model choice fields,
# read from each endpoint's OPTIONS response. Map the NVCM content-type string to
# the REST endpoint whose ``status`` field choices it wants. Only models that
# actually have a status field are listed. NetBox status *values* (e.g. "active")
# are what its filters expect, so the returned ``id`` round-trips back into
# device/object selection.
_STATUS_ENDPOINTS = {
    "dcim.device": "dcim/devices/",
    "dcim.interface": "dcim/interfaces/",
    "dcim.rack": "dcim/racks/",
    "dcim.site": "dcim/sites/",
    "dcim.location": "dcim/locations/",
    "dcim.cable": "dcim/cables/",
    "dcim.powerfeed": "dcim/power-feeds/",
    "dcim.virtualdevicecontext": "dcim/virtual-device-contexts/",
    "circuits.circuit": "circuits/circuits/",
    "circuits.virtualcircuit": "circuits/virtual-circuits/",
    "ipam.prefix": "ipam/prefixes/",
    "ipam.ipaddress": "ipam/ip-addresses/",
    "ipam.iprange": "ipam/ip-ranges/",
    "ipam.vlan": "ipam/vlans/",
    "ipam.vrf": "ipam/vrfs/",
    "virtualization.virtualmachine": "virtualization/virtual-machines/",
    "virtualization.vminterface": "virtualization/interfaces/",
}
# NetBox has no global status list. The reference provider returns a global set
# for a null content type; NetBox's closest sensible default is the device
# status set (the primary selection context), which is what a null content type
# resolves to here.
_DEFAULT_STATUS_CONTENT_TYPE = "dcim.device"


def _selections(items: list[dict[str, Any]]) -> list[DCIMSelection]:
    return [DCIMSelection(id=str(i["id"]), name=i["name"]) for i in items]


class NetBoxReferenceMixin:
    """Read-only reference data used to populate workflow forms."""

    async def _locations_of(self, kind: str) -> list[dict[str, Any]]:
        """Return the ``{id, name}`` rows for one level of NetBox's hierarchy."""
        list_field = _LOCATION_MODELS[kind][0]
        data = await self.graphql_query(f"query {{ {list_field} {{ id name }} }}")
        return list(data.get(list_field) or [])

    async def list_locations(self, location_types: tuple[str, ...] = ()) -> list[DCIMSelection]:
        # A requested type selects which model to query -- NetBox's
        # Region/SiteGroup/Site/Location hierarchy *is* the type, with no per-object
        # type field to filter on.
        requested = [t.lower().replace(" ", "_") for t in location_types]
        unknown = [t for t in requested if t not in _LOCATION_MODELS]
        if unknown:
            logger.debug("No NetBox model for location type(s) %r; skipping", unknown)

        # Distinct models, in the order asked for. "sitegroup" and "site_group" are
        # accepted spellings of one model, so a caller passing both must not make it
        # be queried twice.
        kinds: list[str] = []
        for name in requested or _DEFAULT_LOCATION_MODELS:
            model = _LOCATION_MODELS.get(name)
            if model is not None and model not in [_LOCATION_MODELS[k] for k in kinds]:
                kinds.append(name)

        seen: dict[str, str] = {}
        rows: list[dict[str, Any]] = []
        for kind in kinds:
            for item in await self._locations_of(kind):
                key = str(item["id"])
                if key in seen:
                    # Two levels holding the same PK. Only one can be offered: the
                    # id is all NVCM sends back, and get_location_metadata resolves
                    # it to exactly one object.
                    logger.debug(
                        "NetBox %s id %s collides with the %s of that id; keeping the %s",
                        kind,
                        key,
                        seen[key],
                        seen[key],
                    )
                    continue
                seen[key] = kind
                rows.append(item)
        return _selections(rows)

    async def get_location_metadata(self, location_id: str) -> DCIMSelection | None:
        """Return normalized metadata for one NetBox location PK, or ``None``.

        Same id space as :meth:`list_locations`, so an id taken from a workflow
        form round-trips here. Every level is tried, in
        :data:`_LOCATION_RESOLUTION_ORDER`, and the first match wins -- PKs are
        per-model, so ``list_locations`` never offers two entries under one id.

        One document with an aliased list query per level, rather than a singular
        query per level in turn: a list filtered to a missing id comes back empty
        instead of erroring, so all four resolve in a single round-trip.
        """
        aliases = {
            f"lvl{index}": _LOCATION_MODELS[kind][0]
            for index, kind in enumerate(_LOCATION_RESOLUTION_ORDER)
        }
        # `id` on a list filter is an IDComparisonFilterLookup, not a bare ID --
        # passing the scalar makes NetBox reject the whole document.
        selections = " ".join(
            f"{alias}: {field}(filters: {{id: {{exact: $id}}}}) {{ id name }}"
            for alias, field in aliases.items()
        )
        try:
            data = await self.graphql_query(
                f"query ($id: ID!) {{ {selections} }}", {"id": location_id}
            )
        except DCIMNotFoundError:
            return None
        for alias in aliases:
            rows = data.get(alias) or []
            if rows:
                return DCIMSelection(id=str(rows[0]["id"]), name=rows[0]["name"])
        return None

    async def _managed_device_selections(self, field: str) -> list[DCIMSelection]:
        """Return distinct ``tenant``/``role`` choices across managed devices.

        "Managed" is ``DeviceConfigStatus.render_enabled`` -- the same source
        ``get_managed_device_metadata`` uses -- resolved to device ids first. Read over
        GraphQL asking only for the two nested objects, rather than paginating every
        field of the whole managed fleet to collect a few dozen names.
        """
        managed_ids = sorted(await self.status_flag_device_ids(render_enabled=True) or ())
        if not managed_ids:
            return []
        pages = await asyncio.gather(
            *(
                self.graphql_query(_MANAGED_DEVICE_SELECTIONS_QUERY, {"ids": chunk})
                for chunk in chunked(managed_ids)
            )
        )
        selections: dict[str, DCIMSelection] = {}
        for page in pages:
            for device in page.get("device_list") or []:
                obj = device.get(field)
                if isinstance(obj, dict) and obj.get("id") is not None and obj.get("name"):
                    key = str(obj["id"])
                    selections[key] = DCIMSelection(id=key, name=str(obj["name"]))
        return list(selections.values())

    async def list_tenants(self, managed_only: bool = False) -> list[DCIMSelection]:
        if managed_only:
            return await self._managed_device_selections("tenant")
        data = await self.graphql_query(_TENANTS_QUERY)
        return _selections(data.get("tenant_list") or [])

    async def list_roles(self, managed_only: bool = False) -> list[DCIMSelection]:
        if managed_only:
            return await self._managed_device_selections("role")
        data = await self.graphql_query(_DEVICE_ROLES_QUERY)
        return _selections(data.get("device_role_list") or [])

    async def list_namespace_tags(self, location: str | None = None) -> list[str]:
        """Return the namespace tag names in use across NetBox VRFs.

        NetBox has no first-class IPAM namespace: a namespace is a VRF, and a
        namespace *zone* is emulated with a tag on the VRFs in it. These tag
        names are the namespace selectors NVCM offers as choices.

        ``location`` scopes the VRFs server-side via the ``nvcm_site`` VRF custom
        field (NetBox VRFs have no native site link). This is guarded on that
        field: when ``location`` is omitted no scoping is applied, and where the
        field is undefined the query is simply unconstrained.

        Resolved to the Site pk first, which is what that field holds. See
        :meth:`_site_field_value`.
        """
        params: list[tuple[str, str]] = []
        if location:
            params.append(("cf_nvcm_site", await self._site_field_value(location)))
        vrfs = await self.rest_list(VRFS_PATH, params)
        # Names only -- deliberately not ``_mappers.tag_names``, which also returns
        # slugs. These are offered to operators as choices, so a tag must appear
        # once (as its name), not twice (name and slug).
        tags: set[str] = set()
        for vrf in vrfs:
            for tag in vrf.get("tags") or []:
                name = tag.get("name") if isinstance(tag, dict) else None
                if isinstance(name, str) and name:
                    tags.add(name)
        return sorted(tags)

    async def list_overlays(
        self, location: str | None = None, isolation_type: str | None = None
    ) -> list[DCIMSelection]:
        params = []
        if isolation_type:
            params.append(("isolation_type", isolation_type))
        if location:
            # The only caller that cannot say which model it means: this lists both
            # kinds of overlay, and SpectrumX anchors at a Site while InfiniBand
            # anchors at a Location. Whichever resolves is filtered on, with its
            # scope_type -- pks are per-model, so scope_id alone would also match
            # the other model's overlays.
            scope_type, row = await self._resolve_any_scope(location)
            if row is None:
                return []
            params.append(("scope_id", str(row["id"])))
            params.append(("scope_type", str(scope_type)))
        overlays = await self.rest_list(self.OVERLAYS_PATH, params)
        return _selections(overlays)

    async def list_statuses(self, content_type: str | None = None) -> list[DCIMSelection]:
        """Return the status choices for a content type as ``{id, name}`` options.

        NetBox has no first-class Status object; statuses are per-model choice
        fields read from the endpoint's ``OPTIONS`` response. The returned ``id``
        is the NetBox value (``"active"``), which round-trips into a filter.

        ``None`` resolves to the device status set. A known content type with no
        status field, or an unknown one, raises ``DCIMInvalidDataError`` -- the
        SDK contract wants a hard failure rather than an empty list.
        """
        resolved = content_type or _DEFAULT_STATUS_CONTENT_TYPE
        endpoint = _STATUS_ENDPOINTS.get(resolved)
        if endpoint is None:
            raise DCIMInvalidDataError(
                f"NetBox has no known status choices for content type {resolved!r}"
            )
        options = await self.rest_request("OPTIONS", endpoint)
        choices = (
            (((options or {}).get("actions") or {}).get("POST") or {}).get("status") or {}
        ).get("choices") or []
        return [
            DCIMSelection(
                id=str(choice["value"]),
                name=str(choice.get("display_name") or choice.get("display") or choice["value"]),
            )
            for choice in choices
            if isinstance(choice, dict) and choice.get("value") is not None
        ]
