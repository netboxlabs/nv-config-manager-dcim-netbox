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

_LOCATIONS_QUERY = "query { location_list { id name } }"
_LOCATION_BY_ID_QUERY = "query ($id: ID!) { location(id: $id) { id name } }"
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

    async def list_locations(self, location_types: tuple[str, ...] = ()) -> list[DCIMSelection]:
        # ``location_types`` is inert: NetBox's Region/SiteGroup/Site/Location
        # hierarchy has no per-location type to filter on, so it is accepted only
        # for contract parity. Logged so callers can see it did not filter.
        if location_types:
            logger.debug(
                "NetBox has no location-type concept; ignoring location_types=%r", location_types
            )
        data = await self.graphql_query(_LOCATIONS_QUERY)
        return _selections(data.get("location_list") or [])

    async def get_location_metadata(self, location_id: str) -> DCIMSelection | None:
        """Return normalized metadata for one NetBox location PK, or ``None``.

        Same id space as :meth:`list_locations`, so an id taken from a workflow
        form round-trips here. Query failures propagate; only a location that
        NetBox does not have resolves to ``None`` — which NetBox reports as a
        GraphQL error on the singular query, classified by the transport as
        ``DCIMNotFoundError``.
        """
        try:
            data = await self.graphql_query(_LOCATION_BY_ID_QUERY, {"id": location_id})
        except DCIMNotFoundError:
            return None
        location = data.get("location")
        if not location:
            return None
        return DCIMSelection(id=str(location["id"]), name=location["name"])

    async def _managed_device_selections(self, field: str) -> list[DCIMSelection]:
        """Return distinct ``tenant``/``role`` choices across managed devices.

        "Managed" is ``DeviceConfigStatus.render_enabled`` — the same source
        ``get_managed_device_metadata`` uses — resolved to device ids first. Read over
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
        """
        params: list[tuple[str, str]] = []
        if location:
            params.append(("cf_nvcm_site", location))
        vrfs = await self.rest_list(VRFS_PATH, params)
        # Names only — deliberately not ``_mappers.tag_names``, which also returns
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
            # Callers pass a site/location name/slug; the overlay is scoped to a
            # Site or Location (CachedScopeMixin), filtered here by scope_id.
            _scope_type, scope_id = await self._resolve_scope(location)
            if scope_id is None:
                # No NetBox Site/Location by that name -> nothing can match it.
                return []
            params.append(("scope_id", str(scope_id)))
        overlays = await self.rest_list(self.OVERLAYS_PATH, params)
        return _selections(overlays)

    async def list_statuses(self, content_type: str | None = None) -> list[DCIMSelection]:
        """Return the status choices for a content type as ``{id, name}`` options.

        NetBox has no first-class Status object; statuses are per-model choice
        fields, read from the endpoint's ``OPTIONS`` response
        (``actions.POST.status.choices`` → ``value`` / ``display_name``). The
        returned ``id`` is the NetBox status value (e.g. ``"active"``), which is
        exactly what object filters expect, so it round-trips into selection.

        A ``None`` content type resolves to the device status set — the sensible
        NetBox default, since it has no global status list. A *known* content
        type with no status field, or a genuinely unknown one, raises
        ``DCIMInvalidDataError``: the SDK contract (pinned by the provider test
        suite) requires a hard failure here rather than an empty list, so a
        caller cannot mistake "this model has no statuses" for "no statuses are
        defined".
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
