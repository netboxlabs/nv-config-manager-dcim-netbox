# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Map NetBox REST / GraphQL responses into provider-neutral SDK models.

NetBox specifics reflected here:
- ``site`` is a native FK on Device (no location-parent climb).
- IP fields carry a mask (``address`` = ``10.0.0.1/24``); strip it for a host.
- MAC lives on a separate model: GraphQL ``primary_mac_address { mac_address }``;
  REST keeps a back-compat scalar ``mac_address`` on the interface serializer.
"""

from __future__ import annotations

from typing import Any

import netaddr
from nv_config_manager_dcim import (
    DCIMInvalidDataError,
    DeviceMetadata,
    HostInterfaceMetadata,
    HostMetadata,
)
from nv_config_manager_dcim.workflow_models import (
    DeviceBayData,
    HostDeviceData,
    InterfaceData,
    NetworkDeviceData,
    Platform,
)

# NVCM status custom fields on the NetBox device. ``nvcm_render_enabled`` and
# ``nvcm_ztp_enabled`` are already used elsewhere in this provider (inventory /
# DHCP); ``nvcm_deploy_enabled`` and ``nvcm_backup_enabled`` are the parity
# additions. When a field is not defined on the device its value reads back as
# ``None`` and the flag degrades to ``False`` (see ``_cf_flag``).
_STATUS_FLAG_FIELDS = {
    "render_enabled": "nvcm_render_enabled",
    "deploy_enabled": "nvcm_deploy_enabled",
    "backup_enabled": "nvcm_backup_enabled",
    "ztp_enabled": "nvcm_ztp_enabled",
}

# NetBox custom field carrying a host's operator-facing alias (parity with the
# reference provider's ``cf_alias``). Absent field -> ``None``.
_ALIAS_FIELD = "alias"


def strip_mask(address: str | None) -> str | None:
    """Return the host portion of a NetBox ``address`` value (drops ``/mask``)."""
    if not address:
        return None
    return address.split("/", 1)[0]


def canonical_mac(value: str | None) -> str | None:
    """Return a canonical MAC string, or ``None`` for empty/invalid input.

    Normalizes to a lowercase, colon-separated, zero-padded form so MAC
    comparisons are case- and separator-insensitive (``AA-BB-CC-00-00-01`` and
    ``aa:bb:cc:0:0:1`` both canonicalize to ``aa:bb:cc:00:00:01``). NetBox itself
    already stores MACs on a first-class ``MACAddress`` model, so this is used at
    matching boundaries (e.g. deduplicating an interface's MACs) rather than to
    rewrite values NetBox returns for display.
    """
    if not value:
        return None
    try:
        return str(netaddr.EUI(value, dialect=netaddr.mac_unix_expanded))
    except (netaddr.AddrFormatError, ValueError, TypeError):
        return value


def _cf_flag(device: dict[str, Any], field: str) -> bool:
    """Read a boolean NVCM status custom field, defaulting absent/None to False."""
    return bool((device.get("custom_fields") or {}).get(field))


def _slug(obj: Any) -> str:
    return (obj or {}).get("slug") or ""


def _name(obj: Any) -> str | None:
    return (obj or {}).get("name") if obj else None


def platform_from_slug(slug: str | None, device_name: str) -> Platform:
    """Coerce a NetBox platform slug into the workflow ``Platform`` enum."""
    if not slug:
        raise DCIMInvalidDataError(f"NetBox device {device_name!r} has no platform")
    try:
        return Platform(slug)
    except ValueError as exc:
        raise DCIMInvalidDataError(
            f"Unsupported platform {slug!r} for NetBox device {device_name!r}"
        ) from exc


def device_metadata_from_gql(device: dict[str, Any], device_url: str) -> DeviceMetadata:
    """Map a device object to ``DeviceMetadata``.

    The GraphQL and REST device shapes coincide for these fields (nested
    ``{name}`` objects and a masked ``primary_ip4.address``), so both sources
    share this mapping via the ``device_metadata_from_rest`` alias below.
    """
    return DeviceMetadata(
        device_id=str(device["id"]),
        name=device["name"],
        site=_name(device.get("site")) or "Unknown",
        platform=_name(device.get("platform")),
        role=_name(device.get("role")),
        rack=_name(device.get("rack")),
        primary_ip4=strip_mask((device.get("primary_ip4") or {}).get("address")),
        device_url=device_url,
    )


# REST and GraphQL device shapes coincide for every DeviceMetadata field.
device_metadata_from_rest = device_metadata_from_gql


def network_device_from_rest(device: dict[str, Any]) -> NetworkDeviceData:
    """Map a REST device object to ``NetworkDeviceData``.

    The ``*_enabled`` flags are backed by NVCM status custom fields on the device
    (``nvcm_render_enabled`` etc.). A field that is not defined on the device
    reads back as ``None`` and degrades to ``False``, so this is safe to apply
    before every flag exists in a given NetBox instance.
    """
    name = device["name"]
    return NetworkDeviceData(
        id=str(device["id"]),
        name=name,
        rack=_name(device.get("rack")),
        position=device.get("position"),
        role=_slug(device.get("role")),
        site=_name(device.get("site")) or "",
        device_type=_slug(device.get("device_type")),
        platform=platform_from_slug(_slug(device.get("platform")) or None, name),
        primary_ip4=strip_mask((device.get("primary_ip4") or {}).get("address")),
        primary_ip6=strip_mask((device.get("primary_ip6") or {}).get("address")),
        intent=device.get("config_context"),
        **{flag: _cf_flag(device, field) for flag, field in _STATUS_FLAG_FIELDS.items()},
    )


def interface_from_rest(interface: dict[str, Any]) -> InterfaceData:
    """Map a REST interface object to ``InterfaceData``.

    NOTE: the MAC is surfaced as NetBox stores it on its ``MACAddress`` model;
    canonicalization (``canonical_mac``) is applied where MACs are *compared*
    (see ``hosts.py``) rather than rewriting the value shown to callers.
    """
    vrf = interface.get("vrf") or {}
    return InterfaceData(
        name=interface["name"],
        id=str(interface["id"]),
        host=_name(interface.get("device")) or "",
        mac_address=interface.get("mac_address") or None,
        vrf_id=str(vrf["id"]) if vrf.get("id") is not None else None,
    )


def _interface_from_gql(interface: dict[str, Any], host_name: str) -> InterfaceData:
    # MAC is surfaced as NetBox's MACAddress model returns it; comparisons that
    # must be case/separator-insensitive go through ``canonical_mac`` (hosts.py).
    primary_mac = interface.get("primary_mac_address") or {}
    vrf = interface.get("vrf") or {}
    return InterfaceData(
        name=interface["name"],
        id=str(interface["id"]),
        host=host_name,
        mac_address=primary_mac.get("mac_address"),
        vrf_id=str(vrf["id"]) if vrf.get("id") is not None else None,
    )


def host_device_from_gql(device: dict[str, Any]) -> HostDeviceData:
    """Map a GraphQL device object (with device bays + interfaces) to ``HostDeviceData``."""
    name = device["name"]
    bays = [
        DeviceBayData(
            name=bay["name"],
            id=str(bay["id"]),
            installed_device_id=(
                str(bay["installed_device"]["id"]) if bay.get("installed_device") else None
            ),
        )
        for bay in (device.get("devicebays") or [])
    ]
    interfaces = [_interface_from_gql(i, name) for i in (device.get("interfaces") or [])]
    return HostDeviceData(
        id=str(device["id"]),
        name=name,
        rack=_name(device.get("rack")),
        position=device.get("position"),
        role=_slug(device.get("role")),
        site=_name(device.get("site")) or "",
        device_type=_slug(device.get("device_type")),
        serial=device.get("serial"),
        device_bays=bays,
        interfaces=interfaces,
    )


def host_metadata_from_gql(device: dict[str, Any]) -> HostMetadata:
    """Map a GraphQL device object to ``HostMetadata``.

    ``alias`` is read from the device's ``alias`` custom field (parity with the
    reference provider's ``cf_alias``). When the custom field is not selected or
    not defined on the device, it degrades to ``None``.
    """
    interfaces = []
    for iface in device.get("interfaces") or []:
        mac = (iface.get("primary_mac_address") or {}).get("mac_address")
        if not mac:
            macs = iface.get("mac_addresses") or []
            mac = macs[0]["mac_address"] if macs else None
        if mac:
            interfaces.append(HostInterfaceMetadata(name=iface["name"], mac_address=mac))
    alias = (device.get("custom_fields") or {}).get(_ALIAS_FIELD)
    return HostMetadata(
        device_id=str(device["id"]),
        name=device["name"],
        tenant=_name(device.get("tenant")) or "",
        alias=alias if isinstance(alias, str) and alias else None,
        interfaces=tuple(interfaces),
    )
