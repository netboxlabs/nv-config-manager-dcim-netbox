# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""DHCP operations.

Backed by native NetBox IPAM (prefixes/IPs tagged ``dhcp-subnet`` / ``dhcp-pool``
/ ``dhcp-reserve``), config-context profiles for Kea static/option data, and the
``nvcm_gateway`` object custom field for a subnet's gateway. Returns plain JSON
structures consumed by the Kea config generator.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging

from nv_config_manager_dcim import DCIMInvalidDataError, DCIMNotFoundError

from nv_config_manager_dcim_netbox._mappers import choice_value, fk_id, strip_mask

# Per-device NVCM lifecycle flags (ztp_enabled / is_aggregate_managed) live on the
# plugin's DeviceConfigStatus model, the same source the render/backup paths read.
from nv_config_manager_dcim_netbox.fabric_common import (
    DEVICES_PATH,
    INTERFACES_PATH,
    IP_ADDRESSES_PATH,
    PREFIXES_PATH,
    STATUS_PATH,
)

logger = logging.getLogger(__name__)

# Config-context profile names NV Config Manager stores DHCP data under.
_SITE_OPTIONS_PROFILE = "nv-config-manager-site-dhcp-option-def"
_STATIC_DATA_PROFILE = "nv-config-manager-kea-static-data"


class NetBoxDHCPMixin:
    """DHCP subnet, pool, reservation, and option reads."""

    async def get_dhcp_site_options(self) -> dict[str, object]:
        contexts = await self.config_contexts_for_profile(_SITE_OPTIONS_PROFILE)
        return (contexts[0].get("data") or {}) if contexts else {}

    async def get_dhcp_static_data(self) -> list[dict[str, object]]:
        contexts = await self.config_contexts_for_profile(_STATIC_DATA_PROFILE)
        return [c.get("data") or {} for c in contexts]

    async def get_dhcp_contexts(
        self, is_aggregate_managed: bool | None = None
    ) -> dict[str, dict[str, object]]:
        # ZTP eligibility is a DeviceConfigStatus field, not a device custom field:
        # resolve it to ids, then fetch those devices for their config_context.
        ztp_device_ids = await self.status_flag_device_ids(
            ztp_enabled=True, is_aggregate_managed=is_aggregate_managed
        )
        devices = await self.devices_by_ids(ztp_device_ids or ())
        # Keyed by the NetBox device id, matching the device_id the confgen reads
        # back from reservations / option-candidates.
        return {str(d["id"]): (d.get("config_context") or {}) for d in devices}

    async def get_dhcp_auto_subnets(
        self, family: int = 4, is_aggregate_managed: bool | None = None
    ) -> list[dict[str, object]]:
        # The three tag queries are independent, so issue them together.
        prefixes, pool_ips, reserved_ips = await asyncio.gather(
            # Filter by family server-side; the client-side check below still
            # guards a NetBox that ignores the parameter.
            self.rest_list(PREFIXES_PATH, [("tag", "dhcp-subnet"), ("family", str(family))]),
            self.rest_list(IP_ADDRESSES_PATH, [("tag", "dhcp-pool")]),
            self.rest_list(IP_ADDRESSES_PATH, [("tag", "dhcp-reserve")]),
        )

        # NetBox returns tagged prefixes and IPs as flat lists with no parent link,
        # so containment is computed here. Longest-prefix-wins assigns each IP to
        # exactly one subnet, which matters once dhcp-subnet prefixes nest.
        candidates: list[tuple[dict, object]] = []
        for prefix in prefixes:
            if choice_value(prefix.get("family")) != family:
                continue
            try:
                network = ipaddress.ip_network(prefix["prefix"], strict=False)
            except (ValueError, TypeError, KeyError):
                # Malformed prefix CIDR: skip it rather than failing the fetch.
                continue
            candidates.append((prefix, network))

        pools_by_subnet = self._bucket_by_subnet(pool_ips, candidates)
        reserves_by_subnet = self._bucket_by_subnet(reserved_ips, candidates)

        # Warm the interface/device/status caches in three bulk queries rather than
        # letting _resolve_interface_ip fetch each one individually (it still falls
        # back to a single fetch for anything not prefetched).
        device_cache, interface_cache, status_cache = await self._prefetch_ip_owners(
            [ip for ips in pools_by_subnet.values() for ip in ips]
            + [ip for ips in reserves_by_subnet.values() for ip in ips]
        )

        subnets: list[dict[str, object]] = []
        for prefix, network in candidates:
            gateway = await self._gateway_address(prefix)
            if not gateway:
                # Unreachable in practice (malformed CIDRs are filtered above), but
                # kea stringifies the gateway unconditionally and would emit the
                # literal 'None', which it then rejects.
                continue
            pools = pools_by_subnet.get(prefix["id"], [])
            reserves = reserves_by_subnet.get(prefix["id"], [])
            caches = (device_cache, interface_cache, status_cache)
            reservations = [
                entry
                for ip in reserves
                if (entry := await self._dhcp_entry(ip, is_aggregate_managed, *caches))
            ]
            # Pool IPs on an interface are also option candidates: the confgen
            # applies their interface-role subnet_options at subnet level, so DHCP
            # clients still get a ZTP boot-file. Unlike a reservation, a candidate
            # needs an addressable identity (MAC or serial).
            option_candidates = [
                entry
                for ip in pools
                if (
                    entry := await self._dhcp_entry(
                        ip, is_aggregate_managed, *caches, require_identity=True
                    )
                )
            ]
            # The kea config-gen consumes these as ipaddress objects (subnet
            # containment checks, address.version), so hand back typed values.
            subnets.append(
                {
                    "id": prefix["id"],
                    "prefix": network,
                    "gateway": ipaddress.ip_address(gateway),
                    "pool_ips": [ipaddress.ip_address(strip_mask(ip["address"])) for ip in pools],
                    "option_candidates": option_candidates,
                    "reservations": reservations,
                }
            )
        return subnets

    # -- helpers -------------------------------------------------------------

    def _ip_in_network(self, ip: dict, network) -> bool:
        addr = ip.get("address")
        if not addr:
            return False
        try:
            return ipaddress.ip_interface(addr).ip in network
        except ValueError:
            return False

    def _bucket_by_subnet(
        self, ips: list[dict], candidates: list[tuple[dict, object]]
    ) -> dict[object, list[dict]]:
        """Group ``ips`` under the id of the most-specific subnet containing each."""
        buckets: dict[object, list[dict]] = {}
        for ip in ips:
            subnet_id = self._most_specific_subnet_id(ip, candidates)
            if subnet_id is not None:
                buckets.setdefault(subnet_id, []).append(ip)
        return buckets

    async def _prefetch_ip_owners(
        self, ips: list[dict]
    ) -> tuple[dict[str, dict], dict[str, dict], dict[str, dict]]:
        """Bulk-load the interfaces, devices, and config-statuses ``ips`` resolve to.

        Returns the three caches ``_resolve_interface_ip`` consumes. Without this,
        that method issues three requests per address; here it is three requests
        total. Anything a bulk query does not return is simply absent from the
        cache and falls back to the per-item fetch.
        """
        interface_cache: dict[str, dict] = {}
        device_cache: dict[str, dict] = {}
        status_cache: dict[str, dict] = {}

        interface_ids = {
            str(iface_id)
            for ip in ips
            if ip.get("assigned_object_type") == "dcim.interface"
            and (iface_id := (ip.get("assigned_object") or {}).get("id"))
        }
        if not interface_ids:
            return device_cache, interface_cache, status_cache

        for row in await self.rest_list(
            INTERFACES_PATH, [("id", i) for i in sorted(interface_ids)]
        ):
            if row.get("id") is not None:
                interface_cache[str(row["id"])] = row

        device_ids = {
            str(pk)
            for row in interface_cache.values()
            if (pk := fk_id(row.get("device"))) is not None
        }
        if not device_ids:
            return device_cache, interface_cache, status_cache

        devices, statuses = await asyncio.gather(
            self.rest_list(DEVICES_PATH, [("id", i) for i in sorted(device_ids)]),
            self.rest_list(STATUS_PATH, [("device_id", i) for i in sorted(device_ids)]),
        )
        for row in devices:
            if row.get("id") is not None:
                device_cache[str(row["id"])] = row
        for row in statuses:
            pk = fk_id(row.get("device"))
            if pk is not None:
                status_cache.setdefault(str(pk), row)
        # A device with no DeviceConfigStatus row caches as {} so the per-item path
        # does not re-query it; _aggregate_matches treats that as "flag unset".
        for device_id in device_ids:
            status_cache.setdefault(device_id, {})
        return device_cache, interface_cache, status_cache

    def _most_specific_subnet_id(self, ip: dict, candidates: list[tuple[dict, object]]):
        """Return the id of the longest-prefix candidate subnet containing ``ip``.

        ``candidates`` is a list of ``(prefix_dict, ip_network)`` pairs. When
        ``dhcp-subnet`` prefixes are nested, an IP is contained by several; the
        most specific (longest prefixlen) wins so the IP is assigned to exactly
        one subnet. Returns ``None`` when no tagged subnet contains the IP. Reduces
        to the single match in the flat, non-overlapping case.
        """
        best_id = None
        best_len = -1
        for prefix, network in candidates:
            if network.prefixlen > best_len and self._ip_in_network(ip, network):
                best_id = prefix["id"]
                best_len = network.prefixlen
        return best_id

    async def _gateway_address(self, prefix: dict) -> str | None:
        try:
            network = ipaddress.ip_network(prefix.get("prefix"), strict=False)
        except (ValueError, TypeError):
            return None
        # Explicit override first: the nvcm_gateway custom field (an IP object or id).
        gateway = (prefix.get("custom_fields") or {}).get("nvcm_gateway")
        explicit = None
        if isinstance(gateway, dict):
            explicit = strip_mask(gateway.get("address"))
        elif gateway:
            ip = await self.rest_request("GET", f"{IP_ADDRESSES_PATH}{gateway}/")
            explicit = strip_mask((ip or {}).get("address"))
        if explicit:
            # Validate the override actually lies within the subnet, matching the
            # reference DCIM provider — a gateway outside the prefix is a data error
            # kea can't honor, so fail loudly rather than emitting a bogus router.
            if ipaddress.ip_address(explicit) not in network:
                raise DCIMInvalidDataError(
                    f"nvcm_gateway {explicit} is not within subnet {network}"
                )
            return explicit
        # Derive the gateway when none is set, so a subnet without an explicit one
        # still gets working DHCP: /31 and /127 use the network address, everything
        # else the first usable (.1). nvcm_gateway is only an override.
        if network.prefixlen in (31, 127):
            return str(network.network_address)
        return str(network.network_address + 1)

    async def _resolve_interface_ip(
        self, ip: dict, device_cache, interface_cache, status_cache
    ) -> tuple | None:
        """Resolve an interface-assigned IP to (interface, device, status,
        address, device_id), or None when it can't be resolved.

        Only interface-assigned IPs resolve to a device; IPs assigned to other
        object types (VM interfaces, FHRP groups, ...) are skipped so a
        cross-model id collision can't resolve to an unrelated interface.
        """
        if ip.get("assigned_object_type") != "dcim.interface":
            return None
        address = strip_mask(ip.get("address"))
        if not address:
            return None
        assigned = ip.get("assigned_object") or {}
        iface_id = assigned.get("id")
        if not iface_id:
            return None
        interface = interface_cache.get(str(iface_id))
        if interface is None:
            try:
                interface = await self.rest_request("GET", f"{INTERFACES_PATH}{iface_id}/")
            except DCIMNotFoundError:
                # Stale assignment pointing at a deleted interface: skip it
                # rather than failing the whole subnet fetch.
                interface = None
            interface_cache[str(iface_id)] = interface or {}
        if not interface:
            return None
        device_id = fk_id(interface.get("device") or {})
        if not device_id:
            return None
        device = device_cache.get(str(device_id))
        if device is None:
            try:
                device = await self.rest_request("GET", f"{DEVICES_PATH}{device_id}/")
            except DCIMNotFoundError:
                # Stale interface pointing at a deleted device: skip it rather
                # than failing the whole subnet fetch (same policy as the
                # interface lookup above).
                device = None
            device_cache[str(device_id)] = device or {}
        if not device:
            return None
        # ZTP-enabled and aggregate-managed are per-device lifecycle flags on the
        # plugin's DeviceConfigStatus model (not device custom fields).
        status = status_cache.get(str(device_id))
        if status is None:
            rows = await self.rest_list(STATUS_PATH, [("device_id", str(device_id))])
            status = rows[0] if rows else {}
            status_cache[str(device_id)] = status
        return interface, device, status, address, device_id

    def _interface_role(self, interface: dict):
        # NetBox has no native interface role field, so the loader persists the
        # source design's role into a custom field. The kea config-gen keys
        # per-role DHCP option overrides on it. Fall back to a native ``role``
        # field if a future NetBox/plugin ever provides one.
        role = (interface.get("custom_fields") or {}).get("nvcm_interface_role")
        if role:
            return role
        native = interface.get("role")
        return native.get("name") if isinstance(native, dict) else native

    def _entry_dict(self, interface, device, address, device_id) -> dict:
        return {
            "mac_address": interface.get("mac_address"),
            "serial": device.get("serial"),
            "platform": (device.get("platform") or {}).get("name")
            if isinstance(device.get("platform"), dict)
            else None,
            "interface_name": interface.get("name"),
            "interface_role": self._interface_role(interface),
            "device_name": device.get("name"),
            # The confgen substitutes this into the ZTP boot-file URL
            # (/v1/device/<id>/boot-script), which the ZTP service resolves against
            # the config-store under this same provider-owned id.
            "device_id": str(device_id),
            "address": ipaddress.ip_address(address),
        }

    def _aggregate_matches(self, status: dict, is_aggregate_managed) -> bool:
        # Only enforced when a mode is requested and the device's status carries the
        # flag. A missing status row, or one without the flag, must not exclude the
        # device.
        if is_aggregate_managed is None or not status or "is_aggregate_managed" not in status:
            return True
        return bool(status.get("is_aggregate_managed")) == is_aggregate_managed

    async def _dhcp_entry(
        self,
        ip: dict,
        is_aggregate_managed,
        device_cache,
        interface_cache,
        status_cache,
        *,
        require_identity: bool = False,
    ) -> dict | None:
        """Build one reservation / option-candidate entry for an interface-assigned IP.

        Both kinds share their eligibility rules, matching the reference DCIM
        provider: the IP must resolve to a device, ZTP must not be *explicitly*
        disabled, and the aggregate-managed flag must match. ``require_identity``
        adds the option-candidate rule that the interface carry a MAC or the device
        a serial. Returns ``None`` when the IP is not eligible.
        """
        resolved = await self._resolve_interface_ip(ip, device_cache, interface_cache, status_cache)
        if resolved is None:
            return None
        interface, device, status, address, device_id = resolved
        # ZTP defaults to enabled: only an explicit False drops the entry, so a
        # device with no status row is still eligible. Note this is deliberately not
        # ``not status.get("ztp_enabled")``, which would exclude those.
        if status.get("ztp_enabled") is False:
            return None
        if not self._aggregate_matches(status, is_aggregate_managed):
            return None
        if require_identity and not interface.get("mac_address") and not device.get("serial"):
            return None
        return self._entry_dict(interface, device, address, device_id)
