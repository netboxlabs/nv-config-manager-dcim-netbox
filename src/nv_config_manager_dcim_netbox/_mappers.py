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


def strip_mask(address: str | None) -> str | None:
    """Return the host portion of a NetBox ``address`` value (drops ``/mask``)."""
    if not address:
        return None
    return address.split("/", 1)[0]


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

    The ``*_enabled`` flags are backed by custom fields added by the NetBox
    plugin; until those exist they default to ``False``.
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
    )


def interface_from_rest(interface: dict[str, Any]) -> InterfaceData:
    """Map a REST interface object to ``InterfaceData``."""
    vrf = interface.get("vrf") or {}
    return InterfaceData(
        name=interface["name"],
        id=str(interface["id"]),
        host=_name(interface.get("device")) or "",
        mac_address=interface.get("mac_address") or None,
        vrf_id=str(vrf["id"]) if vrf.get("id") is not None else None,
    )


def _interface_from_gql(interface: dict[str, Any], host_name: str) -> InterfaceData:
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

    ``alias`` has no NetBox custom-field equivalent by default, so it is left
    ``None`` here.
    """
    interfaces = []
    for iface in device.get("interfaces") or []:
        mac = (iface.get("primary_mac_address") or {}).get("mac_address")
        if not mac:
            macs = iface.get("mac_addresses") or []
            mac = macs[0]["mac_address"] if macs else None
        if mac:
            interfaces.append(HostInterfaceMetadata(name=iface["name"], mac_address=mac))
    return HostMetadata(
        device_id=str(device["id"]),
        name=device["name"],
        tenant=_name(device.get("tenant")) or "",
        alias=None,
        interfaces=tuple(interfaces),
    )
