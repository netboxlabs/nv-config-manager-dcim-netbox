# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Reference / selection lists (locations, tenants, roles, statuses, overlays)."""

from __future__ import annotations

from typing import Any

from nv_config_manager_dcim import DCIMInvalidDataError, DCIMSelection

_LOCATIONS_QUERY = "query { location_list { id name } }"
_TENANTS_QUERY = "query { tenant_list { id name } }"
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
# NetBox has no global status list; default to device statuses (the primary
# selection context) when no content type is given.
_DEFAULT_STATUS_CONTENT_TYPE = "dcim.device"


def _selections(items: list[dict[str, Any]]) -> list[DCIMSelection]:
    return [DCIMSelection(id=str(i["id"]), name=i["name"]) for i in items]


class NetBoxReferenceMixin:
    """Read-only reference data used to populate workflow forms."""

    async def list_locations(self, location_types: tuple[str, ...] = ()) -> list[DCIMSelection]:
        # NetBox locations have no "location type" concept; the arg is ignored.
        data = await self.graphql_query(_LOCATIONS_QUERY)
        return _selections(data.get("location_list") or [])

    async def list_tenants(self, managed_only: bool = False) -> list[DCIMSelection]:
        # managed_only relies on the NVCM state custom field; not applied here.
        data = await self.graphql_query(_TENANTS_QUERY)
        return _selections(data.get("tenant_list") or [])

    async def list_roles(self, managed_only: bool = False) -> list[DCIMSelection]:
        # managed_only relies on the NVCM state custom field; not applied here.
        data = await self.graphql_query(_DEVICE_ROLES_QUERY)
        return _selections(data.get("device_role_list") or [])

    async def list_namespace_tags(self, location: str | None = None) -> list[str]:
        """Return the namespace tag names in use across NetBox VRFs.

        NetBox has no first-class IPAM namespace: a namespace is a VRF, and a
        namespace *zone* is emulated with a tag on the VRFs in it. These tag
        names are the namespace selectors NVCM offers as choices. ``location``
        is accepted for contract parity but does not constrain the result —
        NetBox VRFs are global, so there is no location dimension to filter on.
        """
        vrfs = await self.rest_list("ipam/vrfs/")
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
            # Callers pass a site/location *name*; the filter needs a numeric PK.
            location_id = await self._resolve_location_id(location)
            if location_id is None:
                # No NetBox Location by that name -> nothing can match it.
                return []
            params.append(("location_id", str(location_id)))
        overlays = await self.rest_list("plugins/nv-config-manager/overlays/", params)
        return _selections(overlays)

    async def list_statuses(self, content_type: str | None = None) -> list[DCIMSelection]:
        """Return the status choices for a content type as ``{id, name}`` options.

        NetBox has no first-class Status object; statuses are per-model choice
        fields, read from the endpoint's ``OPTIONS`` response
        (``actions.POST.status.choices`` → ``value`` / ``display_name``). The
        returned ``id`` is the NetBox status value (e.g. ``"active"``), which is
        exactly what object filters expect, so it round-trips into selection.
        ``content_type`` defaults to ``dcim.device`` (NetBox has no global list).
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
