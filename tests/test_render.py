# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Assembly tests for ``get_render_data`` (transport stubbed).

These exercise the mapping from NetBox's native GraphQL/REST shapes into the
provider-neutral, fully typed ``RenderData`` contract (2026-07-28 SDK) without a
live NetBox: the transport methods (``graphql_query`` / ``rest_list``) are
replaced with canned responses.
"""

from __future__ import annotations

import pytest
from nv_config_manager_dcim import (
    DCIMInvalidDataError,
    RenderData,
    RenderDataRequest,
    create_dcim_client,
)

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
        {"id": 7, "vnid": 10099, "vni_type": "l3", "vrf": 8, "overlay": 77},
    ],
    "ipam/vrfs/": [{"id": 8, "name": "Tenant-A", "rd": "65000:1"}],
    "ipam/prefixes/": [
        {"prefix": "10.0.0.0/24", "role": {"value": "loopback"}, "tags": [{"name": "mgmt"}]},
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
    assert len(rd.device.overlays.l3_vnis) == 1  # vnid 10010; the overlay-77 vxlan is excluded
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
    assert prefixes[0].role == "loopback" and prefixes[0].tags == ("mgmt",)
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
