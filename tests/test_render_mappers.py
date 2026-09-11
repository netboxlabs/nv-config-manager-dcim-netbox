# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the individual native-to-SDK render mappers.

Covers each mapper helper's malformed / missing-data path directly (checklist
item 1), and asserts the SDK render models reject unknown / provider-specific
fields (checklist item 2).
"""

from __future__ import annotations

import ipaddress

import pydantic
import pytest
from nv_config_manager_dcim import (
    DCIMInvalidDataError,
    RenderBGPPeer,
    RenderInterface,
    RenderIPAddress,
    RenderVrf,
)

from nv_config_manager_dcim_netbox.render import (
    _access_from_context,
    _bgp_instances,
    _endpoint_set,
    _evpn,
    _firmware_from_context,
    _prefix_set,
    _render_interface,
    _render_ip,
    _render_vlan,
    _render_vrf,
    _routing_from_context,
    _services_from_context,
)

# Area marker for `make <area>` / `pytest -m <area>`.
pytestmark = pytest.mark.render

_NETWORKS = [ipaddress.ip_network("10.0.0.0/24"), ipaddress.ip_network("10.0.0.4/31")]


# --- item 2: SDK models reject unknown / provider-specific fields ---------------


def test_sdk_models_reject_unknown_fields():
    # A valid construction succeeds...
    RenderVrf(name="Tenant-A", route_distinguisher="*:60000")
    # ...but a provider-only / misspelled key is rejected, not silently dropped.
    with pytest.raises(pydantic.ValidationError):
        RenderVrf(name="Tenant-A", netbox_id=7)
    with pytest.raises(pydantic.ValidationError):
        RenderInterface(name="swp1", type="virtual", enabled=True, mgmt=True)
    with pytest.raises(pydantic.ValidationError):
        RenderBGPPeer(
            name="p",
            status="active",
            description="",
            peer_group="",
            peer_role="",
            asn="65001",
            source_ip="10.0.0.1",  # provider-only field
        )


def test_sdk_models_are_immutable():
    vrf = RenderVrf(name="Tenant-A")
    with pytest.raises(pydantic.ValidationError):
        vrf.name = "changed"


# --- item 1: per-mapper malformed / missing-data paths --------------------------


def test_render_vrf_missing_name_returns_none():
    assert _render_vrf({"rd": "*:1"}) is None
    assert _render_vrf(None) is None
    assert _render_vrf("not-a-dict") is None
    vrf = _render_vrf({"name": "T", "rd": None})
    assert vrf.name == "T" and vrf.route_distinguisher is None


def test_render_vlan_missing_vid_returns_none():
    assert _render_vlan({"name": "Data"}) is None
    assert _render_vlan(None) is None
    assert _render_vlan({"vid": 100, "name": "Data"}).vid == 100


def test_render_ip_malformed_address_returns_none():
    assert _render_ip("not-an-ip", None, _NETWORKS) is None
    assert _render_ip("", None, _NETWORKS) is None


def test_render_ip_derives_host_version_and_ancestry():
    ip = _render_ip("10.0.0.5/31", {"value": "loopback"}, _NETWORKS)
    assert isinstance(ip, RenderIPAddress)
    assert str(ip.host) == "10.0.0.5" and ip.version == 4 and ip.role == "loopback"
    # nearest parent first
    assert tuple(str(p) for p in ip.parent_prefixes) == ("10.0.0.4/31", "10.0.0.0/24")


def test_render_ip_ipv6():
    ip = _render_ip("2001:db8::1/64", None, [])
    assert ip.version == 6 and str(ip.host) == "2001:db8::1" and ip.parent_prefixes == ()


def test_render_interface_missing_type_raises():
    with pytest.raises(DCIMInvalidDataError):
        _render_interface({"name": "swp1", "enabled": True}, _NETWORKS)


def test_render_interface_skips_malformed_addresses():
    iface = _render_interface(
        {
            "name": "swp1",
            "type": "virtual",
            "enabled": True,
            "ip_addresses": [{"address": "garbage"}, {"address": "10.0.0.5/31"}],
        },
        _NETWORKS,
    )
    # the malformed address is dropped, the valid one is kept
    assert len(iface.addresses) == 1
    assert str(iface.addresses[0].host) == "10.0.0.5"


def test_bgp_instances_skips_sessions_without_local_as():
    assert _bgp_instances([{"id": 1, "remote_as": {"asn": 65001}}]) == ()


def test_bgp_instances_tolerates_missing_remote_address():
    (instance,) = _bgp_instances(
        [{"id": 1, "local_as": {"asn": 65000}, "remote_as": {"asn": 65001}}]
    )
    assert instance.asn == "65000"
    peer = instance.peers[0]
    assert peer.peer_ipv4 is None and peer.peer_ipv6 is None and peer.asn == "65001"


# The session names neither interface nor VRF. The local address is on this
# device, so the interface holding it gives the source VRF; the remote address is
# on the peer, so resolving it names the neighbour.
_BGP_INTERFACES = [
    {"name": "lo", "vrf": None, "ip_addresses": [{"address": "10.78.151.224/32"}]},
    {
        "name": "swp21s0",
        "vrf": {"name": "EXIT", "rd": "*:4000"},
        "ip_addresses": [{"address": "10.76.245.73/31"}],
    },
]

_BGP_PEER_ENDPOINTS = {
    "9": {
        "interface_name": "Ethernet49",
        "device_name": "example-wan-2",
        "device_role": "TAN-BBR",
    }
}

_BGP_SESSION = {
    "id": 1,
    "name": "oobspine-example-wan-2",
    "status": {"value": "active", "label": "Active"},
    "local_as": {"asn": 4265355097},
    "remote_as": {"asn": 4265355123},
    "local_address": {"id": 8, "address": "10.76.245.73/31"},
    "remote_address": {"id": 9, "address": "10.76.245.72/31"},
}


def test_bgp_peer_identity_derived_from_the_remote_address():
    (instance,) = _bgp_instances([_BGP_SESSION], _BGP_INTERFACES, "lo", None, _BGP_PEER_ENDPOINTS)
    peer = instance.peers[0]
    assert (peer.name, peer.description) == ("example-wan-2", "example-wan-2")
    assert peer.source_interface == "Ethernet49"
    # The VRF is the *local* address's interface, not the peer's.
    assert peer.source_vrf == "EXIT"
    # No peer group on the session, so the peer device's role names it.
    assert (peer.peer_role, peer.peer_group) == ("TAN-BBR", "TAN-BBR")
    assert (peer.status, instance.status) == ("Active", "Active")


def test_bgp_peer_identity_survives_an_uncabled_source_interface():
    """An iBGP/EVPN session is sourced from a loopback, which has no cable to follow.

    Deriving the neighbour from cabling leaves ``peer_group`` blank here, and the
    default-VRF template writes it out unguarded.
    """
    session = {
        **_BGP_SESSION,
        "local_address": {"id": 8, "address": "10.78.151.224/32"},
    }
    (instance,) = _bgp_instances([session], _BGP_INTERFACES, "lo", None, _BGP_PEER_ENDPOINTS)
    peer = instance.peers[0]
    assert (peer.name, peer.peer_group) == ("example-wan-2", "TAN-BBR")
    assert (peer.source_vrf, instance.vrfs) == ("default", ("default",))


def test_bgp_instance_vrfs_span_router_id_and_peer_vrfs():
    """``bgp_routing_instance()`` selects by VRF: omit one and its neighbour vanishes."""
    (instance,) = _bgp_instances([_BGP_SESSION], _BGP_INTERFACES, "lo", None, _BGP_PEER_ENDPOINTS)
    assert instance.vrfs == ("EXIT", "default")


def test_bgp_session_peer_group_wins_over_the_peer_role():
    session = {**_BGP_SESSION, "peer_group": {"id": 3, "name": "underlay"}}
    (instance,) = _bgp_instances([session], _BGP_INTERFACES, "lo", None, _BGP_PEER_ENDPOINTS)
    assert instance.peers[0].peer_group == "underlay"


def test_bgp_unidentifiable_peer_without_a_peer_group_raises():
    """A blank peer_group renders ``peer-group:`` with no value -- invalid, not partial."""
    with pytest.raises(DCIMInvalidDataError, match="assigned to no interface"):
        _bgp_instances([_BGP_SESSION], _BGP_INTERFACES, "lo", None, {})


def test_bgp_unidentifiable_peer_named_by_its_own_peer_group_still_renders():
    """Narrower than the reference, which requires the peer endpoint unconditionally."""
    session = {**_BGP_SESSION, "peer_group": {"id": 3, "name": "underlay"}}
    (instance,) = _bgp_instances([session], _BGP_INTERFACES, "lo", None, {})
    peer = instance.peers[0]
    assert (peer.peer_group, peer.peer_role) == ("underlay", "")
    assert peer.name == "oobspine-example-wan-2" and peer.source_interface is None


def test_peerless_device_asn_still_reports_a_routing_instance():
    """netbox-bgp has no per-device instance object; the contract (and Nautobot) do.

    Without this, a device that has an ASN and peers with nobody reports no BGP at
    all, and only the ``bgp_routing_instance()`` fallback keeps its render honest.
    """
    (instance,) = _bgp_instances([], _BGP_INTERFACES, "lo", None, None, "4230000001")
    assert (instance.asn, instance.status) == ("4230000001", "Active")
    assert (instance.router_id_interface, instance.vrfs, instance.peers) == ("lo", ("default",), ())


def test_device_asn_does_not_duplicate_an_instance_a_session_already_carries():
    instances = _bgp_instances(
        [_BGP_SESSION], _BGP_INTERFACES, "lo", None, _BGP_PEER_ENDPOINTS, "4265355097"
    )
    assert len(instances) == 1 and len(instances[0].peers) == 1


def test_bgp_instances_group_by_local_asn():
    instances = _bgp_instances(
        [
            {"id": 1, "local_as": {"asn": 65000}, "remote_as": {"asn": 1}},
            {"id": 2, "local_as": {"asn": 65000}, "remote_as": {"asn": 2}},
            {"id": 3, "local_as": {"asn": 65001}, "remote_as": {"asn": 3}},
        ]
    )
    by_asn = {i.asn: i for i in instances}
    assert set(by_asn) == {"65000", "65001"}
    assert len(by_asn["65000"].peers) == 2 and len(by_asn["65001"].peers) == 1


def test_endpoint_set_reads_ipv4_ipv6_mapping():
    es = _endpoint_set({"ipv4": ["10.0.0.1"], "ipv6": ["2001:db8::1"]})
    assert tuple(str(x) for x in es.ipv4) == ("10.0.0.1",)
    assert tuple(str(x) for x in es.ipv6) == ("2001:db8::1",)


def test_endpoint_set_empty_or_wrong_type_returns_none():
    assert _endpoint_set({}) is None
    assert _endpoint_set(None) is None
    assert _endpoint_set(["10.0.0.1"]) is None  # must be a {ipv4,ipv6} mapping


def test_endpoint_set_accepts_a_hostname():
    """``RenderEndpointSet`` is typed ``str``, not ``IPvAnyAddress``: a service
    endpoint may be a hostname, so the provider must not re-impose IP-only
    validation the SDK deliberately dropped."""
    es = _endpoint_set({"ipv4": ["ntp.example.com"]})
    assert tuple(str(x) for x in es.ipv4) == ("ntp.example.com",)


def test_prefix_set_invalid_prefix_raises():
    """The prefix set is still ``IPvAnyNetwork``, so this is the live half of
    ``_address_family_set``'s error path."""
    with pytest.raises(DCIMInvalidDataError):
        _prefix_set({"ipv4": ["not-a-prefix"]})


def test_services_from_context_only_sets_present_keys():
    services = _services_from_context({"dns": {"ipv4": ["10.0.0.1"]}})
    assert tuple(str(x) for x in services.dns.ipv4) == ("10.0.0.1",)
    assert services.ntp is None and services.syslog is None and services.tacacs is None


def test_services_dhcp_and_management_prefixes():
    services = _services_from_context(
        {
            "dhcp": {"kea": {"ipv4": ["10.0.0.53"]}},
            "management_prefixes": {"ipv4": ["10.0.0.0/24"]},
        }
    )
    assert services.dhcp[0].name == "kea"
    assert tuple(str(x) for x in services.dhcp[0].endpoints.ipv4) == ("10.0.0.53",)
    assert tuple(str(p) for p in services.management_prefixes.ipv4) == ("10.0.0.0/24",)


def test_firmware_from_context_maps_bundle_and_overrides():
    fw = _firmware_from_context(
        {
            "intended-firmware": {"version": "5.13.1"},
            "firmware_bundle_version": "b1",
            "firmware_bundles": {
                "b1": {
                    "nv_os": {"version": "5.13.1", "image_file": "nvos.bin"},
                    "firmware": {"bmc": {"version": "1.2", "s3_path": "s3://x"}},
                }
            },
            "firmware_overrides": {"skip_components": ["cpld"]},
        }
    )
    assert fw.desired_version == "5.13.1" and fw.selected_bundle_version == "b1"
    assert fw.bundles[0].version == "b1"
    assert fw.bundles[0].operating_system.version == "5.13.1"
    assert fw.bundles[0].components[0].name == "bmc"
    assert fw.overrides.skip_components == ("cpld",)


def test_access_from_context_maps_credential_references():
    access = _access_from_context(
        {
            "password_mappings": {
                "admin": {"password": "root_password", "rotation": "P30D", "role": "r"}
            }
        }
    )
    cred = access.credentials[0]
    assert cred.username == "admin" and cred.secret_name == "root_password"
    assert cred.rotation == "P30D" and cred.role == "r"


def test_access_from_context_requires_password_and_rotation():
    with pytest.raises(DCIMInvalidDataError):
        _access_from_context({"password_mappings": {"admin": {"password": "x"}}})


def test_evpn_from_context():
    evpn = _evpn(
        {
            "evpn_esi_base_mac": "00:11:22:33:44:55",
            "fabric-mac": "aa:bb:cc:dd:ee:ff",
            "evpn": {"df-preference": 100},
        }
    )
    assert evpn.esi_base_mac == "00:11:22:33:44:55"
    assert evpn.fabric_mac == "aa:bb:cc:dd:ee:ff" and evpn.df_preference == 100


def test_routing_from_context_default_asn_and_isis():
    routing = _routing_from_context(
        [],
        {"bgp": {"asn": "65000", "local-asn": "65001"}, "isis": {"interfaces": {"swp1": 10}}},
    )
    assert routing.default_asn == "65000" and routing.local_asn == "65001"
    assert routing.isis_interfaces[0].interface_name == "swp1"
    assert routing.isis_interfaces[0].metric == 10
