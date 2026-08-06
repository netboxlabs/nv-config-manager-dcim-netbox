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

from nv_config_manager_dcim import DCIMInvalidDataError, DCIMNotFoundError

from nv_config_manager_dcim_netbox._mappers import strip_mask
from nv_config_manager_dcim_netbox.client import encode_device_id

# Config-context profile names NV Config Manager stores DHCP data under.
_SITE_OPTIONS_PROFILE = "nv-config-manager-site-dhcp-option-def"
_STATIC_DATA_PROFILE = "nv-config-manager-kea-static-data"
# Per-device NVCM lifecycle flags (ztp_enabled / is_aggregate_managed) live on the
# plugin's DeviceConfigStatus model, the same source the render/backup paths read.
_STATUS_PATH = "plugins/nv-config-manager/device-config-statuses/"


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
        # Key by the UUID-encoded device id so it matches the device_id the confgen
        # reads back from reservations/option-candidates (which are UUID-encoded so
        # the ZTP boot-file URL resolves in the UUID-keyed config-store).
        return {encode_device_id(d["id"]): (d.get("config_context") or {}) for d in devices}

    async def get_dhcp_auto_subnets(
        self, family: int = 4, is_aggregate_managed: bool | None = None
    ) -> list[dict[str, object]]:
        prefixes = await self.rest_list("ipam/prefixes/", [("tag", "dhcp-subnet")])
        pool_ips = await self.rest_list("ipam/ip-addresses/", [("tag", "dhcp-pool")])
        reserved_ips = await self.rest_list("ipam/ip-addresses/", [("tag", "dhcp-reserve")])
        device_cache: dict[str, dict] = {}
        interface_cache: dict[str, dict] = {}
        status_cache: dict[str, dict] = {}

        # Parse the family-filtered tagged subnets up front so each pool/reserve IP
        # can be assigned to its *most-specific* containing subnet only. NetBox's
        # REST shape returns tagged prefixes and IPs as flat lists with no parent
        # link, so containment is computed here. With nested ``dhcp-subnet``
        # prefixes a pure-containment match would place an IP in every enclosing
        # subnet; longest-prefix-wins keeps it in exactly one (and is a no-op for
        # the flat, non-overlapping case).
        candidates: list[tuple[dict, object]] = []
        for prefix in prefixes:
            if _choice_value(prefix.get("family")) != family:
                continue
            try:
                network = ipaddress.ip_network(prefix["prefix"], strict=False)
            except (ValueError, TypeError, KeyError):
                # Malformed prefix CIDR: skip it rather than failing the fetch.
                continue
            candidates.append((prefix, network))

        pools_by_subnet: dict[object, list[dict]] = {}
        for ip in pool_ips:
            subnet_id = self._most_specific_subnet_id(ip, candidates)
            if subnet_id is not None:
                pools_by_subnet.setdefault(subnet_id, []).append(ip)
        reserves_by_subnet: dict[object, list[dict]] = {}
        for ip in reserved_ips:
            subnet_id = self._most_specific_subnet_id(ip, candidates)
            if subnet_id is not None:
                reserves_by_subnet.setdefault(subnet_id, []).append(ip)

        subnets: list[dict[str, object]] = []
        for prefix, network in candidates:
            gateway = await self._gateway_address(prefix)
            if not gateway:
                # _gateway_address derives a gateway for any parseable prefix (and
                # raises for an out-of-subnet explicit override), so this only guards
                # the impossible malformed-CIDR case already filtered above — don't
                # hand kea a None gateway (its config-gen does
                # ``options["routers"] = str(subnet["gateway"])`` unconditionally,
                # turning None into the literal 'None' it rejects).
                continue
            pools = pools_by_subnet.get(prefix["id"], [])
            reserves = reserves_by_subnet.get(prefix["id"], [])
            reservations = []
            for ip in reserves:
                entry = await self._reservation_entry(
                    ip, is_aggregate_managed, device_cache, interface_cache, status_cache
                )
                if entry is not None:
                    reservations.append(entry)
            # Pool IPs assigned to an interface are also option candidates: the
            # confgen applies their interface-role subnet_options at the subnet
            # level (so a pool/DHCP client — e.g. an OOB management leaf's uplink —
            # still receives its ZTP boot-file, which reservations alone can't
            # deliver). Mirrors the reference DCIM provider.
            option_candidates = []
            for ip in pools:
                entry = await self._option_candidate_entry(
                    ip, is_aggregate_managed, device_cache, interface_cache, status_cache
                )
                if entry is not None:
                    option_candidates.append(entry)
            # The kea config-gen consumes these as ipaddress objects (subnet
            # containment checks, address.version), so hand back typed values.
            subnets.append(
                {
                    "id": prefix["id"],
                    "prefix": network,
                    "gateway": ipaddress.ip_address(gateway),
                    "pool_ips": [
                        ipaddress.ip_address(strip_mask(ip["address"])) for ip in pools
                    ],
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
            ip = await self.rest_request("GET", f"ipam/ip-addresses/{gateway}/")
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
        # No explicit gateway → derive it, matching the reference DCIM provider:
        # /31 and /127 use the network address, otherwise the first usable address (.1).
        # This is why a topology with no explicit gateway (e.g. the air-trial
        # OOB-management subnet) still gets working DHCP — nvcm_gateway is only an
        # override for the uncommon case.
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
        # ZTP-enabled and aggregate-managed are per-device lifecycle flags on the
        # plugin's DeviceConfigStatus model (not device custom fields).
        status = status_cache.get(str(device_id))
        if status is None:
            rows = await self.rest_list(_STATUS_PATH, [("device_id", str(device_id))])
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
            # UUID-encode: the confgen substitutes this into the ZTP boot-file URL
            # (/v1/device/<id>/boot-script), which the ZTP service resolves against
            # the UUID-keyed config-store. A raw integer id 404s there.
            "device_id": encode_device_id(device_id),
            "address": ipaddress.ip_address(address),
        }

    def _aggregate_matches(self, status: dict, is_aggregate_managed) -> bool:
        # Unconstrained unless a specific mode is requested *and* the device's
        # status actually carries the aggregate-managed flag. A missing/empty
        # status (no DeviceConfigStatus row, or one without the flag) must not
        # exclude the device — matching the reference DCIM provider's default-unset
        # semantics. Only enforce the match when the flag is present.
        if is_aggregate_managed is None or not status or "is_aggregate_managed" not in status:
            return True
        return bool(status.get("is_aggregate_managed")) == is_aggregate_managed

    async def _reservation_entry(
        self, ip: dict, is_aggregate_managed, device_cache, interface_cache, status_cache
    ) -> dict | None:
        resolved = await self._resolve_interface_ip(
            ip, device_cache, interface_cache, status_cache
        )
        if resolved is None:
            return None
        interface, device, status, address, device_id = resolved
        # ZTP defaults to enabled: only an *explicit* ztp_enabled=False drops the
        # reservation. A device with no status (empty dict) or an unset flag is
        # still ZTP-eligible — matching the option-candidate path below and the
        # reference DCIM provider's default-True semantics. Using ``not
        # status.get("ztp_enabled")`` here wrongly excluded no-status devices.
        if status.get("ztp_enabled") is False:
            return None
        if not self._aggregate_matches(status, is_aggregate_managed):
            return None
        return self._entry_dict(interface, device, address, device_id)

    async def _option_candidate_entry(
        self, ip: dict, is_aggregate_managed, device_cache, interface_cache, status_cache
    ) -> dict | None:
        # A pool IP assigned to an interface is an option candidate: it carries an
        # interface role whose subnet_options the confgen applies at the subnet
        # level. Matches the reference DCIM provider — ZTP must not be explicitly
        # disabled, the aggregate-managed flag must match, and the interface needs
        # a MAC or the device a serial.
        resolved = await self._resolve_interface_ip(
            ip, device_cache, interface_cache, status_cache
        )
        if resolved is None:
            return None
        interface, device, status, address, device_id = resolved
        if status.get("ztp_enabled") is False:
            return None
        if not self._aggregate_matches(status, is_aggregate_managed):
            return None
        if not interface.get("mac_address") and not device.get("serial"):
            return None
        return self._entry_dict(interface, device, address, device_id)
