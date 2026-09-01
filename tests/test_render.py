# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Assembly tests for ``get_render_data`` (transport stubbed).

These exercise the mapping from NetBox's native GraphQL/REST shapes into the
provider-neutral, fully typed ``RenderData`` contract (2026-07-28 SDK) without a
live NetBox: the transport methods (``graphql_query`` / ``rest_list``) are
replaced with canned responses.
"""

from __future__ import annotations

import copy

import pytest
from nv_config_manager_dcim import (
    DCIMInvalidDataError,
    RenderData,
    RenderDataRequest,
    create_dcim_client,
)

# Area marker for `make <area>` / `pytest -m <area>`.
pytestmark = pytest.mark.render

_SETTINGS = {"server": "https://netbox.example", "token": "t"}

_DEVICE = {
    "id": "1",
    "name": "leaf-01",
    "serial": "ABC123",
    "role": {"name": "Leaf"},
    "platform": {"name": "Cumulus Linux"},
    "device_type": {"model": "SN3700", "manufacturer": {"name": "NVIDIA"}},
    "tenant": {"name": "Fabric"},
    "tags": [{"name": "managed"}],
    "config_context": {"ntp": {"ipv4": ["10.0.0.1"]}, "bgp": {"asn": "65000"}, "site_asn": "65000"},
    "site": {"id": "2", "name": "DM-Akron", "slug": "dm-akron", "region": {"name": "Ohio"}},
    "location": {"id": "9", "name": "Rack-Row-A", "parent": {"id": "8", "name": "Hall-1"}},
    "interfaces": [
        {
            "id": "11",
            "name": "swp1",
            "enabled": True,
            "type": "25gbase_x_sfp28",
            "mtu": 9000,
            "mgmt_only": False,
            "description": "uplink",
            "mac_addresses": [{"mac_address": "aa:bb:cc:00:00:01"}],
            "vrf": {"name": "Tenant-A", "rd": "65000:1"},
            "untagged_vlan": {"vid": 100, "name": "Data"},
            "tagged_vlans": [{"vid": 200, "name": "Voice"}],
            "ip_addresses": [{"address": "10.0.0.5/31", "role": None}],
            "tags": [{"name": "uplink"}],
        },
        {
            "id": "12",
            "name": "swp2",
            "enabled": True,
            "type": "25gbase_x_sfp28",
            "vrf": {"name": "Tenant-A", "rd": "65000:1"},
            "ip_addresses": [],
        },
    ],
}

_REST_MAP = {
    "plugins/bgp/session/": [
        {
            "id": 1,
            "name": "peer-1",
            "status": {"value": "active"},
            "local_as": {"asn": 65000},
            "remote_as": {"asn": 65001},
            "remote_address": {"address": "10.0.0.4/31"},
            "peer_group": {"name": "underlay"},
        }
    ],
    "plugins/nv-config-manager/overlay-assignments/": [
        {"id": 1, "assigned_object_type": "dcim.device", "assigned_object_id": 1, "overlay": 10},
        {
            "id": 2,
            "assigned_object_type": "dcim.interface",
            "assigned_object_id": 11,
            "overlay": 10,
        },
        # unrelated interface (#99 not on this device) -> its overlay #77 must be excluded
        {
            "id": 3,
            "assigned_object_type": "dcim.interface",
            "assigned_object_id": 99,
            "overlay": 77,
        },
    ],
    "plugins/nv-config-manager/overlays/": [{"id": 10, "name": "blue"}],
    "plugins/nv-config-manager/vxlans/": [
        {"id": 5, "vnid": 10010, "vni_type": "l3", "vrf": 8, "overlay": 10, "l3_vlan_id": None},
        {
            "id": 6,
            "vnid": 10020,
            "vni_type": "l2",
            "vlan": 100,
            "overlay": 10,
            "import_targets": [{"name": "65000:10020"}],
            "export_targets": [],
        },
        # overlay #77 is not assigned to this device -> excluded from scope
        # VRF 9 is not on any of this device's interfaces, so this L3 VNI is out
        # of scope: the reference scopes L3 by the device's VRFs, not by overlay
        # assignment (L3 overlays are never assigned to devices).
        {"id": 7, "vnid": 10099, "vni_type": "l3", "vrf": 9, "overlay": 77},
    ],
    "ipam/vrfs/": [{"id": 8, "name": "Tenant-A", "rd": "65000:1"}],
    "ipam/prefixes/": [
        {
            "prefix": "10.0.0.0/24",
            "role": {"name": "Loopback", "slug": "loopback"},
            # `role-aggregate` is what the reference filters location prefixes by;
            # `10.0.0.4/31` below stands for the site's non-aggregate prefixes,
            # which must not reach the render payload.
            "tags": [{"name": "role-aggregate"}, {"name": "mgmt"}],
        },
        {"prefix": "10.0.0.4/31"},
    ],
    "ipam/vlans/": [
        {
            "id": 100,
            "vid": 100,
            "name": "Data",
            "custom_fields": {"nvcm_helper_addresses": ["10.0.0.53"]},
        },
        {"id": 200, "vid": 200, "name": "Voice"},
    ],
    "plugins/nv-config-manager/nvlink-domains/": [{"topology": "gb200_nvl36r1_c2g4"}],
}


def _client_with(device, rest_map):
    client = create_dcim_client("netbox", _SETTINGS)

    async def fake_graphql(query, variables=None):
        return {"device": device}

    async def fake_rest_list(path, params=None):
        return rest_map.get(path, [])

    client.graphql_query = fake_graphql
    client.rest_list = fake_rest_list
    return client


async def test_get_render_data_assembles_typed_sections():
    client = _client_with(_DEVICE, _REST_MAP)
    try:
        rd = await client.get_render_data(RenderDataRequest(device_id="1"))
    finally:
        await client.close()

    assert isinstance(rd, RenderData)

    # --- identity ---
    identity = rd.device.identity
    assert (identity.id, identity.name, identity.platform, identity.role, identity.model) == (
        "1",
        "leaf-01",
        "Cumulus Linux",
        "Leaf",
        "SN3700",
    )
    assert identity.tags == ("managed",)
    # Location hierarchy: innermost NetBox location -> ... -> Site -> Region.
    assert identity.location.name == "Rack-Row-A"
    assert identity.location.parent.name == "Hall-1"
    assert identity.location.parent.parent.kind == "Site"
    assert identity.location.parent.parent.parent.kind == "Region"

    # --- interfaces (typed) ---
    assert len(rd.device.interfaces) == 2
    swp1 = rd.device.interfaces[0]
    assert (swp1.name, swp1.type, swp1.enabled, swp1.mtu) == ("swp1", "25gbase_x_sfp28", True, 9000)
    assert swp1.mac_address == "aa:bb:cc:00:00:01"
    assert swp1.vrf.name == "Tenant-A" and swp1.vrf.route_distinguisher == "65000:1"
    assert swp1.untagged_vlan.vid == 100 and swp1.tagged_vlans[0].vid == 200
    # address: host/version derived, parent prefixes ordered nearest-first.
    addr = swp1.addresses[0]
    assert str(addr.host) == "10.0.0.5" and addr.version == 4
    assert tuple(str(p) for p in addr.parent_prefixes) == ("10.0.0.4/31", "10.0.0.0/24")

    # --- network: distinct device VRFs (deduped) ---
    assert [v.name for v in rd.device.network.vrfs] == ["Tenant-A"]
    # NVLink topology from the device's NVLink domain (feeds NVSwitch templates).
    assert rd.device.network.nvlink_topology == "gb200_nvl36r1_c2g4"

    # --- routing: BGP grouped by local ASN ---
    assert len(rd.device.routing.bgp_instances) == 1
    inst = rd.device.routing.bgp_instances[0]
    assert inst.asn == "65000" and len(inst.peers) == 1
    peer = inst.peers[0]
    assert peer.asn == "65001" and str(peer.peer_ipv4) == "10.0.0.4"
    assert rd.device.routing.site_asn == "65000"

    # --- overlays: scoped to the device's assigned overlays only ---
    assert len(rd.device.overlays.l3_vnis) == 1  # vnid 10010; the VRF-9 vxlan is out of scope
    assert rd.device.overlays.l3_vnis[0].vrf.name == "Tenant-A"
    assert len(rd.device.overlays.l2_vnis) == 1
    l2 = rd.device.overlays.l2_vnis[0]
    assert l2.vni == 10020 and l2.vlan.vid == 100
    assert l2.import_targets[0].name == "65000:10020"

    # --- services from config_context ---
    assert tuple(str(x) for x in rd.device.services.ntp.ipv4) == ("10.0.0.1",)

    # --- location address space ---
    assert rd.location.location.kind == "Site" and rd.location.location.name == "DM-Akron"
    prefixes = rd.location.address_space.prefixes
    assert str(prefixes[0].prefix) == "10.0.0.0/24"
    assert prefixes[0].role == "Loopback" and prefixes[0].tags == ("role-aggregate", "mgmt")
    # Only the aggregates: the untagged 10.0.0.4/31 in the fixture is excluded.
    assert len(prefixes) == 1
    data_vlan = next(v for v in rd.location.address_space.vlans if v.vlan.vid == 100)
    assert tuple(str(h) for h in data_vlan.helper_addresses) == ("10.0.0.53",)
    assert rd.location.routing.site_asn == "65000"


async def test_render_data_round_trips_through_cache():
    client = _client_with(_DEVICE, _REST_MAP)
    try:
        rd = await client.get_render_data(RenderDataRequest(device_id="1"))
    finally:
        await client.close()
    restored = RenderData.from_cache(rd.to_cache())
    assert isinstance(restored, RenderData)
    assert restored.device.identity.name == "leaf-01"
    assert restored == rd
    # full-graph re-validation of the returned model (every nested Render* model)
    assert RenderData.model_validate(restored.model_dump()) == rd


async def test_get_render_data_requires_platform_role_model():
    client = _client_with({**_DEVICE, "platform": None}, {})
    try:
        with pytest.raises(DCIMInvalidDataError):
            await client.get_render_data(RenderDataRequest(device_id="1"))
    finally:
        await client.close()


async def test_interface_missing_type_raises():
    bad = {**_DEVICE, "interfaces": [{"id": "11", "name": "swp1", "enabled": True}]}
    client = _client_with(bad, {})
    try:
        with pytest.raises(DCIMInvalidDataError):
            await client.get_render_data(RenderDataRequest(device_id="1"))
    finally:
        await client.close()


async def test_get_render_data_without_netbox_location_uses_site():
    client = _client_with({**_DEVICE, "location": None}, _REST_MAP)
    try:
        rd = await client.get_render_data(RenderDataRequest(device_id="1"))
    finally:
        await client.close()
    # With no NetBox location, the device location IS the Site.
    assert rd.device.identity.location.kind == "Site"
    assert rd.device.identity.location.name == "DM-Akron"


async def test_render_interface_exposes_cabling_peer_for_bgp_neighbor():
    """A cabled interface exposes its peer (name, peer IPv4, role, routing ASN)
    so adjacency-derived templates can form a BGP neighbour; an uncabled
    interface exposes no peer.

    Regression for the OOB mgmt-leaf: the superpod plugin's ``vrf.j2`` builds a
    BGP neighbour from ``intf.connected_interface.device`` (peer_ipv4 + ASN,
    gated on role ``OOB-Server``). Without ``connected_interface`` populated the
    switch renders zero neighbours, never learns its route to the provisioning
    network, and fails to POST ``/provisioned``.
    """
    device = copy.deepcopy(_DEVICE)
    device["interfaces"][0]["connected_endpoints"] = [
        {
            "name": "eth1",
            "vrf": None,
            "ip_addresses": [{"address": "10.120.0.0/31", "role": None}],
            "device": {
                "id": "99",
                "name": "oob-mgmt-server",
                "role": {"name": "OOB-Server"},
                "tenant": {"name": "Public Demo"},
                "tags": [{"name": "air-trial"}],
                "config_context": {"bgp": {"asn": "65000"}},
            },
        }
    ]
    client = _client_with(device, _REST_MAP)
    try:
        rd = await client.get_render_data(RenderDataRequest(device_id="1"))
    finally:
        await client.close()

    by_name = {i.name: i for i in rd.device.interfaces}

    peer = by_name["swp1"].connected_interface
    assert peer is not None, "cabled interface must expose its connected peer"
    assert peer.name == "eth1"
    assert str(peer.addresses[0].host) == "10.120.0.0"  # -> template peer_ipv4
    assert peer.device.name == "oob-mgmt-server"
    assert peer.device.role == "OOB-Server"  # neighbour gate in vrf.j2
    assert peer.device.routing_asn == "65000"  # numbered remote-as

    # uncabled interface exposes no peer -> no spurious neighbour
    assert by_name["swp2"].connected_interface is None


async def test_render_ports_role_lag_targets_and_router_id():
    """Ports of four computed render gaps: interface role (from nvcm_interface_role
    CF), LAG member inversion, VRF route-targets, and BGP router-id interface."""
    device = copy.deepcopy(_DEVICE)
    device["interfaces"][0]["custom_fields"] = {"nvcm_interface_role": "Uplink"}
    device["interfaces"][0]["vrf"]["import_targets"] = [{"name": "65000:1"}]
    device["interfaces"][0]["vrf"]["export_targets"] = [{"name": "65000:2"}]
    device["interfaces"][1]["lag"] = {"id": "77"}  # swp2 is a member of bond0
    device["interfaces"].append(
        {"id": "77", "name": "bond0", "enabled": True, "type": "lag", "ip_addresses": []}
    )
    device["interfaces"].append(
        {
            "id": "50",
            "name": "lo",
            "enabled": True,
            "type": "virtual",
            "custom_fields": {"nvcm_interface_role": "Loopback"},
            "ip_addresses": [],
        }
    )
    client = _client_with(device, _REST_MAP)
    try:
        rd = await client.get_render_data(RenderDataRequest(device_id="1"))
    finally:
        await client.close()

    by = {i.name: i for i in rd.device.interfaces}
    assert by["swp1"].role == "Uplink"  # from nvcm_interface_role CF
    assert by["swp1"].vrf.import_targets[0].name == "65000:1"
    assert by["swp1"].vrf.export_targets[0].name == "65000:2"
    assert by["bond0"].member_interfaces == ("swp2",)  # inverse lag relation
    assert by["swp2"].member_interfaces == ()
    inst = rd.device.routing.bgp_instances[0]
    assert inst.router_id_interface == "lo"  # loopback drives BGP router-id


async def test_console_server_ports_from_native_dcim():
    """Console-server ports and whatever each is cabled to."""
    device = copy.deepcopy(_DEVICE)
    device["consoleserverports"] = [
        {
            "name": "console-1",
            "connected_endpoints": [
                {
                    "name": "console",
                    "device": {
                        "name": "leaf-99",
                        "device_type": {"manufacturer": {"name": "NVIDIA"}},
                    },
                }
            ],
        },
        # Uncabled: still rendered, just without a far side.
        {"name": "console-2", "connected_endpoints": []},
    ]
    client = _client_with(device, _REST_MAP)
    try:
        rd = await client.get_render_data(RenderDataRequest(device_id="1"))
    finally:
        await client.close()

    ports = rd.device.network.console_server_ports
    assert [p.name for p in ports] == ["console-1", "console-2"]
    assert ports[0].connected_device_name == "leaf-99"
    assert ports[0].connected_device_manufacturer == "NVIDIA"
    assert ports[0].connected_port_name == "console"
    assert ports[1].connected_device_name is None


async def test_bgp_peer_ttl_from_peer_group_extra_attributes():
    """netbox-bgp keeps multihop TTL in extra_attributes, on the session or its group."""
    rest = copy.deepcopy(_REST_MAP)
    rest["plugins/bgp/session/"][0]["peer_group"] = {"id": 3, "name": "underlay"}
    rest["plugins/bgp/peer-group/"] = [
        {"id": 3, "name": "underlay", "extra_attributes": {"ttl": 4}}
    ]
    client = _client_with(_DEVICE, rest)
    try:
        rd = await client.get_render_data(RenderDataRequest(device_id="1"))
    finally:
        await client.close()

    assert rd.device.routing.bgp_instances[0].peers[0].ttl == 4


async def test_bgp_peer_ttl_on_the_session_wins():
    rest = copy.deepcopy(_REST_MAP)
    rest["plugins/bgp/session/"][0]["peer_group"] = {"id": 3, "name": "underlay"}
    rest["plugins/bgp/session/"][0]["extra_attributes"] = {"ttl": 1}
    rest["plugins/bgp/peer-group/"] = [{"id": 3, "extra_attributes": {"ttl": 4}}]
    client = _client_with(_DEVICE, rest)
    try:
        rd = await client.get_render_data(RenderDataRequest(device_id="1"))
    finally:
        await client.close()

    assert rd.device.routing.bgp_instances[0].peers[0].ttl == 1


async def test_bgp_peer_ttl_absent_stays_none():
    client = _client_with(_DEVICE, _REST_MAP)
    try:
        rd = await client.get_render_data(RenderDataRequest(device_id="1"))
    finally:
        await client.close()

    assert rd.device.routing.bgp_instances[0].peers[0].ttl is None


async def test_location_topology_route_servers_and_wan_routers():
    """Route servers come from the route-server tag, WAN routers from role."""
    rest = copy.deepcopy(_REST_MAP)
    seen: list[list[tuple[str, str]]] = []

    client = create_dcim_client("netbox", _SETTINGS)

    async def fake_graphql(query, variables=None):
        return {"device": _DEVICE}

    # Device 5 has a loopback (id 50) and a transit port (id 51); only the loopback's
    # address may surface. NetBox has no interface-name filter on ip-addresses, so the
    # loopbacks are resolved on dcim/interfaces and selected by interface_id.
    ip_queries: list[list[tuple[str, str]]] = []

    async def fake_rest_list(path, params=None):
        if path == "dcim/devices/":
            seen.append(list(params or []))
            tags = [v for k, v in (params or []) if k == "tag"]
            if tags == ["route-server"]:
                return [{"id": 5, "name": "rs-1", "config_context": {"bgp": {"asn": "65100"}}}]
            return [{"id": 6, "name": "wan-1", "config_context": {}}]
        if path == "dcim/interfaces/":
            starts_with = [v for k, v in (params or []) if k == "name__isw"]
            if not starts_with:
                return rest.get(path, [])
            # One query covers every device, so the rows carry their own device.
            device_ids = {v for k, v in (params or []) if k == "device_id"}
            return [{"id": 50, "name": "lo", "device": {"id": 5}} for _ in [1] if "5" in device_ids]
        if path == "ipam/ip-addresses/":
            ip_queries.append(list(params or []))
            selected = {v for k, v in (params or []) if k == "interface_id"}
            addresses = {"50": "10.9.9.9/32", "51": "10.1.1.0/31"}
            return [
                {"address": addresses[i], "assigned_object_id": int(i)}
                for i in sorted(selected)
                if i in addresses
            ]
        return rest.get(path, [])

    client.graphql_query = fake_graphql
    client.rest_list = fake_rest_list
    try:
        rd = await client.get_render_data(RenderDataRequest(device_id="1"))
    finally:
        await client.close()

    topology = rd.location.topology
    assert [d.name for d in topology.route_servers] == ["rs-1"]
    assert topology.route_servers[0].routing_asn == "65100"
    # Only the loopback address, not the transit /31 on interface 51.
    assert [str(a) for a in topology.route_servers[0].loopback_addresses] == ["10.9.9.9"]
    assert [d.name for d in topology.wan_routers] == ["wan-1"]
    # Both selections are scoped to the site, never fabric-wide.
    assert all(("site_id", "2") in params for params in seen)
    # interface__name__isw is not a NetBox filter, and unknown params are dropped
    # rather than rejected, so it returned every address on the device.
    assert any(("interface_id", "50") in params for params in ip_queries)
    assert not any(key.startswith("interface__name") for p in ip_queries for key, _ in p)


async def test_location_device_with_no_loopback_interface_has_no_loopbacks():
    # No loopback interface means no loopback addresses, not every device address.
    client = create_dcim_client("netbox", _SETTINGS)

    async def fake_rest_list(path, params=None):
        if path == "dcim/interfaces/":
            return []
        if path == "ipam/ip-addresses/":
            raise AssertionError("must not query addresses without a loopback interface")
        return []

    client.rest_list = fake_rest_list
    try:
        by_device = await client._loopback_addresses_by_device(["5"])
        device = client._location_device({"id": 5, "name": "rs-1"}, by_device.get("5", []))
    finally:
        await client.close()
    assert device.loopback_addresses == ()


async def test_location_topology_degrades_when_netbox_rejects_the_tag_or_role_slug():
    # tag/role are slug-keyed and validated, so an instance missing route-server or
    # the wan/tan-bbr roles answers 400, not 200-with-no-rows.
    rest = copy.deepcopy(_REST_MAP)
    client = create_dcim_client("netbox", _SETTINGS)

    async def fake_graphql(query, variables=None):
        return {"device": _DEVICE}

    async def fake_rest_list(path, params=None):
        if path == "dcim/devices/" and any(k in ("tag", "role") for k, _ in params or []):
            raise DCIMInvalidDataError("NetBox rejected the request (400)")
        return rest.get(path, [])

    client.graphql_query = fake_graphql
    client.rest_list = fake_rest_list
    try:
        rd = await client.get_render_data(RenderDataRequest(device_id="1"))
    finally:
        await client.close()

    assert rd.location.topology.route_servers == ()
    assert rd.location.topology.wan_routers == ()


async def test_l2_vni_vrfs_from_device_level_assignments():
    """Only device-level assignments become l2_vni_vrfs.

    An interface-level assignment is a port joining an overlay, not the device
    carrying that overlay's VRF.
    """
    client = _client_with(_DEVICE, _REST_MAP)
    try:
        rd = await client.get_render_data(RenderDataRequest(device_id="1"))
    finally:
        await client.close()

    vrfs = rd.device.overlays.l2_vni_vrfs
    assert [v.name for v in vrfs] == ["blue"]
    assert vrfs[0].vni == 10020
    assert [t.name for t in vrfs[0].import_targets] == ["65000:10020"]


async def test_l2_vni_vrfs_fall_back_to_assignment_targets():
    """A VXLAN with no route targets defers to the assignment's overrides."""
    rest = copy.deepcopy(_REST_MAP)
    for vxlan in rest["plugins/nv-config-manager/vxlans/"]:
        if vxlan.get("vni_type") == "l2":
            vxlan["import_targets"] = []
            vxlan["export_targets"] = []
    rest["plugins/nv-config-manager/overlay-assignments/"][0]["import_targets"] = [
        {"name": "65000:999"}
    ]
    client = _client_with(_DEVICE, rest)
    try:
        rd = await client.get_render_data(RenderDataRequest(device_id="1"))
    finally:
        await client.close()

    assert [t.name for t in rd.device.overlays.l2_vni_vrfs[0].import_targets] == ["65000:999"]


async def test_l2_vni_vrfs_excludes_interface_only_overlays():
    rest = copy.deepcopy(_REST_MAP)
    # Drop the device-level assignment, leaving only the interface one.
    rest["plugins/nv-config-manager/overlay-assignments/"] = [
        a
        for a in rest["plugins/nv-config-manager/overlay-assignments/"]
        if a["assigned_object_type"] != "dcim.device"
    ]
    client = _client_with(_DEVICE, rest)
    try:
        rd = await client.get_render_data(RenderDataRequest(device_id="1"))
    finally:
        await client.close()

    # The overlay still reaches the device through its interface...
    assert rd.device.overlays.l2_vnis
    # ...but the device does not carry its VRF.
    assert rd.device.overlays.l2_vni_vrfs == ()


async def test_peer_groups_are_scoped_to_the_ids_the_sessions_reference():
    # Unfiltered, this paginated the whole fabric's peer-group table per render.
    rest = copy.deepcopy(_REST_MAP)
    rest["plugins/bgp/session/"][0]["peer_group"] = {"id": 3, "name": "underlay"}
    rest["plugins/bgp/peer-group/"] = [{"id": 3, "extra_attributes": {"ttl": 4}}]
    seen: list[list[tuple[str, str]]] = []

    client = create_dcim_client("netbox", _SETTINGS)

    async def fake_graphql(query, variables=None):
        return {"device": _DEVICE}

    async def fake_rest_list(path, params=None):
        if path == "plugins/bgp/peer-group/":
            seen.append(list(params or []))
        return rest.get(path, [])

    client.graphql_query = fake_graphql
    client.rest_list = fake_rest_list
    try:
        rd = await client.get_render_data(RenderDataRequest(device_id="1"))
    finally:
        await client.close()

    assert rd.device.routing.bgp_instances[0].peers[0].ttl == 4
    assert seen and all(("id", "3") in params for params in seen)


async def test_no_peer_group_read_when_no_session_references_one():
    rest = copy.deepcopy(_REST_MAP)
    rest["plugins/bgp/session/"][0].pop("peer_group", None)
    client = create_dcim_client("netbox", _SETTINGS)

    async def fake_rest_list(path, params=None):
        if path == "plugins/bgp/peer-group/":
            raise AssertionError("must not read peer groups when none are referenced")
        return rest.get(path, [])

    async def fake_graphql(query, variables=None):
        return {"device": _DEVICE}

    client.graphql_query = fake_graphql
    client.rest_list = fake_rest_list
    try:
        await client.get_render_data(RenderDataRequest(device_id="1"))
    finally:
        await client.close()


async def test_topology_device_in_both_lists_is_fetched_once_and_appears_in_both():
    # A border leaf can be route-server-tagged AND WAN-roled; map it once.
    rest = copy.deepcopy(_REST_MAP)
    both = {"id": 5, "name": "border-1", "config_context": {"bgp": {"asn": "65100"}}}
    loopback_calls: list[str] = []

    client = create_dcim_client("netbox", _SETTINGS)

    async def fake_graphql(query, variables=None):
        return {"device": _DEVICE}

    async def fake_rest_list(path, params=None):
        if path == "dcim/devices/" and any(k in ("tag", "role") for k, _ in params or []):
            return [both]
        if path == "dcim/interfaces/" and any(k == "name__isw" for k, _ in params or []):
            loopback_calls.append(dict(params or [])["device_id"])
            return [{"id": 50, "name": "lo"}]
        if path == "ipam/ip-addresses/":
            return [{"address": "10.9.9.9/32"}]
        return rest.get(path, [])

    client.graphql_query = fake_graphql
    client.rest_list = fake_rest_list
    try:
        rd = await client.get_render_data(RenderDataRequest(device_id="1"))
    finally:
        await client.close()

    topology = rd.location.topology
    assert [d.name for d in topology.route_servers] == ["border-1"]
    assert [d.name for d in topology.wan_routers] == ["border-1"]
    assert loopback_calls == ["5"], "the shared device must be resolved once"


async def test_one_wan_role_query_per_role_so_an_undefined_role_keeps_the_other():
    # _optional_list degrades the whole query on a 400, so a combined role filter
    # lets a role this NetBox lacks discard the devices the other role matched.
    rest = copy.deepcopy(_REST_MAP)
    role_params: list[str] = []
    client = create_dcim_client("netbox", _SETTINGS)

    async def fake_graphql(query, variables=None):
        return {"device": _DEVICE}

    async def fake_rest_list(path, params=None):
        if path == "dcim/devices/":
            roles = [value for key, value in params or [] if key == "role"]
            role_params.extend(roles)
            if "tan-bbr" in roles:
                raise DCIMInvalidDataError("tan-bbr is not one of the available choices")
            if roles == ["wan"]:
                return [{"id": 5, "name": "wan-1", "config_context": {}}]
            return []
        if path == "dcim/interfaces/" and any(k == "name__isw" for k, _ in params or []):
            return []
        return rest.get(path, [])

    client.graphql_query = fake_graphql
    client.rest_list = fake_rest_list
    try:
        rd = await client.get_render_data(RenderDataRequest(device_id="1"))
    finally:
        await client.close()

    assert sorted(role_params) == ["tan-bbr", "wan"], "each role must be its own query"
    assert [d.name for d in rd.location.topology.wan_routers] == ["wan-1"]
