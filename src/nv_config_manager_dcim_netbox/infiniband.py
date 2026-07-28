# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""InfiniBand operations (backed by the NetBox fabric plugin).

PKey partitions map to IB-PKey overlays + ``InfiniBandPKey`` rows; interface
membership uses overlay assignments; the port GUID is the ``ib_guid`` interface
custom field. UFM host/pkey resolution walks NetBox devices and their site.
"""

from __future__ import annotations

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
from nv_config_manager_dcim_netbox.fabric_common import FABRIC_STATUS, normalize_pkey

_IB_PKEY_ISOLATION = "ib_pkey"
_IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")

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

_HOST_BY_NAME_QUERY = """
query ($names: [String!]!) {
  device_list(filters: {name: {in_list: $names}}) {
    id
    name
    primary_ip4 { address }
    site { id name }
    location { id name }
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
        iface = await self.rest_request("GET", f"dcim/interfaces/{interface_id}/")
        if not iface:
            raise DCIMNotFoundError(f"NetBox interface {interface_id} not found")
        device = iface.get("device") or {}
        return IBInterfaceGuid(
            interface_id=str(iface["id"]),
            device_name=device.get("name") if isinstance(device, dict) else "",
            interface_name=iface["name"],
            guid=(iface.get("custom_fields") or {}).get("ib_guid") or "",
        )

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
                out.append(
                    IBInterfaceGuid(
                        interface_id=str(iface["id"]),
                        device_name=dname,
                        interface_name=iface["name"],
                        guid=(iface.get("custom_fields") or {}).get("ib_guid") or "",
                    )
                )
        return out

    async def set_ib_interface_guid(self, interface_id: str, guid: str) -> None:
        await self.rest_request(
            "PATCH",
            f"dcim/interfaces/{interface_id}/",
            json={"custom_fields": {"ib_guid": guid}},
        )

    async def get_ib_interface_records(
        self, device_interface_pairs: list[tuple[str, str]]
    ) -> list[IBInterfaceGuid]:
        out: list[IBInterfaceGuid] = []
        for device, iface in device_interface_pairs:
            rows = await self.rest_list("dcim/interfaces/", [("device", device), ("name", iface)])
            for row in rows:
                dev = row.get("device") or {}
                out.append(
                    IBInterfaceGuid(
                        interface_id=str(row["id"]),
                        device_name=dev.get("name") if isinstance(dev, dict) else device,
                        interface_name=row["name"],
                        guid=(row.get("custom_fields") or {}).get("ib_guid") or "",
                    )
                )
        return out

    async def find_ib_interfaces_by_guids(self, guids: list[str]) -> list[IBInterfaceGuid]:
        out: list[IBInterfaceGuid] = []
        for guid in guids:
            rows = await self.rest_list("dcim/interfaces/", [("cf_ib_guid", guid)])
            for row in rows:
                dev = row.get("device") or {}
                out.append(
                    IBInterfaceGuid(
                        interface_id=str(row["id"]),
                        device_name=dev.get("name") if isinstance(dev, dict) else "",
                        interface_name=row["name"],
                        guid=(row.get("custom_fields") or {}).get("ib_guid") or "",
                    )
                )
        return out

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
        location_id = await self._lookup_id("dcim/locations/", location_name)
        if location_id is None:
            raise DCIMInvalidDataError(f"NetBox location {location_name!r} not found")
        tenant_id = await self._resolve_tenant_id(tenant_name)

        overlay = await self._find_overlay(partition_name, location_id)
        if overlay:
            overlay_id = overlay["id"]
        else:
            body = {
                "name": partition_name,
                "location": location_id,
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

    async def _find_ib_assignment(self, overlay_id, interface_id) -> dict | None:
        rows = await self.rest_list(
            self.ASSIGNMENTS_PATH,
            [("overlay_id", str(overlay_id)), ("assigned_object_id", str(interface_id))],
        )
        return rows[0] if rows else None

    async def _create_ib_assignment(self, overlay_id, interface_id, guid, membership_type):
        return await self.rest_request(
            "POST",
            self.ASSIGNMENTS_PATH,
            json={
                "overlay": overlay_id,
                "assigned_object_type": "dcim.interface",
                "assigned_object_id": int(interface_id),
                "guid": guid,
                "membership_type": membership_type,
                "status": FABRIC_STATUS,
            },
        )

    async def ensure_ib_pkey_assignments(
        self, overlay_id: str, assignments: list[tuple[str, str, str]]
    ) -> list[str]:
        result: list[str] = []
        for interface_id, guid, membership_type in assignments:
            existing = await self._find_ib_assignment(overlay_id, interface_id)
            if existing:
                if (existing.get("membership_type") or "") != membership_type:
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
        removed: list[str] = []
        unassigned: list[str] = []
        for interface_id in interface_ids:
            existing = await self._find_ib_assignment(overlay_id, interface_id)
            if existing:
                await self._safe_delete(f"{self.ASSIGNMENTS_PATH}{existing['id']}/")
                removed.append(str(interface_id))
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
                membership_type=(r.get("membership_type") or "full").strip().lower(),
            )
            for r in rows
            if str(r.get("assigned_object_type")) == "dcim.interface"
        ]

    async def sync_ib_pkey_assignments(
        self, overlay_id: str, desired_assignments: list[tuple[str, str, str]]
    ) -> tuple[list[str], list[str], list[str]]:
        rows = await self.rest_list(self.ASSIGNMENTS_PATH, [("overlay_id", str(overlay_id))])
        current = {
            str(r["assigned_object_id"]): r
            for r in rows
            if str(r.get("assigned_object_type")) == "dcim.interface"
        }
        desired = {
            str(iface): (guid, membership) for iface, guid, membership in desired_assignments
        }
        added: list[str] = []
        removed: list[str] = []
        unchanged: list[str] = []
        for iface, row in current.items():
            if iface not in desired:
                await self._safe_delete(f"{self.ASSIGNMENTS_PATH}{row['id']}/")
                removed.append(iface)
        for iface, (guid, membership) in desired.items():
            if iface in current:
                row = current[iface]
                if (row.get("membership_type") or "") != membership:
                    await self.rest_request(
                        "PATCH",
                        f"{self.ASSIGNMENTS_PATH}{row['id']}/",
                        json={"membership_type": membership},
                    )
                unchanged.append(iface)
            else:
                await self._create_ib_assignment(overlay_id, iface, guid, membership)
                added.append(iface)
        return added, removed, unchanged

    # -- host / pkey-context resolution -------------------------------------

    async def _resolve_ib_device(self, host: str) -> dict | None:
        data = await self.graphql_query(_HOST_BY_NAME_QUERY, {"names": [host]})
        devices = data.get("device_list") or []
        if not devices and _IPV4_RE.match(host):
            # Fall back to matching a device whose primary IPv4 host equals `host`.
            # Resolve via the IP address record instead of scanning the whole
            # fleet: the matching IP -> its interface -> that interface's device.
            for ip in await self.rest_list("ipam/ip-addresses/", [("address", host)]):
                if ip.get("assigned_object_type") != "dcim.interface":
                    continue
                iface_id = (ip.get("assigned_object") or {}).get("id")
                if not iface_id:
                    continue
                interface = await self.rest_request("GET", f"dcim/interfaces/{iface_id}/")
                name = ((interface or {}).get("device") or {}).get("name")
                if not name:
                    continue
                found = await self.graphql_query(_HOST_BY_NAME_QUERY, {"names": [name]})
                devices = [
                    c
                    for c in (found.get("device_list") or [])
                    if strip_mask((c.get("primary_ip4") or {}).get("address")) == host
                ]
                if devices:
                    break
        if len(devices) > 1:
            raise DCIMConflictError(f"Multiple NetBox devices match host {host!r}")
        return devices[0] if devices else None

    async def canonicalize_ib_host(self, host: str) -> str:
        device = await self._resolve_ib_device(host)
        if not device:
            raise DCIMNotFoundError(f"No NetBox device for IB host {host!r}")
        return strip_mask((device.get("primary_ip4") or {}).get("address")) or device["name"]

    async def resolve_ib_host_site(self, host: str) -> IBHostSite:
        device = await self._resolve_ib_device(host)
        if not device:
            raise DCIMNotFoundError(f"No NetBox device for IB host {host!r}")
        site = device.get("site") or {}
        return IBHostSite(
            device_id=str(device["id"]),
            device_name=device["name"],
            device_primary_ip=strip_mask((device.get("primary_ip4") or {}).get("address")),
            site_id=str(site.get("id") or ""),
            site_name=site.get("name") or "",
        )

    async def resolve_ib_pkey_context(
        self, host: str, pkey: str, *, create_overlay_for_orphan: bool = False
    ) -> IBPKeyContext:
        pkey_norm = normalize_pkey(pkey)
        device = await self._resolve_ib_device(host)
        if not device:
            raise DCIMNotFoundError(f"No NetBox device for IB host {host!r}")
        host_site = IBHostSite(
            device_id=str(device["id"]),
            device_name=device["name"],
            device_primary_ip=strip_mask((device.get("primary_ip4") or {}).get("address")),
            site_id=str((device.get("site") or {}).get("id") or ""),
            site_name=(device.get("site") or {}).get("name") or "",
        )
        location_id = (device.get("location") or {}).get("id")
        params = [("isolation_type", _IB_PKEY_ISOLATION)]
        if location_id:
            params.append(("location_id", str(location_id)))
        overlays = await self.rest_list(self.OVERLAYS_PATH, params)
        overlays_by_id = {str(overlay["id"]): overlay for overlay in overlays}
        # One pkey query, then match against the location/isolation-scoped
        # overlays, instead of a pkeys query per overlay.
        matches = []
        for pk in await self.rest_list(self.PKEYS_PATH, [("pkey", pkey_norm)]):
            if normalize_pkey(pk.get("pkey", "")) != pkey_norm:
                continue
            overlay = overlays_by_id.get(str(self._fk_id(pk.get("overlay"))))
            if overlay is not None:
                matches.append((overlay, pk))
        if len(matches) > 1:
            raise DCIMConflictError(f"Ambiguous PKey {pkey_norm!r} for host {host!r}")
        if matches:
            overlay, pk = matches[0]
        elif create_overlay_for_orphan:
            overlay, pk = await self._adopt_orphan_pkey(pkey_norm, device, location_id)
        else:
            raise DCIMNotFoundError(f"No IB PKey {pkey_norm!r} found for host {host!r}")
        return IBPKeyContext(
            host_site=host_site,
            overlay_id=str(overlay["id"]),
            overlay_name=overlay["name"],
            pkey_id=str(pk["id"]),
            pkey=pkey_norm,
        )

    async def _adopt_orphan_pkey(self, pkey_norm, device, location_id):
        rows = await self.rest_list(self.PKEYS_PATH, [("pkey", pkey_norm)])
        orphans = [r for r in rows if self._fk_id(r.get("overlay")) is None]
        if not orphans:
            raise DCIMNotFoundError(f"No orphan PKey {pkey_norm!r} to adopt")
        if location_id is None:
            raise DCIMInvalidDataError("Device has no location to host an orphan PKey overlay")
        overlay_name = f"ib-pkey-overlay-{pkey_norm}"
        overlay = await self._find_overlay(overlay_name, location_id)
        if not overlay:
            body = {
                "name": overlay_name,
                "location": location_id,
                "isolation_type": _IB_PKEY_ISOLATION,
                "status": FABRIC_STATUS,
            }
            tenant_id = self._fk_id(device.get("tenant") or {})
            if tenant_id:
                body["tenant"] = tenant_id
            overlay = await self.rest_request("POST", self.OVERLAYS_PATH, json=body)
        pk = orphans[0]
        await self.rest_request(
            "PATCH", f"{self.PKEYS_PATH}{pk['id']}/", json={"overlay": overlay["id"]}
        )
        return overlay, pk
