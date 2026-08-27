# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""InfiniBand operations (backed by the NetBox fabric plugin).

PKey partitions map to IB-PKey overlays + ``InfiniBandPKey`` rows; interface
membership uses overlay assignments; the port GUID is the ``ib_guid`` interface
custom field. UFM host/pkey resolution walks NetBox devices and their site.
"""

from __future__ import annotations

import asyncio
import re

from nv_config_manager_dcim import (
    DCIMConflictError,
    DCIMInvalidDataError,
    DCIMNotFoundError,
    IBHostSite,
    IBInterfaceGuid,
    IBNeighbor,
    IBPKeyAssignment,
    IBPKeyCleanup,
    IBPKeyContext,
    IBPKeyPartition,
    IBSwitchTopology,
)

from nv_config_manager_dcim_netbox._mappers import strip_mask
from nv_config_manager_dcim_netbox.fabric_common import (
    FABRIC_STATUS,
    INTERFACES_PATH,
    IP_ADDRESSES_PATH,
    LOCATION_SCOPE_TYPE,
    normalize_pkey,
)

_IB_PKEY_ISOLATION = "ib_pkey"
_IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def _normalize_membership(value: str | None) -> str:
    """Canonicalize an IB PKey membership type (default ``full``, stripped/lower).

    UFM and NetBox may report the membership as ``""``, ``"Full"``, or with
    surrounding whitespace; normalizing both sides of a comparison avoids
    redundant PATCH thrash on values that are semantically equal.
    """
    return (value or "full").strip().lower() or "full"


def _fk_name(value: object) -> str:
    """Return a nested object's name, or ``""`` for a bare PK / missing object."""
    return str(value.get("name") or "") if isinstance(value, dict) else ""


def _ib_guid(row: dict, device_name: str = "") -> IBInterfaceGuid:
    """Build an :class:`IBInterfaceGuid` from an interface row.

    The port GUID is the ``ib_guid`` interface custom field. ``device_name`` is the
    fallback used when the row carries no nested device (e.g. one fetched by a
    query that already knows which device it asked about).
    """
    return IBInterfaceGuid(
        interface_id=str(row["id"]),
        device_name=_fk_name(row.get("device")) or device_name,
        interface_name=row["name"],
        guid=(row.get("custom_fields") or {}).get("ib_guid") or "",
    )


_SWITCH_TOPOLOGY_QUERY = """
query ($ids: [ID!]!) {
  device_list(filters: {id: {in_list: $ids}}) {
    id
    name
    interfaces {
      name
      connected_endpoints { ... on InterfaceType { name device { name } } }
    }
  }
}
"""

_INTERFACE_GUIDS_QUERY = """
query ($names: [String!]!) {
  device_list(filters: {name: {in_list: $names}}) {
    name
    interfaces { id name custom_fields }
  }
}
"""

# Device role an IB host must carry to be accepted as the UFM appliance. Matches
# the reference provider's role-name check.
_UFM_ROLE_NAME = "UFM"

_HOST_BY_NAME_QUERY = """
query ($names: [String!]!) {
  device_list(filters: {name: {in_list: $names}}) {
    id
    name
    role { name }
    primary_ip4 { address }
    tenant { id name }
    site { id name }
    location {
      id
      name
      parent {
        id
        name
        parent {
          id
          name
          parent { id name }
        }
      }
    }
  }
}
"""


class NetBoxInfiniBandMixin:
    """InfiniBand topology, GUID, partition, and membership operations."""

    # -- topology + GUIDs ----------------------------------------------------

    async def get_ib_switch_topology(self, switch_device_ids: list[str]) -> IBSwitchTopology:
        if not switch_device_ids:
            return IBSwitchTopology(switch_names={}, intended_neighbors={})
        data = await self.graphql_query(
            _SWITCH_TOPOLOGY_QUERY, {"ids": [str(i) for i in switch_device_ids]}
        )
        switch_names: dict[str, str] = {}
        neighbors: dict[str, dict[str, IBNeighbor]] = {}
        for device in data.get("device_list") or []:
            did = str(device["id"])
            switch_names[did] = device["name"]
            local: dict[str, IBNeighbor] = {}
            for iface in device.get("interfaces") or []:
                endpoint = next(iter(iface.get("connected_endpoints") or []), None)
                peer_device = (endpoint or {}).get("device") or {}
                if endpoint and endpoint.get("name") and peer_device.get("name"):
                    local[iface["name"]] = IBNeighbor(
                        device_name=peer_device["name"], interface_name=endpoint["name"]
                    )
            neighbors[did] = local
        return IBSwitchTopology(switch_names=switch_names, intended_neighbors=neighbors)

    async def get_ib_interface_guid(self, interface_id: str) -> IBInterfaceGuid:
        iface = await self.rest_request("GET", f"{INTERFACES_PATH}{interface_id}/")
        if not iface:
            raise DCIMNotFoundError(f"NetBox interface {interface_id} not found")
        return _ib_guid(iface)

    async def get_ib_interface_guids(
        self, device_interface_pairs: set[tuple[str, str]]
    ) -> list[IBInterfaceGuid]:
        if not device_interface_pairs:
            return []
        names = sorted({device for device, _ in device_interface_pairs})
        requested = {(device.lower(), iface) for device, iface in device_interface_pairs}
        data = await self.graphql_query(_INTERFACE_GUIDS_QUERY, {"names": names})
        out: list[IBInterfaceGuid] = []
        for device in data.get("device_list") or []:
            dname = device["name"]
            for iface in device.get("interfaces") or []:
                if (dname.lower(), iface["name"]) not in requested:
                    continue
                out.append(_ib_guid(iface, dname))
        return out

    async def set_ib_interface_guid(self, interface_id: str, guid: str) -> None:
        await self.rest_request(
            "PATCH",
            f"{INTERFACES_PATH}{interface_id}/",
            json={"custom_fields": {"ib_guid": guid}},
        )

    async def get_ib_interface_records(
        self, device_interface_pairs: list[tuple[str, str]]
    ) -> list[IBInterfaceGuid]:
        # One query per device (``name`` is multi-valued and ORs) instead of one
        # per pair, and the per-device queries run concurrently.
        by_device: dict[str, list[str]] = {}
        for device, iface in device_interface_pairs:
            by_device.setdefault(device, []).append(iface)
        pages = await asyncio.gather(
            *(
                self.rest_list(
                    INTERFACES_PATH,
                    [("device", device), *(("name", iface) for iface in ifaces)],
                )
                for device, ifaces in by_device.items()
            )
        )
        out: list[IBInterfaceGuid] = []
        for device, rows in zip(by_device, pages, strict=True):
            wanted = set(by_device[device])
            out.extend(_ib_guid(row, device) for row in rows if row.get("name") in wanted)
        return out

    async def find_ib_interfaces_by_guids(self, guids: list[str]) -> list[IBInterfaceGuid]:
        # Exact filter only: NetBox ignores an unknown ``__ie`` lookup on a custom
        # field and returns every interface, which reads back as a bogus multi-match.
        # The exact filter is already case-insensitive. Repeated ``cf_*`` params are
        # not reliably OR-ed, so query per GUID, concurrently.
        pages = await asyncio.gather(
            *(
                self.rest_list(INTERFACES_PATH, [("cf_ib_guid", guid)])
                for guid in dict.fromkeys(guids)
            )
        )
        return [_ib_guid(row) for rows in pages for row in rows]

    # -- partitions ----------------------------------------------------------

    async def ensure_ib_pkey_partition(
        self,
        pkey: str,
        partition_name: str,
        location_name: str,
        tenant_name: str | None,
        membership_type: str,
    ) -> IBPKeyPartition:
        pkey_norm = normalize_pkey(pkey)
        # Normalized on write as well as on compare, so the stored value cannot
        # differ from what ensure_/sync_ib_pkey_assignments later compare against.
        membership_type = _normalize_membership(membership_type)
        location_row = await self._resolve_scope(location_name, scope_type=LOCATION_SCOPE_TYPE)
        if location_row is None:
            raise DCIMInvalidDataError(f"NetBox Location {location_name!r} not found")
        tenant_id = await self._resolve_tenant_id(tenant_name)

        overlay = await self._find_overlay(
            partition_name, location_name, scope_type=LOCATION_SCOPE_TYPE
        )
        if overlay:
            overlay_id = overlay["id"]
        else:
            body = {
                "name": partition_name,
                "scope_type": LOCATION_SCOPE_TYPE,
                "scope_id": location_row["id"],
                "isolation_type": _IB_PKEY_ISOLATION,
                "status": FABRIC_STATUS,
            }
            if tenant_id is not None:
                body["tenant"] = tenant_id
            overlay_id = (await self.rest_request("POST", self.OVERLAYS_PATH, json=body))["id"]

        rows = await self.rest_list(
            self.PKEYS_PATH, [("pkey", pkey_norm), ("overlay_id", str(overlay_id))]
        )
        if rows:
            pkey_obj = rows[0]
        else:
            body = {
                "pkey": pkey_norm,
                "name": f"PKey-{pkey_norm}",
                "overlay": overlay_id,
                "membership_type": membership_type,
                "status": FABRIC_STATUS,
            }
            if tenant_id is not None:
                body["tenant"] = tenant_id
            pkey_obj = await self.rest_request("POST", self.PKEYS_PATH, json=body)
        return IBPKeyPartition(
            pkey_id=str(pkey_obj["id"]),
            pkey=pkey_norm,
            partition_id=str(overlay_id),
            partition_name=partition_name,
        )

    async def ensure_orphan_ib_pkey(self, pkey: str) -> IBPKeyPartition:
        pkey_norm = normalize_pkey(pkey)
        rows = await self.rest_list(self.PKEYS_PATH, [("pkey", pkey_norm)])
        orphans = [r for r in rows if self._fk_id(r.get("overlay")) is None]
        if len(orphans) > 1:
            raise DCIMConflictError(f"Multiple orphan PKeys {pkey_norm!r}")
        if orphans:
            obj = orphans[0]
        else:
            obj = await self.rest_request(
                "POST",
                self.PKEYS_PATH,
                json={"name": f"PKey-{pkey_norm}", "pkey": pkey_norm, "status": FABRIC_STATUS},
            )
        return IBPKeyPartition(pkey_id=str(obj["id"]), pkey=pkey_norm)

    async def cleanup_ib_pkey_partition(
        self, overlay_id: str, overlay_name: str, pkey_id: str, pkey: str, ufm_partition_empty: bool
    ) -> IBPKeyCleanup:
        assignments = await self.rest_list(self.ASSIGNMENTS_PATH, [("overlay_id", str(overlay_id))])
        if assignments or not ufm_partition_empty:
            return IBPKeyCleanup(
                partition_empty=False,
                pkey_deleted=False,
                overlay_deleted=False,
                remaining_assignments=len(assignments),
            )
        await self._safe_delete(f"{self.PKEYS_PATH}{pkey_id}/")
        overlay_deleted = False
        if overlay_name == f"ib-pkey-overlay-{normalize_pkey(pkey)}":
            remaining = await self.rest_list(self.PKEYS_PATH, [("overlay_id", str(overlay_id))])
            if not remaining:
                await self._safe_delete(f"{self.OVERLAYS_PATH}{overlay_id}/")
                overlay_deleted = True
        return IBPKeyCleanup(
            partition_empty=True,
            pkey_deleted=True,
            overlay_deleted=overlay_deleted,
            remaining_assignments=0,
        )

    # -- assignments ---------------------------------------------------------

    async def _ib_assignments_by_interface(self, overlay_id) -> dict[str, dict]:
        """Return the overlay's interface assignments keyed by interface id.

        One query, so callers walking a device's ports do a dict lookup rather than
        a round trip each.
        """
        rows = await self.rest_list(self.ASSIGNMENTS_PATH, [("overlay_id", str(overlay_id))])
        return {
            str(r["assigned_object_id"]): r
            for r in rows
            if str(r.get("assigned_object_type")) == "dcim.interface"
        }

    async def _create_ib_assignment(self, overlay_id, interface_id, guid, membership_type):
        return await self._create_overlay_assignment(
            overlay_id,
            "dcim.interface",
            interface_id,
            guid=guid,
            membership_type=membership_type,
        )

    async def ensure_ib_pkey_assignments(
        self, overlay_id: str, assignments: list[tuple[str, str, str]]
    ) -> list[str]:
        current = await self._ib_assignments_by_interface(overlay_id)
        result: list[str] = []
        for interface_id, guid, membership_type in assignments:
            membership_type = _normalize_membership(membership_type)
            existing = current.get(str(interface_id))
            if existing:
                if _normalize_membership(existing.get("membership_type")) != membership_type:
                    await self.rest_request(
                        "PATCH",
                        f"{self.ASSIGNMENTS_PATH}{existing['id']}/",
                        json={"membership_type": membership_type},
                    )
                result.append(str(existing["id"]))
            else:
                row = await self._create_ib_assignment(
                    overlay_id, interface_id, guid, membership_type
                )
                result.append(str(row["id"]))
        return result

    async def remove_ib_pkey_assignments(
        self, overlay_id: str, interface_ids: list[str]
    ) -> tuple[list[str], list[str]]:
        # Contract: ``removed`` carries ASSIGNMENT ids (the rows deleted), while
        # ``unassigned`` carries the INTERFACE ids that had no assignment to
        # remove -- matching the reference DCIM provider's return identity.
        current = await self._ib_assignments_by_interface(overlay_id)
        removed: list[str] = []
        unassigned: list[str] = []
        for interface_id in interface_ids:
            existing = current.get(str(interface_id))
            if existing:
                await self._safe_delete(f"{self.ASSIGNMENTS_PATH}{existing['id']}/")
                removed.append(str(existing["id"]))
            else:
                unassigned.append(str(interface_id))
        return removed, unassigned

    async def get_ib_pkey_assignments(self, overlay_id: str) -> list[IBPKeyAssignment]:
        rows = await self.rest_list(self.ASSIGNMENTS_PATH, [("overlay_id", str(overlay_id))])
        return [
            IBPKeyAssignment(
                assignment_id=str(r["id"]),
                interface_id=str(r["assigned_object_id"]),
                guid=r.get("guid") or "",
                membership_type=_normalize_membership(r.get("membership_type")),
            )
            for r in rows
            if str(r.get("assigned_object_type")) == "dcim.interface"
        ]

    async def sync_ib_pkey_assignments(
        self, overlay_id: str, desired_assignments: list[tuple[str, str, str]]
    ) -> tuple[list[str], list[str], list[str]]:
        current = await self._ib_assignments_by_interface(overlay_id)
        desired = {
            str(iface): (guid, _normalize_membership(membership))
            for iface, guid, membership in desired_assignments
        }
        # Contract: all three lists carry ASSIGNMENT ids (matching the reference
        # DCIM provider), not interface ids.
        added: list[str] = []
        removed: list[str] = []
        unchanged: list[str] = []
        for iface, row in current.items():
            if iface not in desired:
                await self._safe_delete(f"{self.ASSIGNMENTS_PATH}{row['id']}/")
                removed.append(str(row["id"]))
        for iface, (guid, membership) in desired.items():
            if iface in current:
                row = current[iface]
                if _normalize_membership(row.get("membership_type")) != membership:
                    await self.rest_request(
                        "PATCH",
                        f"{self.ASSIGNMENTS_PATH}{row['id']}/",
                        json={"membership_type": membership},
                    )
                unchanged.append(str(row["id"]))
            else:
                created = await self._create_ib_assignment(overlay_id, iface, guid, membership)
                added.append(str(created["id"]))
        return added, removed, unchanged

    # -- host / pkey-context resolution -------------------------------------

    async def _resolve_ib_device(self, host: str) -> dict | None:
        data = await self.graphql_query(_HOST_BY_NAME_QUERY, {"names": [host]})
        devices = data.get("device_list") or []
        if not devices and _IPV4_RE.match(host):
            # Also match a device carrying `host` on any interface, not just its
            # primary IPv4. Resolve IP -> interface -> device rather than scanning
            # the fleet, deduping by id so the conflict guard below still fires.
            interface_ids = sorted(
                {
                    str(iface_id)
                    for ip in await self.rest_list(IP_ADDRESSES_PATH, [("address", host)])
                    if ip.get("assigned_object_type") == "dcim.interface"
                    and (iface_id := (ip.get("assigned_object") or {}).get("id"))
                }
            )
            by_id: dict[str, dict] = {}
            if interface_ids:
                # One interfaces query for every matching IP, then one host query
                # for the distinct device names — rather than a GET plus a GraphQL
                # round trip per address.
                interfaces = await self.rest_list(
                    INTERFACES_PATH, [("id", i) for i in interface_ids]
                )
                names = sorted({name for i in interfaces if (name := _fk_name(i.get("device")))})
                if names:
                    found = await self.graphql_query(_HOST_BY_NAME_QUERY, {"names": names})
                    for candidate in found.get("device_list") or []:
                        by_id[str(candidate["id"])] = candidate
            devices = list(by_id.values())
        if len(devices) > 1:
            raise DCIMConflictError(f"Multiple NetBox devices match host {host!r}")
        if not devices:
            return None
        device = devices[0]
        # A host resolved here is the UFM appliance that owns the IB fabric. Names
        # and IPs are not unique to that role, so an unrelated device answering to
        # the same name must be rejected rather than driving UFM operations.
        if (device.get("role") or {}).get("name") != _UFM_ROLE_NAME:
            raise DCIMInvalidDataError(
                f"Device {device['name']!r} is not assigned the {_UFM_ROLE_NAME} role in NetBox"
            )
        return device

    async def canonicalize_ib_host(self, host: str) -> str:
        device = await self._resolve_ib_device(host)
        if not device:
            raise DCIMNotFoundError(f"No NetBox device for IB host {host!r}")
        return strip_mask((device.get("primary_ip4") or {}).get("address")) or device["name"]

    @staticmethod
    def _ib_location_chain(location: dict | None) -> list[dict]:
        """Return a device's location followed by each of its parent locations.

        NetBox scopes overlays by ``location``; the reference provider matches a
        PKey at the device location *or any ancestor*. NetBox models this as the
        self-referential ``Location`` tree (rather than a typed location chain),
        so we walk the ``parent`` links fetched by the host GraphQL query.
        """
        chain: list[dict] = []
        current = location
        seen: set = set()
        while current and current.get("id") not in seen:
            seen.add(current.get("id"))
            chain.append(current)
            current = current.get("parent")
        return chain

    def _ib_host_site(self, device: dict) -> IBHostSite:
        """Build the public host-site model, requiring a Site on the device.

        The reference provider requires a Site-typed ancestor in the location
        hierarchy. NetBox models Site as a first-class Device field, so the
        NetBox-idiomatic guard is a non-empty ``device.site``; a device with no
        Site cannot resolve to an IB host site.
        """
        site = device.get("site") or {}
        site_id = self._fk_id(site)
        if not site_id:
            raise DCIMInvalidDataError(
                f"NetBox device {device.get('name')!r} has no Site; cannot resolve IB host site"
            )
        return IBHostSite(
            device_id=str(device["id"]),
            device_name=device["name"],
            device_primary_ip=strip_mask((device.get("primary_ip4") or {}).get("address")),
            site_id=str(site_id),
            site_name=(site.get("name") if isinstance(site, dict) else "") or "",
        )

    async def resolve_ib_host_site(self, host: str) -> IBHostSite:
        device = await self._resolve_ib_device(host)
        if not device:
            raise DCIMNotFoundError(f"No NetBox device for IB host {host!r}")
        return self._ib_host_site(device)

    async def resolve_ib_pkey_context(
        self, host: str, pkey: str, *, create_overlay_for_orphan: bool = False
    ) -> IBPKeyContext:
        pkey_norm = normalize_pkey(pkey)
        device = await self._resolve_ib_device(host)
        if not device:
            raise DCIMNotFoundError(f"No NetBox device for IB host {host!r}")
        host_site = self._ib_host_site(device)
        # Match the PKey at the device location OR any ancestor. Scoped on exact
        # ``scope_id`` (+ scope_type, to disambiguate the GFK), not ``location_id``:
        # that filter is descendant-inclusive, so the ancestor chain would also match
        # sibling locations' overlays.
        location_chain = self._ib_location_chain(device.get("location"))
        location_id = self._fk_id(device.get("location") or {})
        scope_ids = [
            str(chain_id)
            for chain_location in location_chain
            if (chain_id := self._fk_id(chain_location)) is not None
        ]
        if not scope_ids:
            # Device.location is optional in NetBox. With no scope_id the overlay
            # query is unscoped and matches every ib_pkey overlay in the install.
            raise DCIMInvalidDataError(
                f"NetBox device {device.get('name')!r} has no Location; "
                f"cannot scope IB PKey {pkey_norm!r} to an overlay"
            )
        params = [("isolation_type", _IB_PKEY_ISOLATION), ("scope_type", LOCATION_SCOPE_TYPE)]
        params.extend(("scope_id", scope_id) for scope_id in scope_ids)
        overlays = await self.rest_list(self.OVERLAYS_PATH, params)
        overlays_by_id = {str(overlay["id"]): overlay for overlay in overlays}
        # One pkey query, then match against the location/isolation-scoped
        # overlays, instead of a pkeys query per overlay.
        matches = []
        pkey_rows = await self.rest_list(self.PKEYS_PATH, [("pkey", pkey_norm)])
        for pk in pkey_rows:
            try:
                stored = normalize_pkey(pk.get("pkey", ""))
            except DCIMInvalidDataError:
                continue  # skip malformed stored pkeys instead of failing the walk
            if stored != pkey_norm:
                continue
            overlay = overlays_by_id.get(str(self._fk_id(pk.get("overlay"))))
            if overlay is not None:
                matches.append((overlay, pk))
        if len(matches) > 1:
            raise DCIMConflictError(f"Ambiguous PKey {pkey_norm!r} for host {host!r}")
        if matches:
            overlay, pk = matches[0]
        elif create_overlay_for_orphan:
            overlay, pk = await self._adopt_orphan_pkey(pkey_norm, device, location_id, pkey_rows)
        else:
            raise DCIMNotFoundError(f"No IB PKey {pkey_norm!r} found for host {host!r}")
        return IBPKeyContext(
            host_site=host_site,
            overlay_id=str(overlay["id"]),
            overlay_name=overlay["name"],
            pkey_id=str(pk["id"]),
            pkey=pkey_norm,
        )

    async def _adopt_orphan_pkey(self, pkey_norm, device, location_id, pkey_rows):
        # ``pkey_rows`` is the caller's own query, not a re-fetch: a second read is
        # not atomic with the first, so a PKey created between them would trip the
        # "Multiple orphan PKeys" guard on rows the caller never saw.
        orphans = [r for r in pkey_rows if self._fk_id(r.get("overlay")) is None]
        if not orphans:
            raise DCIMNotFoundError(f"No orphan PKey {pkey_norm!r} to adopt")
        if len(orphans) > 1:
            details = ", ".join(f"id={r.get('id')}" for r in orphans)
            raise DCIMConflictError(
                f"Multiple orphan PKeys {pkey_norm!r} ({details}); "
                "resolve the duplicates before adopting"
            )
        if location_id is None:
            raise DCIMInvalidDataError("Device has no location to host an orphan PKey overlay")
        # The reference provider requires a Tenant on the device before it will
        # auto-create an overlay for an orphan PKey; enforce the same guard here.
        tenant_id = self._fk_id(device.get("tenant") or {})
        if not tenant_id:
            raise DCIMInvalidDataError(
                f"Device {device.get('name')!r} has no Tenant; cannot auto-create an overlay "
                f"for orphan PKey {pkey_norm}. Set a Tenant on the device or pre-create an "
                "overlay and link the PKey to it."
            )
        overlay_name = f"ib-pkey-overlay-{pkey_norm}"
        # ``location_id`` is the device's Location PK; scope the overlay to that
        # Location directly (no name resolution needed since we already have the id).
        existing = await self.rest_list(
            self.OVERLAYS_PATH,
            [
                ("name", overlay_name),
                ("scope_id", str(location_id)),
                ("scope_type", LOCATION_SCOPE_TYPE),
            ],
        )
        overlay = existing[0] if existing else None
        if not overlay:
            body = {
                "name": overlay_name,
                "scope_type": LOCATION_SCOPE_TYPE,
                "scope_id": location_id,
                "isolation_type": _IB_PKEY_ISOLATION,
                "status": FABRIC_STATUS,
                "tenant": tenant_id,
            }
            overlay = await self.rest_request("POST", self.OVERLAYS_PATH, json=body)
        pk = orphans[0]
        # Refuse to relink a PKey that is already attached to a different overlay.
        current_overlay_id = self._fk_id(pk.get("overlay"))
        if current_overlay_id is not None and str(current_overlay_id) != str(overlay["id"]):
            raise DCIMConflictError(
                f"PKey {pkey_norm!r} (id={pk['id']}) is already linked to overlay "
                f"{current_overlay_id!r}; refusing to relink to {overlay['id']!r}"
            )
        if current_overlay_id is None:
            await self.rest_request(
                "PATCH", f"{self.PKEYS_PATH}{pk['id']}/", json={"overlay": overlay["id"]}
            )
        return overlay, pk
