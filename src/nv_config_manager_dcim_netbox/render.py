# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Render data assembly.

``get_render_data`` translates NetBox's native inventory into the provider-neutral
:class:`RenderData` contract the template engine consumes. As of the 2026-07-28
SDK contract, providers must return *fully typed* render concepts -- there is no
generic ``intent`` blob or ``inventory`` dict; every value belongs to a typed
``Render*`` model that the common template filters read directly.

Sections populated from NetBox native data:

* ``device.identity`` -- id, name, platform, role, model, full location hierarchy.
* ``device.interfaces`` -- typed interfaces (type, VRF, VLANs, addresses with
  prefix ancestry, MAC, management flag).
* ``device.network`` -- the device's distinct VRFs, console-server ports and
  NVLink topology.
* ``device.routing`` -- BGP instances/peers from ``netbox-bgp``, grouped by ASN.
* ``device.overlays`` -- L2/L3 VNIs scoped to the device via the plugin's overlay
  assignments (never the global VXLAN table).
* ``device.services`` / ``device.firmware`` -- best-effort from the device's
  merged ``config_context`` where the native keys exist.
* ``location.location`` / ``location.address_space`` -- the Site hierarchy plus
  site prefixes (role/tags) and VLANs (with plugin DHCP helper addresses).
* ``location.topology`` -- the site's route servers (by tag) and WAN routers (by
  role), for inter-site peering.

Sections with no first-class NetBox source (ISIS/EVPN, firmware bundles,
credential references) are returned as the SDK's default empty models; they
remain optional until a filter requires them.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
from typing import TYPE_CHECKING, Any

from nv_config_manager_dcim import (
    DCIMInvalidDataError,
    DeviceRenderData,
    IntendedInterfaceNeighbor,
    IntendedNeighborDevice,
    LocationRenderData,
    RenderAccessData,
    RenderBGPInstance,
    RenderBGPPeer,
    RenderConnectedDevice,
    RenderConnectedInterface,
    RenderConsoleServerPort,
    RenderCredentialReference,
    RenderData,
    RenderDeviceIdentity,
    RenderEndpointSet,
    RenderEvpnData,
    RenderFirmwareArtifact,
    RenderFirmwareBundle,
    RenderFirmwareComponent,
    RenderFirmwareData,
    RenderFirmwareOverrides,
    RenderInterface,
    RenderIPAddress,
    RenderIsisInterface,
    RenderL2Vni,
    RenderL2VniVrf,
    RenderL3Vni,
    RenderLocation,
    RenderLocationAddressSpace,
    RenderLocationDevice,
    RenderLocationRoutingData,
    RenderLocationTopology,
    RenderLocationVlan,
    RenderNamedEndpointSet,
    RenderNetworkData,
    RenderOverlayData,
    RenderPrefix,
    RenderPrefixSet,
    RenderRouteTarget,
    RenderRoutingData,
    RenderServicesData,
    RenderVlan,
    RenderVrf,
)

from nv_config_manager_dcim_netbox._mappers import choice_str, fk_id
from nv_config_manager_dcim_netbox.fabric_common import (
    BGP_ASN_FIELD,
    BGP_PEER_GROUPS_PATH,
    BGP_SESSIONS_PATH,
    DEVICES_PATH,
    INTERFACES_PATH,
    IP_ADDRESSES_PATH,
    NVLINK_DOMAINS_PATH,
    PREFIXES_PATH,
    VLANS_PATH,
    VRFS_PATH,
)

if TYPE_CHECKING:
    from nv_config_manager_dcim import RenderDataRequest

# Tag the reference filters location prefixes by.
_AGGREGATE_TAG = "role-aggregate"

logger = logging.getLogger(__name__)

# How the reference provider selects a location's routing-relevant devices.
_ROUTE_SERVER_TAG = "route-server"
_WAN_ROLE_SLUGS = ("wan", "tan-bbr")
_LOOPBACK_PREFIX = "lo"

# ipam.asn rows, for resolving the ``nvcm_bgp_asn`` custom field to an ASN number.
ASNS_PATH = "ipam/asns/"

# IPAddress.role is a NetBox choice field and arrives as its value (``vip``); the
# reference emits the Nautobot Role *name*, which the templates compare against
# (``role_name == "VIP"``). Keyed by IPAddressRoleChoices value; unknown values
# pass through unchanged.
_IP_ROLE_LABELS = {
    "loopback": "Loopback",
    "secondary": "Secondary",
    "anycast": "Anycast",
    "vip": "VIP",
    "vrrp": "VRRP",
    "hsrp": "HSRP",
    "glbp": "GLBP",
    "carp": "CARP",
}

_RENDER_DEVICE_QUERY = """
query ($id: ID!) {
  device(id: $id) {
    id
    name
    serial
    role { name }
    platform { name }
    device_type { model manufacturer { name } }
    tenant { name }
    tags { name }
    config_context
    consoleserverports {
      name
      connected_endpoints {
        ... on ConsolePortType {
          name
          device { name device_type { manufacturer { name } } }
        }
      }
    }
    site {
      id
      name
      slug
      region { name }
      tags { name }
    }
    location {
      id
      name
      custom_fields
      parent { id name custom_fields parent { id name custom_fields } }
    }
    interfaces {
      id
      name
      enabled
      type
      mtu
      mgmt_only
      description
      mode
      mac_addresses { mac_address }
      vrf { id name rd import_targets { name } export_targets { name } }
      parent { name }
      lag { id }
      custom_fields
      untagged_vlan { id vid name }
      tagged_vlans { id vid name }
      ip_addresses { address role }
      tags { name }
      connected_endpoints {
        ... on InterfaceType {
          name
          vrf { name }
          ip_addresses { address role }
          device {
            id
            name
            role { name }
            tenant { name }
            tags { name }
            config_context
            custom_fields
          }
        }
      }
    }
  }
}
"""

_INTENDED_NEIGHBORS_QUERY = """
query ($id: ID!) {
  device(id: $id) {
    interfaces {
      name
      enabled
      tags { name }
      connected_endpoints {
        ... on InterfaceType {
          name
          primary_mac_address { mac_address }
          device {
            name
            serial
            position
            role { name }
            rack { name }
          }
        }
      }
    }
  }
}
"""


def _require(value: Any, description: str) -> Any:
    """Return a required render value or raise an SDK-level error."""
    if not value:
        raise DCIMInvalidDataError(f"NetBox device is missing required {description} for render")
    return value


def _tags(obj: dict[str, Any] | None) -> tuple[str, ...]:
    return tuple(tag["name"] for tag in (obj or {}).get("tags") or [] if tag.get("name"))


def _str_or_none(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


def _ref_addr(value: Any) -> Any:
    """Return an address from a nested dict (``{'address': ...}``) or a bare value."""
    return value.get("address") if isinstance(value, dict) else value


def _is_ip(value: Any) -> bool:
    try:
        ipaddress.ip_address(str(value).split("/")[0])
        return True
    except ValueError:
        return False


def _console_server_port(port: dict[str, Any]) -> RenderConsoleServerPort | None:
    """Map one NetBox console-server port and whatever it is cabled to.

    The connected endpoint is the console port on the managed device, so the
    console-access templates can name the far side.
    """
    name = _str_or_none(port.get("name"))
    if not name:
        return None
    endpoint = next(iter(port.get("connected_endpoints") or []), None)
    if not isinstance(endpoint, dict):
        return RenderConsoleServerPort(name=name)
    device = endpoint.get("device") or {}
    manufacturer = (device.get("device_type") or {}).get("manufacturer") or {}
    return RenderConsoleServerPort(
        name=name,
        connected_device_name=_str_or_none(device.get("name")),
        connected_device_manufacturer=_str_or_none(manufacturer.get("name")),
        connected_port_name=_str_or_none(endpoint.get("name")),
    )


def _render_vrf(vrf: dict[str, Any] | None) -> RenderVrf | None:
    """Build a :class:`RenderVrf` from a NetBox VRF dict.

    ``import_targets`` / ``export_targets`` (route-targets) drive the EVPN
    ``route-target import/export`` stanzas; both the GraphQL ``vrf`` selection and
    the REST ``ipam/vrfs`` serializer expose them as ``[{name}]`` lists.
    """
    if not isinstance(vrf, dict) or not vrf.get("name"):
        return None
    return RenderVrf(
        name=str(vrf["name"]),
        route_distinguisher=_str_or_none(vrf.get("rd")),
        import_targets=_targets(vrf.get("import_targets")),
        export_targets=_targets(vrf.get("export_targets")),
    )


def _render_vlan(vlan: dict[str, Any] | None) -> RenderVlan | None:
    if not isinstance(vlan, dict) or vlan.get("vid") is None:
        return None
    return RenderVlan(vid=int(vlan["vid"]), name=_str_or_none(vlan.get("name")))


def _targets(values: Any) -> tuple[RenderRouteTarget, ...]:
    out: list[RenderRouteTarget] = []
    for item in values or []:
        name = item.get("name") if isinstance(item, dict) else item
        if name:
            out.append(RenderRouteTarget(name=str(name)))
    return tuple(out)


def _ip_role_label(role: Any) -> str | None:
    """Return an IP address role as its label (``VIP``), not its choice value (``vip``)."""
    if isinstance(role, dict) and role.get("label"):
        return str(role["label"])
    value = choice_str(role)
    return _IP_ROLE_LABELS.get(value, value) if value else None


def _render_ip(addr: str, role: Any, site_networks: list[Any]) -> RenderIPAddress | None:
    """Build a :class:`RenderIPAddress`, deriving host, version, and prefix ancestry.

    ``parent_prefixes`` is ordered nearest-parent-first (most specific outward),
    which the loopback / point-to-point / Spectrum-X filters rely on.
    """
    try:
        iface = ipaddress.ip_interface(addr)
    except ValueError:
        return None
    host = iface.ip
    parents = sorted(
        (net for net in site_networks if host in net),
        key=lambda net: net.prefixlen,
        reverse=True,
    )
    return RenderIPAddress(
        address=str(iface),
        host=str(host),
        version=host.version,
        role=_ip_role_label(role),
        parent_prefixes=tuple(str(net) for net in parents),
    )


def _render_connected_interface(
    iface: dict[str, Any], site_networks: list[Any], peer_asns: dict[str, str] | None = None
) -> RenderConnectedInterface | None:
    """Build the cabling peer for one interface from its connected endpoint.

    Templates that derive routing from adjacency (e.g. the superpod plugin's
    ``vrf.j2``, which forms a BGP neighbour from ``intf.connected_interface``)
    read the peer interface's IPv4 (``peer_ipv4``) and the peer device's role
    and routing ASN. The peer ASN comes from the peer device's merged
    ``config_context.bgp.asn`` (the loader seeds it per device), else from
    ``peer_asns`` -- the custom-field and session fallbacks resolved by
    ``_connected_peer_asns``, keyed by peer name. Returns ``None`` for uncabled
    interfaces so those never emit a neighbour.
    """
    endpoint = next(iter(iface.get("connected_endpoints") or []), None)
    if not endpoint:
        return None
    peer_device = endpoint.get("device") or {}
    if not peer_device.get("name"):
        return None
    peer_context = peer_device.get("config_context")
    peer_context = peer_context if isinstance(peer_context, dict) else {}
    peer_bgp = peer_context.get("bgp") if isinstance(peer_context.get("bgp"), dict) else {}
    addresses = tuple(
        filter(
            None,
            (
                _render_ip(a["address"], a.get("role"), site_networks)
                for a in endpoint.get("ip_addresses") or []
                if a.get("address")
            ),
        )
    )
    return RenderConnectedInterface(
        name=str(endpoint.get("name") or ""),
        vrf=(endpoint.get("vrf") or {}).get("name"),
        addresses=addresses,
        device=RenderConnectedDevice(
            id=_str_or_none(peer_device.get("id")),
            name=str(peer_device["name"]),
            role=(peer_device.get("role") or {}).get("name") or "",
            tenant=(peer_device.get("tenant") or {}).get("name"),
            tags=_tags(peer_device),
            routing_asn=_str_or_none(peer_bgp.get("asn"))
            or (peer_asns or {}).get(str(peer_device["name"])),
        ),
    )


def _render_interface(
    iface: dict[str, Any],
    site_networks: list[Any],
    lag_members: dict[str, tuple[str, ...]] | None = None,
    peer_asns: dict[str, str] | None = None,
) -> RenderInterface:
    """Map one NetBox interface into the typed :class:`RenderInterface`.

    ``role`` comes from the ``nvcm_interface_role`` custom field (NetBox has no
    native interface role; templates gate uplink/downlink on it). ``member_interfaces``
    is the inverse of the child ``lag`` FK -- the names of interfaces whose ``lag``
    points at this one -- supplied precomputed in ``lag_members`` keyed by LAG id.
    """
    name = _require(iface.get("name"), "interface name")
    iface_type = iface.get("type")
    if not iface_type:
        raise DCIMInvalidDataError(f"NetBox interface {name} is missing required type for render")
    macs = [m.get("mac_address") for m in iface.get("mac_addresses") or [] if m.get("mac_address")]
    addresses = tuple(
        filter(
            None,
            (
                _render_ip(a["address"], a.get("role"), site_networks)
                for a in iface.get("ip_addresses") or []
                if a.get("address")
            ),
        )
    )
    return RenderInterface(
        name=str(name),
        type=str(iface_type),
        enabled=bool(iface.get("enabled", True)),
        mtu=iface.get("mtu"),
        description=iface.get("description") or "",
        tags=_tags(iface),
        role=(iface.get("custom_fields") or {}).get("nvcm_interface_role"),
        mac_address=macs[0] if macs else None,
        vrf=_render_vrf(iface.get("vrf")),
        management_only=bool(iface.get("mgmt_only", False)),
        member_interfaces=(lag_members or {}).get(str(iface.get("id")), ()),
        parent_interface=(iface.get("parent") or {}).get("name") or None,
        untagged_vlan=_render_vlan(iface.get("untagged_vlan")),
        tagged_vlans=tuple(
            filter(None, (_render_vlan(v) for v in iface.get("tagged_vlans") or []))
        ),
        addresses=addresses,
        connected_interface=_render_connected_interface(iface, site_networks, peer_asns),
    )


def _lag_member_map(interfaces: list[dict[str, Any]]) -> dict[str, tuple[str, ...]]:
    """Map each LAG interface id -> the names of its member interfaces.

    NetBox stores the relation as the child's ``lag`` FK; the render contract
    (and the spine ``bond`` templates) want the inverse -- the parent's member
    list. Order-stable by interface iteration order.
    """
    members: dict[str, list[str]] = {}
    for iface in interfaces:
        lag = iface.get("lag")
        if isinstance(lag, dict) and lag.get("id") and iface.get("name"):
            members.setdefault(str(lag["id"]), []).append(str(iface["name"]))
    return {lag_id: tuple(names) for lag_id, names in members.items()}


def _loopback_interface_name(interfaces: list[dict[str, Any]]) -> str | None:
    """Pick the device's loopback interface -- the BGP router-id interface.

    Prefer the interface whose ``nvcm_interface_role`` custom field is ``Loopback``,
    then a conventionally-named ``lo``. Returns ``None`` if neither exists.
    """
    for iface in interfaces:
        if (iface.get("custom_fields") or {}).get("nvcm_interface_role") == "Loopback":
            return str(iface["name"])
    for iface in interfaces:
        if str(iface.get("name")) == "lo":
            return "lo"
    return None


def _device_vrfs(interfaces: list[dict[str, Any]]) -> tuple[RenderVrf, ...]:
    """Collect the distinct VRFs referenced by a device's interfaces (order-stable)."""
    seen: dict[str, RenderVrf] = {}
    for iface in interfaces:
        vrf = _render_vrf(iface.get("vrf"))
        if vrf is not None and vrf.name not in seen:
            seen[vrf.name] = vrf
    return tuple(seen.values())


def _location_kind(level: dict[str, Any]) -> str:
    """The source LocationType name for a location, else the generic kind."""
    custom = level.get("custom_fields")
    if isinstance(custom, str):
        try:
            custom = json.loads(custom)
        except ValueError:
            custom = None
    if isinstance(custom, dict) and custom.get("nvcm_location_type"):
        return str(custom["nvcm_location_type"])
    return "Location"


def _location_hierarchy(device: dict[str, Any]) -> tuple[RenderLocation, RenderLocation]:
    """Build ``(device_location, site_location)`` from the NetBox site/location chain."""
    site = _require(device.get("site"), "site")
    region = site.get("region")
    site_location = RenderLocation(
        id=_str_or_none(site.get("id")),
        name=str(site["name"]),
        kind="Site",
        tags=_tags(site),
        parent=(
            RenderLocation(name=str(region["name"]), kind="Region")
            if isinstance(region, dict) and region.get("name")
            else None
        ),
    )
    chain: list[dict[str, Any]] = []
    node = device.get("location")
    while isinstance(node, dict):
        chain.append(node)
        node = node.get("parent")
    device_location = site_location
    for level in reversed(chain):
        device_location = RenderLocation(
            id=_str_or_none(level.get("id")),
            name=str(level["name"]),
            # The source LocationType, which the reference passes through. NetBox
            # has no such field, so the loader parks it in a custom field.
            kind=_location_kind(level),
            parent=device_location,
        )
    return device_location, site_location


def _peer_group_name(peer_group: Any, group_names: dict[str, str]) -> str:
    """Return a session's peer-group name, whether nested or a bare pk."""
    if isinstance(peer_group, dict) and peer_group.get("name"):
        return str(peer_group["name"])
    return group_names.get(str(fk_id(peer_group)), "")


def _normalize_vrf_name(vrf: Any) -> str:
    """Normalize a VRF reference to a template VRF name.

    No VRF means the global routing table, which the templates call ``default``.
    Site-prefixed names (``SITE_EXIT``, unique per site in DCIM) render unprefixed.
    """
    name = vrf.get("name") if isinstance(vrf, dict) else vrf
    if not name:
        return "default"
    name = str(name)
    return name.split("_", 1)[1] if "_" in name else name


def _status_label(value: Any) -> str:
    """Return a NetBox choice's human label (``Active``), not its value (``active``).

    The render contract carries the label -- the reference provider's statuses are
    named objects, so it emits ``Active`` and the parity corpus compares against it.
    """
    if isinstance(value, dict) and value.get("label"):
        return str(value["label"])
    raw = choice_str(value)
    return raw[:1].upper() + raw[1:] if raw else ""


def _host_of(address: Any) -> str | None:
    """Return the bare host of a NetBox ``address``/``prefix`` string, or ``None``."""
    if not address:
        return None
    try:
        return str(ipaddress.ip_interface(str(address)).ip)
    except ValueError:
        return None


def _interfaces_by_address(interfaces: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index a device's interfaces by the host address of each IP they carry."""
    index: dict[str, dict[str, Any]] = {}
    for iface in interfaces:
        for address in iface.get("ip_addresses") or []:
            host = _host_of(address.get("address"))
            if host is not None:
                index.setdefault(host, iface)
    return index


def _peer_ttl(session: dict[str, Any], group_attributes: dict[str, dict[str, Any]]) -> int | None:
    """Return a peer's multihop TTL from its own or its peer group's extra attributes.

    netbox-bgp keeps free-form settings in ``extra_attributes`` on both the
    session and the peer group; the session wins where both are set.
    """
    # fk_id: netbox-bgp may serialize peer_group nested or as a bare pk.
    sources = (
        session.get("extra_attributes"),
        group_attributes.get(str(fk_id(session.get("peer_group")))),
    )
    for attributes in sources:
        if isinstance(attributes, dict) and attributes.get("ttl") is not None:
            try:
                return int(attributes["ttl"])
            except (TypeError, ValueError):
                continue
    return None


def _session_label(session: dict[str, Any], device_name: str | None) -> str:
    """Name a session in an error by device and session, as the reference does."""
    device = f"device '{device_name}'" if device_name else "device"
    return f"NetBox {device} netbox-bgp session '{session.get('name') or session.get('id')}'"


def _bgp_peer(
    session: dict[str, Any],
    by_address: dict[str, dict[str, Any]],
    peer_endpoints: dict[str, dict[str, Any]],
    group_names: dict[str, str],
    group_attributes: dict[str, dict[str, Any]],
    device_name: str | None = None,
) -> tuple[RenderBGPPeer, str]:
    """Map one ``netbox-bgp`` session to a peer, and the VRF the session runs in.

    netbox-bgp models a peering as local/remote ASN plus local/remote *addresses*;
    it names neither the interface nor the VRF, both of which the render contract
    carries. Each address supplies one half:

    * ``local_address`` is an address on this device, so the interface holding it
      gives the session's source VRF -- the reference reads the same thing off its
      local endpoint's ``source_interface.vrf``.
    * ``remote_address`` is assigned to an interface on the peer, which names the
      neighbour, its role and both its address families -- the reference's
      ``peer.routing_instance.device`` and ``peer.source_interface``, resolved
      here in ``peer_endpoints``.

    Deriving the neighbour from the *address* rather than from cabling is what
    makes a loopback-sourced session work: an iBGP/EVPN peering has no cable to
    follow, and its ``peer_group`` is exactly the field the templates interpolate.

    An address that resolves to no interface fails the render, as it does in the
    reference: unresolved, a local address would fall through to the default VRF
    and put a WAN peer in the default-VRF neighbour block, and a remote address
    leaves no peer to name.
    """
    local = session.get("local_address") or {}
    local_iface = by_address.get(_host_of(local.get("address")))
    if local.get("address") and local_iface is None:
        raise DCIMInvalidDataError(
            f"{_session_label(session, device_name)} has local address {local['address']}, "
            "which is on none of the device's interfaces, so its source VRF cannot be determined"
        )
    source_vrf = _normalize_vrf_name((local_iface or {}).get("vrf"))

    remote = session.get("remote_address") or {}
    endpoint = peer_endpoints.get(str(fk_id(remote)))
    if remote and endpoint is None:
        raise DCIMInvalidDataError(
            f"{_session_label(session, device_name)} has remote address {remote.get('address')}, "
            "which is assigned to no interface, so its peer cannot be identified"
        )
    endpoint = endpoint or {}
    peer_name = _str_or_none(endpoint.get("device_name"))
    # The peer's role doubles as its peer-group name where the session names none
    # -- the reference's fallback, and what the fabric's group naming follows.
    peer_role = str(endpoint.get("device_role") or "")
    peer_group = _peer_group_name(session.get("peer_group"), group_names) or peer_role.upper()
    if remote and not peer_group:
        # The default-VRF neighbour block interpolates peer_group unguarded, so a
        # peer with neither a group nor a role renders an invalid config, not a
        # partial one.
        raise DCIMInvalidDataError(
            f"{_session_label(session, device_name)} names no peer group, and its peer "
            f"{peer_name or remote.get('address')} has no role to fall back to"
        )

    # Both families come from the peer interface, as in the reference; the session's
    # own remote address leads so it stays the neighbour for its family.
    peer_ipv4 = peer_ipv6 = None
    for address in (remote.get("address"), *endpoint.get("addresses", ())):
        try:
            ip = ipaddress.ip_interface(str(address)).ip
        except ValueError:
            continue
        if ip.version == 4 and peer_ipv4 is None:
            peer_ipv4 = str(ip)
        elif ip.version == 6 and peer_ipv6 is None:
            peer_ipv6 = str(ip)

    name = peer_name or session.get("name") or f"peer-{session.get('id')}"
    return (
        RenderBGPPeer(
            name=str(name),
            status=_status_label(session.get("status")) or "Active",
            description=session.get("description") or str(name),
            peer_group=peer_group,
            peer_role=peer_role,
            asn=_str_or_none((session.get("remote_as") or {}).get("asn")) or "",
            peer_ipv4=peer_ipv4,
            peer_ipv6=peer_ipv6,
            source_interface=_str_or_none(endpoint.get("interface_name")),
            source_vrf=source_vrf,
            ttl=_peer_ttl(session, group_attributes),
        ),
        source_vrf,
    )


def _bgp_instances(
    sessions: list[dict[str, Any]],
    interfaces: list[dict[str, Any]] | None = None,
    router_id_interface: str | None = None,
    peer_groups: list[dict[str, Any]] | None = None,
    peer_endpoints: dict[str, dict[str, Any]] | None = None,
    device_asn: str | None = None,
    device_name: str | None = None,
) -> tuple[RenderBGPInstance, ...]:
    """Group ``netbox-bgp`` sessions for a device into BGP instances keyed by local ASN.

    ``netbox-bgp`` exposes no router-id, so the router-id interface is supplied by
    the caller (the device loopback) -- the ``bgp_routing_instance()`` template
    filter treats it as required and raises without it whenever a session exists.

    An instance's ``vrfs`` are the router-id interface's VRF plus each peer's
    source VRF: that filter selects an instance *by VRF*, so a session whose VRF
    the instance omits renders no neighbour at all.

    netbox-bgp has no per-device routing-instance object -- only sessions -- but
    the contract does, and the reference provider's exists whether or not the device
    peers with anyone. ``device_asn`` (the device's own ASN) therefore seeds a peerless
    instance where no session already carries it, so a device that has an ASN and
    no neighbours reports what the reference reports rather than nothing at all.
    ``device_name`` only names the device in errors.
    """
    group_attributes = {
        str(group["id"]): group.get("extra_attributes") or {}
        for group in peer_groups or []
        if group.get("id") is not None
    }
    group_names = {
        str(group["id"]): group.get("name") or ""
        for group in peer_groups or []
        if group.get("id") is not None
    }
    by_address = _interfaces_by_address(interfaces or [])
    router_id_vrf = _normalize_vrf_name(
        next(
            (i.get("vrf") for i in interfaces or [] if i.get("name") == router_id_interface),
            None,
        )
    )
    instances: dict[str, dict[str, Any]] = {}
    for session in sessions:
        local_asn = _str_or_none((session.get("local_as") or {}).get("asn"))
        if local_asn is None:
            # local_as is nullable in netbox-bgp and the peering cannot be rendered
            # without it. Logged, because the render otherwise succeeds with the
            # neighbour simply missing from the config.
            logger.warning(
                "Skipping netbox-bgp session %s: no local_as ASN, so it cannot be rendered",
                session.get("id"),
            )
            continue
        inst = instances.setdefault(
            local_asn,
            {
                "status": _status_label(session.get("status")) or "Active",
                "peers": [],
                "vrfs": {router_id_vrf},
            },
        )
        peer, source_vrf = _bgp_peer(
            session, by_address, peer_endpoints or {}, group_names, group_attributes, device_name
        )
        inst["peers"].append(peer)
        inst["vrfs"].add(source_vrf)
    if device_asn is not None and device_asn not in instances:
        instances[device_asn] = {"status": "Active", "peers": [], "vrfs": {router_id_vrf}}
    return tuple(
        RenderBGPInstance(
            status=data["status"],
            asn=asn,
            router_id_interface=router_id_interface,
            vrfs=tuple(sorted(data["vrfs"])),
            peers=tuple(data["peers"]),
        )
        for asn, data in instances.items()
    )


# --- config_context mappers -----------------------------------------------------
#
# NetBox merges region/site/role/platform/tag-scoped config contexts into the
# device's ``config_context`` (fetched by the render GraphQL query). These mappers
# read the same canonical keys the fabric templates expect (the keys the bundled
# ``config_contexts.yaml`` seeds), so services/firmware/access/routing populate
# from real data rather than defaults. Endpoint/prefix values are explicit
# ``{ipv4: [...], ipv6: [...]}`` mappings, not flat lists.


def _address_family_set(value: Any, model: type, described_as: str):
    """Build a ``{ipv4, ipv6}`` context value into ``model``, or ``None`` if empty.

    Shared by the endpoint (service reachability) and prefix (CIDR) sets. Only the
    SDK model validates, and the two no longer validate alike: ``RenderPrefixSet``
    is typed ``IPvAnyNetwork`` and rejects a malformed prefix, while
    ``RenderEndpointSet`` is plain ``str`` because a service endpoint may be a
    hostname rather than a literal address. So the raise below is reachable for
    prefixes and, by design, not for endpoints -- config_context values that are
    neither an address nor a resolvable name reach the templates unchallenged.
    """
    if not isinstance(value, dict):
        return None
    ipv4 = tuple(str(x) for x in value.get("ipv4") or ())
    ipv6 = tuple(str(x) for x in value.get("ipv6") or ())
    if not ipv4 and not ipv6:
        return None
    try:
        return model(ipv4=ipv4, ipv6=ipv6)
    except ValueError as exc:
        raise DCIMInvalidDataError(f"config_context {described_as}: {exc}") from exc


def _endpoint_set(value: Any) -> RenderEndpointSet | None:
    """Build a :class:`RenderEndpointSet` from a ``{ipv4, ipv6}`` context value."""
    return _address_family_set(value, RenderEndpointSet, "endpoint set has invalid addresses")


def _prefix_set(value: Any) -> RenderPrefixSet | None:
    """Build a :class:`RenderPrefixSet` from a ``{ipv4, ipv6}`` context value."""
    return _address_family_set(value, RenderPrefixSet, "prefix set has invalid prefixes")


# Reserved key under ``config_context.dhcp``: NVCM's own kea_dhcp_confgen reads
# ``dhcp.options.{interface_roles,interface_names}`` for per-subnet option
# overrides, so it is an option subtree, not the name of a DHCP server.
_DHCP_OPTIONS_KEY = "options"


def _services_from_context(context: dict[str, Any]) -> RenderServicesData:
    """Typed effective network-service policy from the merged config context."""
    raw_dhcp = context.get("dhcp") if isinstance(context.get("dhcp"), dict) else {}
    dhcp = tuple(
        RenderNamedEndpointSet(
            name=str(provider), endpoints=_endpoint_set(value) or RenderEndpointSet()
        )
        for provider, value in raw_dhcp.items()
        if provider != _DHCP_OPTIONS_KEY
    )
    return RenderServicesData(
        dns=_endpoint_set(context.get("dns")),
        ntp=_endpoint_set(context.get("ntp")),
        syslog=_endpoint_set(context.get("syslog")),
        tacacs=_endpoint_set(context.get("tacacs")),
        ztp=_endpoint_set(context.get("ztp")),
        firmware_cache=_endpoint_set(context.get("firmware_cache")),
        provisioning=_endpoint_set(context.get("provisioning_servers")),
        dhcp=dhcp,
        management_prefixes=_prefix_set(context.get("management_prefixes")),
    )


def _firmware_artifact(raw: Any) -> RenderFirmwareArtifact:
    raw = raw if isinstance(raw, dict) else {}
    return RenderFirmwareArtifact(
        version=_str_or_none(raw.get("version")),
        image_file=_str_or_none(raw.get("image_file") or raw.get("file")),
        source_path=_str_or_none(raw.get("s3_path")),
    )


def _firmware_component(name: Any, raw: Any) -> RenderFirmwareComponent:
    return RenderFirmwareComponent(name=str(name), artifact=_firmware_artifact(raw))


def _firmware_from_context(context: dict[str, Any]) -> RenderFirmwareData:
    """Desired OS + firmware bundles/overrides from the merged config context."""
    target = context.get("intended-firmware")
    desired = target.get("version") if isinstance(target, dict) else None

    bundles: list[RenderFirmwareBundle] = []
    raw_bundles = context.get("firmware_bundles")
    if isinstance(raw_bundles, dict):
        for version, raw_bundle in raw_bundles.items():
            if not isinstance(raw_bundle, dict):
                continue
            raw_components = raw_bundle.get("firmware")
            components = (
                tuple(_firmware_component(n, c) for n, c in raw_components.items())
                if isinstance(raw_components, dict)
                else ()
            )
            bundles.append(
                RenderFirmwareBundle(
                    version=str(version),
                    operating_system=_firmware_artifact(raw_bundle.get("nv_os")),
                    components=components,
                )
            )

    raw_overrides = context.get("firmware_overrides")
    raw_overrides = raw_overrides if isinstance(raw_overrides, dict) else {}
    raw_custom = raw_overrides.get("custom_components")
    raw_custom = raw_custom if isinstance(raw_custom, dict) else {}
    skip = raw_overrides.get("skip_components")
    if isinstance(skip, (str, bytes)) or not isinstance(skip, (list, tuple)):
        skip = ()
    return RenderFirmwareData(
        desired_version=_str_or_none(desired),
        selected_bundle_version=_str_or_none(context.get("firmware_bundle_version")),
        bundles=tuple(bundles),
        overrides=RenderFirmwareOverrides(
            skip_components=tuple(str(s) for s in skip),
            custom_components=tuple(_firmware_component(n, c) for n, c in raw_custom.items()),
        ),
    )


def _access_from_context(context: dict[str, Any]) -> RenderAccessData:
    """Credential *references* (never secret values) from ``password_mappings``."""
    raw = context.get("password_mappings")
    if not isinstance(raw, dict):
        return RenderAccessData()
    credentials: list[RenderCredentialReference] = []
    for username, cred in raw.items():
        if not isinstance(cred, dict):
            continue
        secret_name = cred.get("password")
        rotation = cred.get("rotation")
        if not secret_name or not rotation:
            raise DCIMInvalidDataError(
                f"config_context.password_mappings['{username}'] requires 'password' and 'rotation'"
            )
        credentials.append(
            RenderCredentialReference(
                username=str(username),
                secret_name=str(secret_name),
                rotation=str(rotation),
                role=_str_or_none(cred.get("role")),
            )
        )
    return RenderAccessData(credentials=tuple(credentials))


def _isis_interfaces(context: dict[str, Any]) -> tuple[RenderIsisInterface, ...]:
    isis = context.get("isis")
    interfaces = isis.get("interfaces") if isinstance(isis, dict) else None
    if not isinstance(interfaces, dict):
        return ()
    return tuple(
        RenderIsisInterface(
            interface_name=str(name), metric=int(metric) if metric is not None else None
        )
        for name, metric in interfaces.items()
    )


def _evpn(context: dict[str, Any]) -> RenderEvpnData:
    evpn = context.get("evpn") if isinstance(context.get("evpn"), dict) else {}
    df = evpn.get("df-preference")
    return RenderEvpnData(
        esi_base_mac=_str_or_none(context.get("evpn_esi_base_mac")),
        fabric_mac=_str_or_none(context.get("fabric-mac")),
        df_preference=int(df) if df is not None else None,
    )


def _routing_from_context(
    bgp_sessions: list[dict[str, Any]],
    context: dict[str, Any],
    interfaces: list[dict[str, Any]] | None = None,
    router_id_interface: str | None = None,
    peer_groups: list[dict[str, Any]] | None = None,
    peer_endpoints: dict[str, dict[str, Any]] | None = None,
    device_name: str | None = None,
) -> RenderRoutingData:
    """Native ``netbox-bgp`` instances plus effective routing policy (ASN/ISIS/EVPN)."""
    bgp = context.get("bgp") if isinstance(context.get("bgp"), dict) else {}
    return RenderRoutingData(
        bgp_instances=_bgp_instances(
            bgp_sessions,
            interfaces,
            router_id_interface,
            peer_groups,
            peer_endpoints,
            _str_or_none(bgp.get("asn")),
            device_name,
        ),
        default_asn=_str_or_none(bgp.get("asn")),
        local_asn=_str_or_none(bgp.get("local-asn")),
        site_asn=_str_or_none(context.get("site_asn")),
        isis_interfaces=_isis_interfaces(context),
        evpn=_evpn(context),
    )


class NetBoxRenderMixin:
    """Typed render-data assembly for the template engine."""

    # ``_safe_list`` / ``_optional_list`` live on FabricCommonMixin.

    async def _assigned_overlays(
        self, device_id: str, interface_ids: set[str]
    ) -> tuple[set[str], dict[str, dict[str, Any]]]:
        """Return the overlays reaching this device, and its device-level assignments.

        The second value is keyed by overlay id and holds only assignments made to
        the device itself. Those are the ones that become ``l2_vni_vrfs``; an
        interface-level assignment is a port joining an overlay, not the device
        carrying that overlay's VRF.

        ``assigned_object_id`` is multi-valued (repeated params OR), so the device
        and all its interfaces resolve in one query rather than one per interface.
        """
        object_ids = [str(device_id), *sorted(interface_ids)]
        rows = await self._safe_list(
            self.ASSIGNMENTS_PATH, [("assigned_object_id", i) for i in object_ids]
        )
        assignments: dict[Any, dict[str, Any]] = {
            row["id"]: row for row in rows if row.get("id") is not None
        }
        overlay_ids: set[str] = set()
        device_assignments: dict[str, dict[str, Any]] = {}
        for assignment in assignments.values():
            ctype = assignment.get("assigned_object_type")
            assigned_id = str(assignment.get("assigned_object_id"))
            matches_device = ctype == "dcim.device" and assigned_id == str(device_id)
            matches_iface = ctype == "dcim.interface" and assigned_id in interface_ids
            if matches_device or matches_iface:
                overlay_id = fk_id(assignment.get("overlay"))
                if overlay_id is not None:
                    overlay_ids.add(str(overlay_id))
                    if matches_device:
                        device_assignments[str(overlay_id)] = assignment
        return overlay_ids, device_assignments

    async def _bgp_peer_endpoints(
        self, sessions: list[dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        """Resolve each session's ``remote_address`` to the peer it identifies.

        Returned keyed by remote-address id: ``{interface_name, device_name,
        device_role, addresses}``. The address serializer nests the interface and
        its brief device, so the first read names both. The role needs a devices
        read, because no brief serializer carries it; ``addresses`` -- every
        address on the peer interface, from which both peer address families are
        derived as the reference does -- needs an addresses read by interface. All
        are id-filtered, and skipped entirely for a device with no sessions, which
        is most of a fabric.
        """
        address_ids = {
            str(pk)
            for session in sessions
            if (pk := fk_id(session.get("remote_address"))) is not None
        }
        if not address_ids:
            return {}
        addresses = await self._id_map(IP_ADDRESSES_PATH, address_ids)
        assigned = {
            address_id: interface
            for address_id, address in addresses.items()
            if isinstance(interface := address.get("assigned_object"), dict)
            and address.get("assigned_object_type") == "dcim.interface"
        }
        devices, interface_addresses = await asyncio.gather(
            self._id_map(
                DEVICES_PATH,
                {
                    str(pk)
                    for interface in assigned.values()
                    if (pk := fk_id(interface.get("device"))) is not None
                },
            ),
            self._interface_addresses(
                {str(i["id"]) for i in assigned.values() if i.get("id") is not None}
            ),
        )
        endpoints: dict[str, dict[str, Any]] = {}
        for address_id, interface in assigned.items():
            brief = interface.get("device") if isinstance(interface.get("device"), dict) else {}
            device = devices.get(str(fk_id(interface.get("device")))) or {}
            endpoints[address_id] = {
                "interface_name": interface.get("name"),
                "device_name": device.get("name") or brief.get("name"),
                "device_role": (device.get("role") or {}).get("name"),
                "addresses": interface_addresses.get(str(interface.get("id")), ()),
            }
        return endpoints

    async def _interface_addresses(self, interface_ids: set[str]) -> dict[str, tuple[str, ...]]:
        """Addresses on each interface, keyed by interface id (empty ids -> no fetch)."""
        if not interface_ids:
            return {}
        rows = await self._safe_list(
            IP_ADDRESSES_PATH, [("interface_id", i) for i in sorted(interface_ids)]
        )
        out: dict[str, list[str]] = {}
        for row in rows:
            # Nested when the serializer expands it, a bare id otherwise.
            owner = str(
                (row.get("assigned_object") or {}).get("id") or row.get("assigned_object_id")
            )
            if owner in interface_ids and row.get("address"):
                out.setdefault(owner, []).append(str(row["address"]))
        return {owner: tuple(found) for owner, found in out.items()}

    async def _connected_peer_asns(
        self,
        interfaces: list[dict[str, Any]],
        sessions: list[dict[str, Any]],
        peer_endpoints: dict[str, dict[str, Any]],
    ) -> dict[str, str]:
        """Routing ASNs for cabled peers with no ``config_context.bgp.asn``, by peer name.

        The reference reads a peer's BGP routing instances before its config
        context. netbox-bgp has no instance object, so after the context (read
        directly by ``_render_connected_interface``) the fallbacks are the peer's
        ``nvcm_bgp_asn`` custom field -- an ``ipam.asn`` reference, a bare pk over
        GraphQL and a nested object over REST -- and, failing that, the remote ASN
        this device's own sessions already state for that peer. Without them a peer
        whose ASN exists only as netbox-bgp data renders ``remote-as external``.
        """
        asns: dict[str, str] = {}
        pks: dict[str, str] = {}
        for iface in interfaces:
            endpoint = next(iter(iface.get("connected_endpoints") or []), None)
            peer = (endpoint or {}).get("device") or {}
            name = _str_or_none(peer.get("name"))
            if name is None or name in asns or name in pks:
                continue
            context = peer.get("config_context")
            bgp = context.get("bgp") if isinstance(context, dict) else None
            if isinstance(bgp, dict) and _str_or_none(bgp.get("asn")) is not None:
                continue
            value = (peer.get("custom_fields") or {}).get(BGP_ASN_FIELD)
            if isinstance(value, dict) and value.get("asn") is not None:
                asns[name] = str(value["asn"])
            elif (pk := fk_id(value)) is not None:
                pks[name] = str(pk)
        resolved = await self._id_map(ASNS_PATH, set(pks.values()))
        for name, pk in pks.items():
            asn = _str_or_none((resolved.get(pk) or {}).get("asn"))
            if asn is not None:
                asns[name] = asn
        for session in sessions:
            endpoint = peer_endpoints.get(str(fk_id(session.get("remote_address")))) or {}
            name = _str_or_none(endpoint.get("device_name"))
            asn = _str_or_none((session.get("remote_as") or {}).get("asn"))
            if name is not None and asn is not None:
                asns.setdefault(name, asn)
        return asns

    async def _l3_vxlans_for_vrfs(self, vrf_ids: set[str]) -> list[dict[str, Any]]:
        """L3 VXLANs whose VRF this device carries -- the reference's scoping."""
        if not vrf_ids:
            return []
        rows = await self._safe_list(self.VXLANS_PATH, [("vrf_id", i) for i in sorted(vrf_ids)])
        return [r for r in rows if r.get("vni_type") == "l3"]

    async def _l2_vxlans_for_vlans(self, vlan_ids: set[str]) -> list[dict[str, Any]]:
        """L2 VXLANs for VLANs attached to this device's interfaces.

        The reference admits an L2 VNI when its overlay is assigned to the device
        *or* its VLAN is on one of the device's ports. Assignment alone misses a
        VLAN trunked to a port whose overlay was never assigned, and the template's
        ``vni_mappings`` filter raises on the first such VLAN it cannot resolve.
        """
        if not vlan_ids:
            return []
        rows = await self._safe_list(self.VXLANS_PATH, [("vlan_id", i) for i in sorted(vlan_ids)])
        # Re-checked in Python: an endpoint that ignores the filter must not hand
        # this device every L2 VNI in the fabric.
        return [
            r for r in rows if r.get("vni_type") != "l3" and str(fk_id(r.get("vlan"))) in vlan_ids
        ]

    async def _id_map(self, path: str, ids: set[str]) -> dict[str, dict[str, Any]]:
        """Fetch objects by id into an ``{id: obj}`` map (empty ids -> no fetch)."""
        if not ids:
            return {}
        params = [("id", i) for i in sorted(ids)]
        rows = await self._safe_list(path, params)
        return {str(row["id"]): row for row in rows if row.get("id") is not None}

    async def _overlays(
        self,
        device_id: str,
        interface_ids: set[str],
        vrf_ids: set[str] | None = None,
        vlan_ids: set[str] | None = None,
    ) -> RenderOverlayData:
        """Build device-scoped L2/L3 VNI inventory from the plugin's overlays.

        Three scopes, matching the reference: an L2 VNI counts when its overlay is
        assigned to the device *or* its VLAN is on one of the device's ports; an L3
        VNI when the device carries its VRF. L3 overlays are never assigned to
        devices, so assignment-only scoping dropped every L3 VNI and with it the
        rendered `evpn` block.
        """
        overlay_ids, device_assignments = await self._assigned_overlays(device_id, interface_ids)
        l3_rows, l2_rows = await asyncio.gather(
            self._l3_vxlans_for_vrfs(vrf_ids or set()),
            self._l2_vxlans_for_vlans(vlan_ids or set()),
        )
        if not overlay_ids and not l3_rows and not l2_rows:
            return RenderOverlayData()
        # Scope both reads to the assigned overlays server-side; fetching the whole
        # overlay and VXLAN tables to filter in Python does not scale with fabric size.
        overlay_params = [("id", i) for i in sorted(overlay_ids)]
        vxlan_params = [("overlay_id", i) for i in sorted(overlay_ids)]
        overlay_rows, vxlan_rows = (
            await asyncio.gather(
                self._safe_list(self.OVERLAYS_PATH, overlay_params),
                self._safe_list(self.VXLANS_PATH, vxlan_params),
            )
            if overlay_ids
            else ([], [])
        )
        # The VRF- and VLAN-scoped rows carry overlays of their own, which the name
        # lookup needs.
        scoped_rows = [*l3_rows, *l2_rows]
        seen = {str(v.get("id")) for v in vxlan_rows}
        vxlan_rows = [*vxlan_rows, *(r for r in scoped_rows if str(r.get("id")) not in seen)]
        extra_overlay_ids = {
            str(fk_id(v.get("overlay"))) for v in scoped_rows if v.get("overlay")
        } - overlay_ids
        if extra_overlay_ids:
            overlay_rows = [
                *overlay_rows,
                *await self._safe_list(
                    self.OVERLAYS_PATH, [("id", i) for i in sorted(extra_overlay_ids)]
                ),
            ]
            overlay_ids = overlay_ids | extra_overlay_ids
        overlays = {str(o["id"]): o for o in overlay_rows if o.get("id") is not None}
        # Re-checked here in case an endpoint ignores the filter: a device must
        # not receive every fabric VNI.
        vxlans = [v for v in vxlan_rows if str(fk_id(v.get("overlay"))) in overlay_ids]
        vrf_map, vlan_map = await asyncio.gather(
            self._id_map(VRFS_PATH, {str(fk_id(v.get("vrf"))) for v in vxlans if v.get("vrf")}),
            self._id_map(VLANS_PATH, {str(fk_id(v.get("vlan"))) for v in vxlans if v.get("vlan")}),
        )

        l2: list[RenderL2Vni] = []
        l3: list[RenderL3Vni] = []
        l2_by_overlay: dict[str, tuple[RenderL2Vni, dict[str, Any]]] = {}
        for vxlan in vxlans:
            overlay_name = _str_or_none(
                (overlays.get(str(fk_id(vxlan.get("overlay")))) or {}).get("name")
            )
            vnid = vxlan.get("vnid")
            if vxlan.get("vni_type") == "l3":
                # Two different failures, which must not share an outcome:
                #
                # No VRF named at all is legal -- VXLAN.vrf is nullable and the
                # plugin's clean() does not require it per vni_type -- so it is
                # incomplete data, logged and skipped.
                #
                # A VRF named but *unresolvable* means the follow-up id query did
                # not return a row the VXLAN points at. Skipping there would emit a
                # config silently missing this VNI, which is then deployed to the
                # switch; the reference provider raises in the same place
                # (dcim-nautobot-2x render.py, "L3 VNI is missing its VRF
                # assignment") and so do we.
                vrf_ref = fk_id(vxlan.get("vrf"))
                if vrf_ref is None:
                    logger.warning(
                        "Skipping L3 VNI from VXLAN %s on device %s: it names no VRF",
                        vxlan.get("id"),
                        device_id,
                    )
                    continue
                vrf = _render_vrf(vrf_map.get(str(vrf_ref)))
                if vrf is None:
                    raise DCIMInvalidDataError(
                        f"NetBox device {device_id} L3 VNI (VXLAN {vxlan.get('id')}) references "
                        f"VRF {vrf_ref}, which could not be resolved"
                    )
                l3.append(
                    RenderL3Vni(
                        vrf=vrf,
                        l3_vlan_id=vxlan.get("l3_vlan_id"),
                        vni=vnid,
                        overlay_name=overlay_name,
                    )
                )
            else:
                # Same split as the L3 branch above.
                vlan_ref = fk_id(vxlan.get("vlan"))
                if vlan_ref is None:
                    logger.warning(
                        "Skipping L2 VNI from VXLAN %s on device %s: it names no VLAN",
                        vxlan.get("id"),
                        device_id,
                    )
                    continue
                vlan = _render_vlan(vlan_map.get(str(vlan_ref)))
                if vlan is None:
                    raise DCIMInvalidDataError(
                        f"NetBox device {device_id} L2 VNI (VXLAN {vxlan.get('id')}) references "
                        f"VLAN {vlan_ref}, which could not be resolved"
                    )
                l2_vni = RenderL2Vni(
                    vlan=vlan,
                    vni=vnid,
                    overlay_name=overlay_name,
                    import_targets=_targets(vxlan.get("import_targets")),
                    export_targets=_targets(vxlan.get("export_targets")),
                )
                l2.append(l2_vni)
                l2_by_overlay.setdefault(str(fk_id(vxlan.get("overlay"))), (l2_vni, vxlan))

        # One entry per overlay assigned to the device itself, naming the L2 VNI
        # whose VRF the device carries. Route targets come from the VXLAN, and
        # fall back to the per-assignment overrides when it declares none.
        l2_vni_vrfs: list[RenderL2VniVrf] = []
        for overlay_id, assignment in device_assignments.items():
            entry = l2_by_overlay.get(overlay_id)
            if entry is None:
                continue
            l2_vni, _vxlan = entry
            name = l2_vni.overlay_name or _str_or_none((overlays.get(overlay_id) or {}).get("name"))
            if name is None:
                continue
            import_targets = l2_vni.import_targets
            export_targets = l2_vni.export_targets
            if not import_targets and not export_targets:
                import_targets = _targets(assignment.get("import_targets"))
                export_targets = _targets(assignment.get("export_targets"))
            l2_vni_vrfs.append(
                RenderL2VniVrf(
                    name=name,
                    vni=l2_vni.vni,
                    import_targets=import_targets,
                    export_targets=export_targets,
                )
            )
        return RenderOverlayData(
            l2_vnis=tuple(l2), l3_vnis=tuple(l3), l2_vni_vrfs=tuple(l2_vni_vrfs)
        )

    async def _loopback_addresses_by_device(
        self, device_ids: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        """Loopback IP addresses for several devices, keyed by device id.

        Two queries whatever the device count: ``device_id`` and ``interface_id``
        are both multi-valued. The IPAddress filterset has no interface-name
        filter, so the loopbacks are resolved first -- ``interface__name__isw`` is
        not a filter NetBox knows, and it drops unknown params rather than
        rejecting them.
        """
        if not device_ids:
            return {}
        interfaces = await self._safe_list(
            INTERFACES_PATH,
            [("device_id", i) for i in sorted(set(device_ids))] + [("name__isw", _LOOPBACK_PREFIX)],
        )
        device_by_interface = {
            str(i["id"]): str(fk_id(i.get("device")))
            for i in interfaces
            if i.get("id") is not None and fk_id(i.get("device")) is not None
        }
        if not device_by_interface:
            return {}
        addresses = await self._safe_list(
            IP_ADDRESSES_PATH, [("interface_id", i) for i in sorted(device_by_interface)]
        )
        out: dict[str, list[dict[str, Any]]] = {}
        for address in addresses:
            # Nested when the serializer expands it, a bare id otherwise.
            owner = (address.get("assigned_object") or {}).get("id") or address.get(
                "assigned_object_id"
            )
            device_id = device_by_interface.get(str(owner))
            if device_id is not None:
                out.setdefault(device_id, []).append(address)
        return out

    async def _loopback_addresses(self, device_id: str) -> list[dict[str, Any]]:
        """Loopback IP addresses for one device."""
        by_device = await self._loopback_addresses_by_device([str(device_id)])
        return by_device.get(str(device_id), [])

    async def _location_topology(self, site_id: str | None) -> RenderLocationTopology:
        """Route servers and WAN routers at the site, for inter-site peering.

        Selected the same way the reference provider selects them: route servers
        by the ``route-server`` tag, WAN routers by role. Both are location-scoped.

        The slugs are hardcoded and come from the plugin's ``nvcm_load_bootstrap``;
        ``_optional_list`` keeps a NetBox missing any of them from failing the render.
        """
        if not site_id:
            return RenderLocationTopology()
        # One query per WAN role: _optional_list degrades the whole query on a 400,
        # so combining them lets one undefined role discard the other's devices.
        route_servers, *wan_router_pages = await asyncio.gather(
            self._optional_list(DEVICES_PATH, [("site_id", site_id), ("tag", _ROUTE_SERVER_TAG)]),
            *(
                self._optional_list(DEVICES_PATH, [("site_id", site_id), ("role", role)])
                for role in _WAN_ROLE_SLUGS
            ),
        )
        # Deduped by id: the queries are independent, so a device carrying more
        # than one WAN role comes back from each of them.
        wan_routers = list(
            {
                str(row["id"]): row
                for page in wan_router_pages
                for row in page
                if row.get("id") is not None
            }.values()
        )
        # Mapped once per distinct device, then indexed back into both tuples: a
        # border leaf can be both route-server-tagged and WAN-roled, and each
        # _location_device costs its own loopback lookup.
        by_id = {str(d["id"]): d for d in (*route_servers, *wan_routers) if d.get("id") is not None}
        order = sorted(by_id)
        loopbacks = await self._loopback_addresses_by_device(order)
        mapped = {i: self._location_device(by_id[i], loopbacks.get(i, [])) for i in order}

        def selected(devices: list[dict[str, Any]]) -> tuple[RenderLocationDevice, ...]:
            return tuple(mapped[str(d["id"])] for d in devices if str(d.get("id")) in mapped)

        return RenderLocationTopology(
            route_servers=selected(route_servers),
            wan_routers=selected(wan_routers),
        )

    def _location_device(
        self, device: dict[str, Any], addresses: list[dict[str, Any]]
    ) -> RenderLocationDevice:
        """One routing-relevant device: its name, BGP ASN and loopback addresses."""
        context = device.get("config_context") or {}
        bgp = context.get("bgp") if isinstance(context.get("bgp"), dict) else {}
        loopbacks: list[str] = []
        for entry in addresses:
            address = _str_or_none(entry.get("address"))
            if address is None:
                continue
            try:
                loopbacks.append(str(ipaddress.ip_interface(address).ip))
            except ValueError:
                continue
        return RenderLocationDevice(
            name=str(device.get("name") or ""),
            routing_asn=_str_or_none(bgp.get("asn")),
            loopback_addresses=tuple(loopbacks),
        )

    async def _location_address_space(
        self, site_id: str | None, prefixes: list[dict[str, Any]] | None = None
    ) -> RenderLocationAddressSpace:
        """Site prefixes (role/tags) and VLANs (with plugin DHCP helper addresses).

        ``prefixes`` may be supplied by a caller that has already fetched the
        site's prefixes (``get_render_data`` needs them for address ancestry), so
        the same query is not issued twice per render.
        """
        if not site_id:
            return RenderLocationAddressSpace()
        if prefixes is None:
            prefixes = await self._safe_list(PREFIXES_PATH, [("site_id", site_id)])
        # The reference scopes this to the aggregates
        # (`prefixes(tags: "role-aggregate", location: $location)`); unfiltered,
        # superpod put 164 prefixes in every render payload instead of 12.
        prefixes = [p for p in prefixes if _AGGREGATE_TAG in _tags(p)]
        vlans = await self._safe_list(VLANS_PATH, [("site_id", site_id)])
        render_prefixes = tuple(
            RenderPrefix(
                prefix=p["prefix"],
                # A Prefix role is a related object ({name, slug}), NOT a choice
                # field ({value, label}); use the name so the `site_aggregates`
                # template filter (matches role by name) can find it.
                role=_str_or_none((p.get("role") or {}).get("name")),
                tags=_tags(p),
            )
            for p in prefixes
            if p.get("prefix")
        )
        render_vlans: list[RenderLocationVlan] = []
        for vlan in vlans:
            render_vlan = _render_vlan(vlan)
            if render_vlan is None:
                continue
            helpers = (vlan.get("custom_fields") or {}).get("nvcm_helper_addresses") or []
            helper_ips = tuple(
                str(ipaddress.ip_address(str(_ref_addr(h)).split("/")[0]))
                for h in helpers
                if _ref_addr(h) and _is_ip(_ref_addr(h))
            )
            render_vlans.append(RenderLocationVlan(vlan=render_vlan, helper_addresses=helper_ips))
        return RenderLocationAddressSpace(prefixes=render_prefixes, vlans=tuple(render_vlans))

    async def get_render_data(self, request: RenderDataRequest) -> RenderData:
        """Assemble the provider-neutral, fully typed render payload for one device."""
        device_id = request.device_id
        data = await self.graphql_query(_RENDER_DEVICE_QUERY, {"id": device_id})
        device = _require(data.get("device"), "device")

        platform = _require(device.get("platform"), "platform")
        role = _require(device.get("role"), "role")
        device_type = _require(device.get("device_type"), "device type")
        interfaces = list(device.get("interfaces") or [])
        interface_ids = {str(i["id"]) for i in interfaces if i.get("id")}
        # The device's VRFs, which scope its L3 VNIs.
        device_vrf_ids = {str(fk_id(i.get("vrf"))) for i in interfaces if i.get("vrf") is not None}
        # The VLANs on its ports, which -- with overlay assignment -- scope its L2 VNIs.
        device_vlan_ids = {
            str(vlan["id"])
            for i in interfaces
            for vlan in (i.get("untagged_vlan"), *(i.get("tagged_vlans") or []))
            if isinstance(vlan, dict) and vlan.get("id") is not None
        }
        context = device.get("config_context") or {}

        device_location, site_location = _location_hierarchy(device)
        site_id = site_location.id

        # Site prefixes double as the ancestry source for interface addresses, so
        # they are fetched once here and handed to _location_address_space rather
        # than queried again there.
        site_prefixes = (
            await self._safe_list(PREFIXES_PATH, [("site_id", site_id)]) if site_id else []
        )
        site_networks = [
            ipaddress.ip_network(p["prefix"], strict=False)
            for p in site_prefixes
            if p.get("prefix")
        ]

        # These reads are independent of one another; run them concurrently rather
        # than serialising a round trip each per render.
        (
            bgp_sessions,
            overlays,
            address_space,
            topology,
            nvlink_domains,
        ) = await asyncio.gather(
            self._safe_list(BGP_SESSIONS_PATH, [("device_id", str(device_id))]),
            self._overlays(device_id, interface_ids, device_vrf_ids, device_vlan_ids),
            self._location_address_space(site_id, site_prefixes if site_id else None),
            self._location_topology(site_id),
            # NVLink topology comes from the device's NVLink domain (plugin model).
            # The NVSwitch fabric-manager templates require network.nvlink_topology.
            self._safe_list(NVLINK_DOMAINS_PATH, [("device_id", str(device_id))]),
        )
        nvlink_topology = (
            _str_or_none(nvlink_domains[0].get("topology")) if nvlink_domains else None
        )

        # Sessions nest only brief peer_group / remote_address objects, so the
        # groups carrying the TTL in extra_attributes and the interfaces the remote
        # addresses land on are resolved separately -- after the sessions, scoped to
        # the ids they reference, so neither paginates a fabric-wide table per
        # render, and both are skipped for a device with no sessions.
        peer_group_map, peer_endpoints = await asyncio.gather(
            self._id_map(
                BGP_PEER_GROUPS_PATH,
                {str(pk) for s in bgp_sessions if (pk := fk_id(s.get("peer_group"))) is not None},
            ),
            self._bgp_peer_endpoints(bgp_sessions),
        )
        peer_groups = list(peer_group_map.values())
        peer_asns = await self._connected_peer_asns(interfaces, bgp_sessions, peer_endpoints)

        lag_members = _lag_member_map(interfaces)
        render_interfaces = tuple(
            _render_interface(i, site_networks, lag_members, peer_asns) for i in interfaces
        )
        router_id_interface = _loopback_interface_name(interfaces)

        return RenderData(
            device=DeviceRenderData(
                identity=RenderDeviceIdentity(
                    id=str(device["id"]),
                    name=str(_require(device.get("name"), "name")),
                    platform=str(platform["name"]),
                    role=str(role["name"]),
                    model=str(_require(device_type.get("model"), "device model")),
                    location=device_location,
                    tags=_tags(device),
                ),
                interfaces=render_interfaces,
                network=RenderNetworkData(
                    vrfs=_device_vrfs(interfaces),
                    nvlink_topology=nvlink_topology,
                    console_server_ports=tuple(
                        mapped
                        for port in device.get("consoleserverports") or []
                        if (mapped := _console_server_port(port)) is not None
                    ),
                ),
                routing=_routing_from_context(
                    bgp_sessions,
                    context,
                    interfaces,
                    router_id_interface,
                    peer_groups,
                    peer_endpoints,
                    _str_or_none(device.get("name")),
                ),
                overlays=overlays,
                firmware=_firmware_from_context(context),
                services=_services_from_context(context),
                access=_access_from_context(context),
            ),
            location=LocationRenderData(
                location=site_location,
                routing=RenderLocationRoutingData(site_asn=_str_or_none(context.get("site_asn"))),
                address_space=address_space,
                topology=topology,
            ),
        )

    async def get_intended_interface_neighbors(
        self, device_id: str
    ) -> list[IntendedInterfaceNeighbor]:
        data = await self.graphql_query(_INTENDED_NEIGHBORS_QUERY, {"id": device_id})
        device = data.get("device")
        if not device:
            return []
        neighbors: list[IntendedInterfaceNeighbor] = []
        for interface in device.get("interfaces") or []:
            if not interface.get("enabled", True):
                continue
            tags = _tags(interface)
            endpoint = next(iter(interface.get("connected_endpoints") or []), None)
            if not endpoint:
                neighbors.append(IntendedInterfaceNeighbor(name=interface["name"], tags=tags))
                continue
            peer_device = endpoint.get("device") or {}
            if not peer_device.get("name"):
                raise DCIMInvalidDataError(
                    f"NetBox connected endpoint for {interface['name']} has no device name"
                )
            neighbors.append(
                IntendedInterfaceNeighbor(
                    name=interface["name"],
                    tags=tags,
                    connected_interface_name=endpoint.get("name"),
                    connected_interface_mac=(endpoint.get("primary_mac_address") or {}).get(
                        "mac_address"
                    ),
                    connected_device=IntendedNeighborDevice(
                        name=peer_device["name"],
                        serial=peer_device.get("serial"),
                        role=(peer_device.get("role") or {}).get("name"),
                        rack=(peer_device.get("rack") or {}).get("name"),
                        position=peer_device.get("position"),
                    ),
                )
            )
        return neighbors
