# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""DHCP operations.

Backed by native NetBox IPAM (prefixes/IPs tagged ``dhcp-subnet`` / ``dhcp-pool``
/ ``dhcp-reserve``), config-context profiles for Kea static/option data, and the
``nvcm_gateway`` object custom field for a subnet's gateway. Returns plain JSON
structures consumed by the Kea config generator.
"""

from __future__ import annotations

import ipaddress

from nv_config_manager_dcim import DCIMNotFoundError

from nv_config_manager_dcim_netbox._mappers import strip_mask

# Config-context profile names NV Config Manager stores DHCP data under.
_SITE_OPTIONS_PROFILE = "nv-config-manager-site-dhcp-option-def"
_STATIC_DATA_PROFILE = "nv-config-manager-kea-static-data"


def _choice_value(value):
    return value.get("value") if isinstance(value, dict) else value


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
        params = [("cf_nvcm_ztp_enabled", "true")]
        if is_aggregate_managed is not None:
            params.append(("cf_nvcm_is_aggregate_managed", str(is_aggregate_managed).lower()))
        devices = await self.rest_list("dcim/devices/", params)
        return {str(d["id"]): (d.get("config_context") or {}) for d in devices}

    async def get_dhcp_auto_subnets(
        self, family: int = 4, is_aggregate_managed: bool | None = None
    ) -> list[dict[str, object]]:
        prefixes = await self.rest_list("ipam/prefixes/", [("tag", "dhcp-subnet")])
        pool_ips = await self.rest_list("ipam/ip-addresses/", [("tag", "dhcp-pool")])
        reserved_ips = await self.rest_list("ipam/ip-addresses/", [("tag", "dhcp-reserve")])
        device_cache: dict[str, dict] = {}
        interface_cache: dict[str, dict] = {}

        subnets: list[dict[str, object]] = []
        for prefix in prefixes:
            if _choice_value(prefix.get("family")) != family:
                continue
            network = ipaddress.ip_network(prefix["prefix"], strict=False)
            pools = [ip for ip in pool_ips if self._ip_in_network(ip, network)]
            reserves = [ip for ip in reserved_ips if self._ip_in_network(ip, network)]
            reservations = []
            for ip in reserves:
                entry = await self._reservation_entry(
                    ip, is_aggregate_managed, device_cache, interface_cache
                )
                if entry is not None:
                    reservations.append(entry)
            subnets.append(
                {
                    "id": prefix["id"],
                    "prefix": prefix["prefix"],
                    "gateway": await self._gateway_address(prefix),
                    "pool_ips": [strip_mask(ip["address"]) for ip in pools],
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

    async def _gateway_address(self, prefix: dict) -> str | None:
        gateway = (prefix.get("custom_fields") or {}).get("nvcm_gateway")
        if isinstance(gateway, dict):
            return strip_mask(gateway.get("address"))
        if gateway:
            ip = await self.rest_request("GET", f"ipam/ip-addresses/{gateway}/")
            return strip_mask((ip or {}).get("address"))
        return None

    async def _reservation_entry(
        self, ip: dict, is_aggregate_managed, device_cache, interface_cache
    ) -> dict | None:
        # Only interface-assigned reservations resolve to a device; skip IPs
        # assigned to other object types (VM interfaces, FHRP groups, ...) so a
        # cross-model id collision can't resolve to an unrelated interface.
        if ip.get("assigned_object_type") != "dcim.interface":
            return None
        assigned = ip.get("assigned_object") or {}
        iface_id = assigned.get("id")
        if not iface_id:
            return None
        interface = interface_cache.get(str(iface_id))
        if interface is None:
            try:
                interface = await self.rest_request("GET", f"dcim/interfaces/{iface_id}/")
            except DCIMNotFoundError:
                # Stale assignment pointing at a deleted interface: skip it
                # rather than failing the whole subnet fetch.
                interface = None
            interface_cache[str(iface_id)] = interface or {}
        if not interface:
            return None
        device_ref = interface.get("device") or {}
        device_id = device_ref.get("id") if isinstance(device_ref, dict) else device_ref
        if not device_id:
            return None
        device = device_cache.get(str(device_id))
        if device is None:
            device = await self.rest_request("GET", f"dcim/devices/{device_id}/")
            device_cache[str(device_id)] = device or {}
        cf = device.get("custom_fields") or {}
        if not cf.get("nvcm_ztp_enabled"):
            return None
        if (
            is_aggregate_managed is not None
            and bool(cf.get("nvcm_is_aggregate_managed")) != is_aggregate_managed
        ):
            return None
        return {
            "mac_address": interface.get("mac_address"),
            "serial": device.get("serial"),
            "platform": (device.get("platform") or {}).get("name")
            if isinstance(device.get("platform"), dict)
            else None,
            "interface_name": interface.get("name"),
            "device_name": device.get("name"),
            "device_id": str(device_id),
            "address": strip_mask(ip.get("address")),
        }
