# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Render data assembly.

``get_render_data`` translates NetBox's native inventory into the provider-neutral
:class:`RenderData` contract the template engine consumes. As of the 2026-07-28
SDK contract, providers must return *fully typed* render concepts — there is no
generic ``intent`` blob or ``inventory`` dict; every value belongs to a typed
``Render*`` model that the common template filters read directly.

Sections populated from NetBox native data:

* ``device.identity`` — id, name, platform, role, model, full location hierarchy.
* ``device.interfaces`` — typed interfaces (type, VRF, VLANs, addresses with
  prefix ancestry, MAC, management flag).
* ``device.network`` — the device's distinct VRFs, console-server ports and
  NVLink topology.
* ``device.routing`` — BGP instances/peers from ``netbox-bgp``, grouped by ASN.
* ``device.overlays`` — L2/L3 VNIs scoped to the device via the plugin's overlay
  assignments (never the global VXLAN table).
* ``device.services`` / ``device.firmware`` — best-effort from the device's
  merged ``config_context`` where the native keys exist.
* ``location.location`` / ``location.address_space`` — the Site hierarchy plus
  site prefixes (role/tags) and VLANs (with plugin DHCP helper addresses).
* ``location.topology`` — the site's route servers (by tag) and WAN routers (by
  role), for inter-site peering.

Sections with no first-class NetBox source (ISIS/EVPN, firmware bundles,
credential references) are returned as the SDK's default empty models; they
remain optional until a filter requires them.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from typing import TYPE_CHECKING, Any

from nv_config_manager_dcim import (
    DCIMInvalidDataError,
    DCIMNotFoundError,
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

logger = logging.getLogger(__name__)

# How the reference provider selects a location's routing-relevant devices.
_ROUTE_SERVER_TAG = "route-server"
_WAN_ROLE_SLUGS = ("wan", "tan-bbr")
_LOOPBACK_PREFIX = "lo"

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
    }
    location {
      id
      name
      parent { id name parent { id name } }
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
      vrf { name rd import_targets { name } export_targets { name } }
      parent { name }
      lag { id }
      custom_fields
      untagged_vlan { vid name }
      tagged_vlans { vid name }
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
        role=choice_str(role) or None if role else None,
        parent_prefixes=tuple(str(net) for net in parents),
    )


def _render_connected_interface(
    iface: dict[str, Any], site_networks: list[Any]
) -> RenderConnectedInterface | None:
    """Build the cabling peer for one interface from its connected endpoint.

    Templates that derive routing from adjacency (e.g. the superpod plugin's
    ``vrf.j2``, which forms a BGP neighbour from ``intf.connected_interface``)
    read the peer interface's IPv4 (``peer_ipv4``) and the peer device's role
    and routing ASN. The peer ASN comes from the peer device's merged
    ``config_context.bgp.asn`` (the loader seeds it per device). Returns ``None``
    for uncabled interfaces so those never emit a neighbour.
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
            routing_asn=_str_or_none(peer_bgp.get("asn")),
        ),
    )


def _render_interface(
    iface: dict[str, Any],
    site_networks: list[Any],
    lag_members: dict[str, tuple[str, ...]] | None = None,
) -> RenderInterface:
    """Map one NetBox interface into the typed :class:`RenderInterface`.

    ``role`` comes from the ``nvcm_interface_role`` custom field (NetBox has no
    native interface role; templates gate uplink/downlink on it). ``member_interfaces``
    is the inverse of the child ``lag`` FK — the names of interfaces whose ``lag``
    points at this one — supplied precomputed in ``lag_members`` keyed by LAG id.
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
        connected_interface=_render_connected_interface(iface, site_networks),
    )


def _lag_member_map(interfaces: list[dict[str, Any]]) -> dict[str, tuple[str, ...]]:
    """Map each LAG interface id → the names of its member interfaces.

    NetBox stores the relation as the child's ``lag`` FK; the render contract
    (and the spine ``bond`` templates) want the inverse — the parent's member
    list. Order-stable by interface iteration order.
    """
    members: dict[str, list[str]] = {}
    for iface in interfaces:
        lag = iface.get("lag")
        if isinstance(lag, dict) and lag.get("id") and iface.get("name"):
            members.setdefault(str(lag["id"]), []).append(str(iface["name"]))
    return {lag_id: tuple(names) for lag_id, names in members.items()}


def _loopback_interface_name(interfaces: list[dict[str, Any]]) -> str | None:
    """Pick the device's loopback interface — the BGP router-id interface.

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


def _location_hierarchy(device: dict[str, Any]) -> tuple[RenderLocation, RenderLocation]:
    """Build ``(device_location, site_location)`` from the NetBox site/location chain."""
    site = _require(device.get("site"), "site")
    region = site.get("region")
    site_location = RenderLocation(
        id=_str_or_none(site.get("id")),
        name=str(site["name"]),
        kind="Site",
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
            kind="Location",
            parent=device_location,
        )
    return device_location, site_location


def _peer_ttl(session: dict[str, Any], group_attributes: dict[str, dict[str, Any]]) -> int | None:
    """Return a peer's multihop TTL from its own or its peer group's extra attributes.

    netbox-bgp keeps free-form settings in ``extra_attributes`` on both the
    session and the peer group; the session wins where both are set.
    """
    group = session.get("peer_group") or {}
    sources = (
        session.get("extra_attributes"),
        group_attributes.get(str(group.get("id"))),
    )
    for attributes in sources:
        if isinstance(attributes, dict) and attributes.get("ttl") is not None:
            try:
                return int(attributes["ttl"])
            except (TypeError, ValueError):
                continue
    return None


def _bgp_instances(
    sessions: list[dict[str, Any]],
    router_id_interface: str | None = None,
    peer_groups: list[dict[str, Any]] | None = None,
) -> tuple[RenderBGPInstance, ...]:
    """Group ``netbox-bgp`` sessions for a device into BGP instances keyed by local ASN.

    ``netbox-bgp`` models each peering as a ``session`` with nested ``local_as`` /
    ``remote_as`` / ``remote_address`` / ``peer_group``. It exposes no VRF
    association nor a router-id, so peers use the default source VRF and the
    router-id interface is supplied by the caller (the device loopback) — the
    ``bgp_routing_instance()`` template filter treats it as required and raises
    without it whenever a session exists.
    """
    group_attributes = {
        str(group["id"]): group.get("extra_attributes") or {}
        for group in peer_groups or []
        if group.get("id") is not None
    }
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
            local_asn, {"status": choice_str(session.get("status")) or "active", "peers": []}
        )
        remote = (session.get("remote_address") or {}).get("address")
        peer_ipv4 = peer_ipv6 = None
        if remote:
            try:
                ip = ipaddress.ip_interface(remote).ip
                if ip.version == 4:
                    peer_ipv4 = str(ip)
                else:
                    peer_ipv6 = str(ip)
            except ValueError:
                pass
        inst["peers"].append(
            RenderBGPPeer(
                name=session.get("name") or f"peer-{session.get('id')}",
                status=choice_str(session.get("status")) or "active",
                description=session.get("description") or "",
                peer_group=(session.get("peer_group") or {}).get("name") or "",
                peer_role="",
                asn=_str_or_none((session.get("remote_as") or {}).get("asn")) or "",
                peer_ipv4=peer_ipv4,
                peer_ipv6=peer_ipv6,
                ttl=_peer_ttl(session, group_attributes),
            )
        )
    return tuple(
        RenderBGPInstance(
            status=data["status"],
            asn=asn,
            router_id_interface=router_id_interface,
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

    Shared by the endpoint (host addresses) and prefix (CIDR) sets — the two
    differ only in which SDK model validates the values.
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


def _services_from_context(context: dict[str, Any]) -> RenderServicesData:
    """Typed effective network-service policy from the merged config context."""
    raw_dhcp = context.get("dhcp") if isinstance(context.get("dhcp"), dict) else {}
    dhcp = tuple(
        RenderNamedEndpointSet(
            name=str(provider), endpoints=_endpoint_set(value) or RenderEndpointSet()
        )
        for provider, value in raw_dhcp.items()
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
    router_id_interface: str | None = None,
    peer_groups: list[dict[str, Any]] | None = None,
) -> RenderRoutingData:
    """Native ``netbox-bgp`` instances plus effective routing policy (ASN/ISIS/EVPN)."""
    bgp = context.get("bgp") if isinstance(context.get("bgp"), dict) else {}
    return RenderRoutingData(
        bgp_instances=_bgp_instances(bgp_sessions, router_id_interface, peer_groups),
        default_asn=_str_or_none(bgp.get("asn")),
        local_asn=_str_or_none(bgp.get("local-asn")),
        site_asn=_str_or_none(context.get("site_asn")),
        isis_interfaces=_isis_interfaces(context),
        evpn=_evpn(context),
    )


class NetBoxRenderMixin:
    """Typed render-data assembly for the template engine."""

    async def _safe_list(self, path: str, params: list[tuple[str, str]] | None = None):
        """List a REST endpoint, treating a missing endpoint (optional plugin) as no data."""
        try:
            return await self.rest_list(path, params)
        except DCIMNotFoundError:
            return []

    async def _optional_list(self, path: str, params: list[tuple[str, str]] | None = None):
        """List an endpoint whose *filter values* may not exist in this NetBox.

        Like ``_safe_list``, but also degrades on 400: NetBox validates slug-keyed
        filters (``tag``, ``role``) against what exists, and these selections are
        optional enrichment, so a rejected slug must not fail the whole render.
        """
        try:
            return await self.rest_list(path, params)
        except DCIMNotFoundError:
            return []
        except DCIMInvalidDataError as exc:
            logger.warning(
                "NetBox rejected the optional %s selection %s (%s); treating it as empty",
                path,
                params,
                exc,
            )
            return []

    async def _assigned_overlays(
        self, device_id: str, interface_ids: set[str]
    ) -> tuple[set[str], dict[str, dict[str, Any]]]:
        """Return the overlays reaching this device, and its device-level assignments.

        The second value is keyed by overlay id and holds only assignments made to
        the device itself. Those are the ones that become ``l2_vni_vrfs``; an
        interface-level assignment is a port joining an overlay, not the device
        carrying that overlay's VRF.

        ``assigned_object_id`` is multi-valued (repeated params OR), so the device
        and all its interfaces are resolved in one query rather than one per
        interface — a switch with 64 ports made 65 round trips here.
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

    async def _id_map(self, path: str, ids: set[str]) -> dict[str, dict[str, Any]]:
        """Fetch objects by id into an ``{id: obj}`` map (empty ids → no fetch)."""
        if not ids:
            return {}
        params = [("id", i) for i in sorted(ids)]
        rows = await self._safe_list(path, params)
        return {str(row["id"]): row for row in rows if row.get("id") is not None}

    async def _overlays(self, device_id: str, interface_ids: set[str]) -> RenderOverlayData:
        """Build device-scoped L2/L3 VNI inventory from the plugin's overlays.

        Scoped via overlay *assignments* (which overlays reach this device), never
        the global VXLAN table — a device must not receive every fabric VNI.
        """
        overlay_ids, device_assignments = await self._assigned_overlays(device_id, interface_ids)
        if not overlay_ids:
            return RenderOverlayData()
        # Scope both reads to the assigned overlays server-side; fetching the whole
        # overlay and VXLAN tables to filter in Python does not scale with fabric size.
        overlay_params = [("id", i) for i in sorted(overlay_ids)]
        vxlan_params = [("overlay_id", i) for i in sorted(overlay_ids)]
        overlay_rows, vxlan_rows = await asyncio.gather(
            self._safe_list(self.OVERLAYS_PATH, overlay_params),
            self._safe_list(self.VXLANS_PATH, vxlan_params),
        )
        overlays = {str(o["id"]): o for o in overlay_rows if o.get("id") is not None}
        # Belt-and-braces: keep the client-side scope check in case an endpoint
        # ignores the filter (the whole point is that a device must not receive
        # every fabric VNI).
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
                vrf = _render_vrf(vrf_map.get(str(fk_id(vxlan.get("vrf")))))
                if vrf is None:
                    continue
                l3.append(
                    RenderL3Vni(
                        vrf=vrf,
                        l3_vlan_id=vxlan.get("l3_vlan_id"),
                        vni=vnid,
                        overlay_name=overlay_name,
                    )
                )
            else:
                vlan = _render_vlan(vlan_map.get(str(fk_id(vxlan.get("vlan")))))
                if vlan is None:
                    continue
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

    async def _loopback_addresses(self, device_id: str) -> list[dict[str, Any]]:
        """Return the IP addresses on the device's loopback interfaces.

        Two queries because the IPAddress filterset has no interface-name filter —
        only ``interface`` (exact name) and ``interface_id``. Resolve the loopbacks
        first, where ``name__isw`` is real, then select their addresses by id.
        ``interface__name__isw`` is not a filter NetBox knows, and it drops unknown
        params rather than rejecting them, so it matched every address.
        """
        interfaces = await self._safe_list(
            INTERFACES_PATH, [("device_id", device_id), ("name__isw", _LOOPBACK_PREFIX)]
        )
        interface_ids = sorted({str(i["id"]) for i in interfaces if i.get("id") is not None})
        if not interface_ids:
            return []
        # Repeated interface_id params OR, so one query covers every loopback.
        return await self._safe_list(
            IP_ADDRESSES_PATH, [("interface_id", i) for i in interface_ids]
        )

    async def _location_topology(self, site_id: str | None) -> RenderLocationTopology:
        """Route servers and WAN routers at the site, for inter-site peering.

        Selected the same way the reference provider selects them: route servers
        by the ``route-server`` tag, WAN routers by role. Both are location-scoped.

        The slugs are hardcoded and come from the plugin's ``nvcm_load_bootstrap``;
        ``_optional_list`` keeps a NetBox missing any of them from failing the render.
        """
        if not site_id:
            return RenderLocationTopology()
        route_servers, wan_routers = await asyncio.gather(
            self._optional_list(DEVICES_PATH, [("site_id", site_id), ("tag", _ROUTE_SERVER_TAG)]),
            self._optional_list(
                DEVICES_PATH,
                [("site_id", site_id), *(("role", role) for role in _WAN_ROLE_SLUGS)],
            ),
        )
        # Mapped once per distinct device, then indexed back into both tuples: a
        # border leaf can be both route-server-tagged and WAN-roled, and each
        # _location_device costs its own loopback lookup.
        by_id = {str(d["id"]): d for d in (*route_servers, *wan_routers) if d.get("id") is not None}
        order = sorted(by_id)
        mapped = dict(
            zip(
                order,
                await asyncio.gather(*(self._location_device(by_id[i]) for i in order)),
                strict=True,
            )
        )

        def selected(devices: list[dict[str, Any]]) -> tuple[RenderLocationDevice, ...]:
            return tuple(mapped[str(d["id"])] for d in devices if str(d.get("id")) in mapped)

        return RenderLocationTopology(
            route_servers=selected(route_servers),
            wan_routers=selected(wan_routers),
        )

    async def _location_device(self, device: dict[str, Any]) -> RenderLocationDevice:
        """One routing-relevant device: its name, BGP ASN and loopback addresses."""
        context = device.get("config_context") or {}
        bgp = context.get("bgp") if isinstance(context.get("bgp"), dict) else {}
        addresses = await self._loopback_addresses(str(device["id"]))
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
            self._overlays(device_id, interface_ids),
            self._location_address_space(site_id, site_prefixes if site_id else None),
            self._location_topology(site_id),
            # NVLink topology comes from the device's NVLink domain (plugin model).
            # The NVSwitch fabric-manager templates require network.nvlink_topology.
            self._safe_list(NVLINK_DOMAINS_PATH, [("device_id", str(device_id))]),
        )
        nvlink_topology = (
            _str_or_none(nvlink_domains[0].get("topology")) if nvlink_domains else None
        )

        # Sessions nest only a brief peer_group, so the groups carrying the TTL in
        # extra_attributes are fetched separately and joined by id — after the
        # sessions, scoped to the ids they reference, so this does not paginate the
        # whole fabric's peer-group table per render.
        peer_groups = list(
            (
                await self._id_map(
                    BGP_PEER_GROUPS_PATH,
                    {
                        str(pk)
                        for s in bgp_sessions
                        if (pk := fk_id(s.get("peer_group"))) is not None
                    },
                )
            ).values()
        )

        lag_members = _lag_member_map(interfaces)
        render_interfaces = tuple(
            _render_interface(i, site_networks, lag_members) for i in interfaces
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
                    bgp_sessions, context, router_id_interface, peer_groups
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
