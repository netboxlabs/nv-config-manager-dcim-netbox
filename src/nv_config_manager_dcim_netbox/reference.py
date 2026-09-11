# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Reference / selection lists (locations, tenants, roles, statuses, overlays)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from nv_config_manager_dcim import (
    DCIMInvalidDataError,
    DCIMLocationIdentifier,
    DCIMNotFoundError,
    DCIMSelection,
    dcim_location_id,
    dcim_location_type,
)

from nv_config_manager_dcim_netbox.fabric_common import (
    LOCATION_SCOPE_TYPE,
    REGION_SCOPE_TYPE,
    SITE_SCOPE_TYPE,
    SITEGROUP_SCOPE_TYPE,
    VRFS_PATH,
    chunked,
    scope_type_for,
)

logger = logging.getLogger(__name__)

# NVCM asks for locations by type -- "Site" for the site dropdown, "Module" for a
# sub-site level. NetBox splits that one hierarchy across four models, and only
# ``dcim.Location`` carries a type of its own: the topology loader parks the
# source LocationType name in the ``nvcm_location_type`` custom field.
#
# So a requested type either names one of the four models, or names a Location
# subtype to filter that model by. ``DCIMSelection.location_type`` answers in the
# same vocabulary, so a value from ``list_locations`` can be passed straight back.
_MODEL_KEYS = {
    "region": "region",
    "sitegroup": "sitegroup",
    "site_group": "sitegroup",
    "site": "site",
    "location": "location",
}
_MODEL_QUERY = {
    "region": ("region_list", "Region"),
    "sitegroup": ("site_group_list", "SiteGroup"),
    "site": ("site_list", "Site"),
    "location": ("location_list", "Location"),
}

# Written by the plugin's topology loader; also what the render reports as
# ``location.kind``.
_LOCATION_TYPE_FIELD = "nvcm_location_type"

# With no type given, return what devices can actually be scoped to: a Site, or
# one of its optional Location sub-groupings.
_DEFAULT_LOCATION_TYPES = ("Site", "Location")

# Order an *untyped* id is resolved in. A reference carrying its location type
# names its own model and skips this entirely.
_LOCATION_RESOLUTION_ORDER = ("site", "location", "region", "sitegroup")

# The scope type ``fabric_common`` derives from a location type, mapped back to
# the model key used here, so the type -> model rule lives in one place.
_SCOPE_TO_MODEL = {
    REGION_SCOPE_TYPE: "region",
    SITEGROUP_SCOPE_TYPE: "sitegroup",
    SITE_SCOPE_TYPE: "site",
    LOCATION_SCOPE_TYPE: "location",
}
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
        """Return the rows for one model of NetBox's location hierarchy.

        Locations also carry ``custom_fields`` -- that is where the source
        LocationType lives, and it is what distinguishes a Module from a Rack.
        """
        list_field, _ = _MODEL_QUERY[kind]
        fields = "id name custom_fields" if kind == "location" else "id name"
        data = await self.graphql_query(f"query {{ {list_field} {{ {fields} }} }}")
        return list(data.get(list_field) or [])

    @staticmethod
    def _location_type_of(kind: str, row: dict[str, Any]) -> str:
        """Return the location type to report for one row."""
        label = _MODEL_QUERY[kind][1]
        if kind != "location":
            return label
        custom = row.get("custom_fields")
        if isinstance(custom, str):
            try:
                custom = json.loads(custom)
            except ValueError:
                custom = None
        if isinstance(custom, dict) and custom.get(_LOCATION_TYPE_FIELD):
            return str(custom[_LOCATION_TYPE_FIELD])
        return label

    async def list_locations(self, location_types: tuple[str, ...] = ()) -> list[DCIMSelection]:
        """Return location options, each tagged with its own location type.

        A requested type either names one of NetBox's four hierarchy models or a
        Location subtype (``Module``, ``Rack``, ...) held in the custom field.
        Ids are per-model in NetBox, so two levels can share one -- they are both
        offered, and ``location_type`` is what tells them apart on the way back.
        """
        requested = [t.strip() for t in location_types if t and t.strip()]
        models: list[str] = []
        subtypes: set[str] = set()
        for raw in requested or _DEFAULT_LOCATION_TYPES:
            model = _MODEL_KEYS.get(raw.lower().replace(" ", "_"))
            if model is not None:
                if model not in models:
                    models.append(model)
            else:
                # Casefolded, matching the model lookup above: ("module",) and
                # ("Module",) name the same thing.
                subtypes.add(raw.casefold())

        # A subtype is a Location narrowed by its custom field, so asking for one
        # means reading that model even when it was not named outright.
        reads = list(models)
        if subtypes and "location" not in reads:
            reads.append("location")

        pages = await asyncio.gather(*(self._locations_of(kind) for kind in reads))
        out: list[DCIMSelection] = []
        for kind, rows in zip(reads, pages, strict=True):
            for row in rows:
                location_type = self._location_type_of(kind, row)
                # Only filter the Location read, and only when the caller asked
                # for subtypes without asking for the model itself.
                if kind == "location" and subtypes and "location" not in models:
                    if location_type.casefold() not in subtypes:
                        continue
                out.append(
                    DCIMSelection(id=str(row["id"]), name=row["name"], location_type=location_type)
                )
        return out

    async def get_location_metadata(self, location: DCIMLocationIdentifier) -> DCIMSelection | None:
        """Return normalized metadata for one NetBox location id, or ``None``.

        Same id space as :meth:`list_locations`, so a value taken from a workflow
        form round-trips here. A reference carrying its location type names the
        model outright; a bare id is tried against each level in
        :data:`_LOCATION_RESOLUTION_ORDER` and the first match wins.

        One document with an aliased list query per level, rather than a singular
        query per level in turn: a list filtered to a missing id comes back empty
        instead of erroring, so every level resolves in a single round-trip.
        """
        location_id = dcim_location_id(location)
        scope = scope_type_for(dcim_location_type(location))
        order = (
            [_SCOPE_TO_MODEL[scope]]
            if scope in _SCOPE_TO_MODEL
            else list(_LOCATION_RESOLUTION_ORDER)
        )
        aliases = {f"lvl{index}": kind for index, kind in enumerate(order)}
        # `id` on a list filter is an IDComparisonFilterLookup, not a bare ID --
        # passing the scalar makes NetBox reject the whole document.
        selections = " ".join(
            f"{alias}: {_MODEL_QUERY[kind][0]}(filters: {{id: {{exact: $id}}}}) "
            f"{{ id name{' custom_fields' if kind == 'location' else ''} }}"
            for alias, kind in aliases.items()
        )
        try:
            data = await self.graphql_query(
                f"query ($id: ID!) {{ {selections} }}", {"id": location_id}
            )
        except DCIMNotFoundError:
            return None
        for alias, kind in aliases.items():
            rows = data.get(alias) or []
            if rows:
                return DCIMSelection(
                    id=str(rows[0]["id"]),
                    name=rows[0]["name"],
                    location_type=self._location_type_of(kind, rows[0]),
                )
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

    async def list_namespace_tags(
        self, location: DCIMLocationIdentifier | None = None
    ) -> list[str]:
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
        self, location: DCIMLocationIdentifier | None = None, isolation_type: str | None = None
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
