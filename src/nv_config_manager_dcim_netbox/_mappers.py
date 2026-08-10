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
    """Return a canonical MAC string; unparseable input is returned unchanged.

    Normalizes to a lowercase, colon-separated, zero-padded form so MAC
    comparisons are case- and separator-insensitive (``AA-BB-CC-00-00-01`` and
    ``aa:bb:cc:0:0:1`` both canonicalize to ``aa:bb:cc:00:00:01``). NetBox itself
    already stores MACs on a first-class ``MACAddress`` model, so this is used at
    matching boundaries (e.g. deduplicating an interface's MACs) rather than to
    rewrite values NetBox returns for display.

    Empty input returns ``None``. A value netaddr cannot parse is returned as
    supplied rather than dropped, so a malformed MAC still compares equal to an
    identical malformed MAC instead of collapsing every bad value onto ``None``.
    """
    if not value:
        return None
    try:
        return str(netaddr.EUI(value, dialect=netaddr.mac_unix_expanded))
    except (netaddr.AddrFormatError, ValueError, TypeError):
        return value


# --- NetBox response-shape helpers -----------------------------------------------
#
# NetBox returns a related object as a nested brief dict in most serializers but
# as a bare PK in some (notably plugin write serializers), and a choice field as
# ``{"value": …, "label": …}`` in REST but a bare string in GraphQL. These read
# either shape so call sites do not each re-implement the check.


def fk_id(value: Any) -> Any:
    """Return an id from either a nested object dict (``{"id": …}``) or a bare PK."""
    return value.get("id") if isinstance(value, dict) else value


def choice_value(value: Any) -> Any:
    """Return a NetBox choice's raw value (``{"value": …}``) or the value itself.

    Preserves the value's type — use this when comparing against a non-string
    (e.g. ``family`` against ``4``). See :func:`choice_str` for the string form.
    """
    return value.get("value") if isinstance(value, dict) else value


def choice_str(value: Any) -> str:
    """Normalize a NetBox status/choice (``{"value": …}`` or a bare string) to ``str``."""
    if isinstance(value, dict):
        return str(value.get("value") or value.get("label") or "")
    return str(value or "")


def tag_names(obj: dict[str, Any] | None) -> set[str]:
    """Return the tag names *and* slugs on an object (callers may match on either)."""
    names: set[str] = set()
    for tag in (obj or {}).get("tags") or []:
        if isinstance(tag, dict):
            for key in ("name", "slug"):
                value = tag.get(key)
                if isinstance(value, str) and value:
                    names.add(value)
    return names


def _cf_flag(device: dict[str, Any], field: str) -> bool:
    """Read a boolean NVCM status custom field, defaulting absent/None to False."""
    return bool((device.get("custom_fields") or {}).get(field))


def slug_of(obj: Any) -> str:
    """Return a nested object's slug, or ``""`` (NetBox roles/platforms key by slug)."""
    return (obj or {}).get("slug") or ""


def status_slug(value: Any) -> str:
    """Normalize a device-status filter value to the NetBox slug.

    NetBox's ``status`` query filter matches the status *slug*, which for device
    statuses is the lowercased single-word label (``active``, ``provisioned``,
    ``planned`` …). NVCM workflows, however, default their status filter to the
    display label the reference DCIM provider uses (e.g. ``["Active",
    "Provisioned"]``); forwarding that verbatim makes NetBox reject the request
    with HTTP 400 (``"Active is not one of the available choices"``), which stalls
    every site-scoped selection (cable/hardware validation, site backup, site
    password rotation). Lowercasing maps label -> slug and is idempotent for a
    value that is already a slug.
    """
    return str(value).lower()


def name_of(obj: Any) -> str | None:
    """Return a nested object's name, or ``None``."""
    return (obj or {}).get("name") if obj else None


def require_device_name(device: dict[str, Any]) -> str:
    """Return a device's name, raising when NetBox has none.

    ``Device.name`` is nullable in NetBox, but every workflow model built here
    keys devices by name, so an unnamed device cannot be represented. Raising the
    SDK error (rather than a ``KeyError``) lets single-device reads report a
    proper provider failure and lets list reads skip the row — both paths already
    handle ``DCIMInvalidDataError``.
    """
    name = device.get("name")
    if not name:
        raise DCIMInvalidDataError(
            f"NetBox device {device.get('id')} has no name; it cannot be identified by name"
        )
    return str(name)


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
        name=require_device_name(device),
        site=name_of(device.get("site")) or "Unknown",
        platform=name_of(device.get("platform")),
        role=name_of(device.get("role")),
        rack=name_of(device.get("rack")),
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
    name = require_device_name(device)
    return NetworkDeviceData(
        id=str(device["id"]),
        name=name,
        rack=name_of(device.get("rack")),
        position=device.get("position"),
        role=slug_of(device.get("role")),
        site=name_of(device.get("site")) or "",
        device_type=slug_of(device.get("device_type")),
        platform=platform_from_slug(slug_of(device.get("platform")) or None, name),
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
        host=name_of(interface.get("device")) or "",
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
    name = require_device_name(device)
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
        rack=name_of(device.get("rack")),
        position=device.get("position"),
        role=slug_of(device.get("role")),
        site=name_of(device.get("site")) or "",
        device_type=slug_of(device.get("device_type")),
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
        name=require_device_name(device),
        tenant=name_of(device.get("tenant")) or "",
        alias=alias if isinstance(alias, str) and alias else None,
        interfaces=tuple(interfaces),
    )
