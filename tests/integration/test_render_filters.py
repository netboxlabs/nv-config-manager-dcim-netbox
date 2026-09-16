# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Compatibility test: the common template filters run on NetBox-derived RenderData.

Per NETBOX_COLLAB.md, the NetBox provider is compatible when the shared
``nv_config_manager_templates`` filters work against its ``RenderData`` "without a
provider check, NetBox adapter, native query, or provider-specific template
branch." This builds ``RenderData`` from representative NetBox GraphQL/REST
responses through the provider, round-trips it through the portable
``to_cache()`` / ``from_cache()`` envelope, and exercises a representative set of
device and location filters against it.

Skipped automatically when the network-templates package is not installed (see
TESTING.md for the venv that provides it).
"""

from __future__ import annotations

import asyncio

import pytest
from nv_config_manager_dcim import RenderData, RenderDataRequest, create_dcim_client

# The shared filter library is an optional test dependency (installed alongside
# the SDK per TESTING.md). Skip the whole module when it is absent.
device_filters = pytest.importorskip("nv_config_manager_templates.filters.device")
location_filters = pytest.importorskip("nv_config_manager_templates.filters.location")

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
    "config_context": {
        "dns": {"ipv4": ["10.0.0.1"]},
        "ntp": {"ipv4": ["10.0.0.2"]},
        "bgp": {"asn": "65000"},
        "site_asn": "65000",
    },
    "site": {"id": "2", "name": "DM-Akron", "slug": "dm-akron", "region": {"name": "Ohio"}},
    "location": None,
    "interfaces": [
        {
            "id": "10",
            "name": "lo",
            "enabled": True,
            "type": "virtual",
            "ip_addresses": [{"address": "10.0.0.5/32", "role": None}],
        },
        {
            "id": "11",
            "name": "swp1",
            "enabled": True,
            "type": "25gbase_x_sfp28",
            # The common VRF filter requires the overlay route-distinguisher form
            # ``*:<vni>`` (what NVCM's fabric VRFs use); a conventional ASN:index
            # RD is rejected by nv_config_manager_templates' VRF.from_render_data.
            "vrf": {"name": "Tenant-A", "rd": "*:4010"},
            "ip_addresses": [{"address": "10.0.1.1/31", "role": None}],
            "tags": [{"name": "uplink"}],
        },
    ],
}

_REST = {
    "plugins/bgp/session/": [
        {
            "id": 1,
            "name": "peer-1",
            "status": {"value": "active"},
            "local_as": {"asn": 65000},
            "remote_as": {"asn": 65001},
            "remote_address": {"id": 40, "address": "10.0.1.0/31"},
            "peer_group": {"name": "underlay"},
        }
    ],
    # The remote address must resolve to an interface on the peer, as the
    # reference requires, or the session fails the render.
    "ipam/ip-addresses/": [
        {
            "id": 40,
            "address": "10.0.1.0/31",
            "assigned_object_type": "dcim.interface",
            "assigned_object": {
                "id": 41,
                "name": "Ethernet1",
                "device": {"id": 42, "name": "spine-01"},
            },
        }
    ],
    "plugins/nv-config-manager/overlay-assignments/": [
        {"id": 1, "assigned_object_type": "dcim.device", "assigned_object_id": 1, "overlay": 10},
    ],
    "plugins/nv-config-manager/overlays/": [{"id": 10, "name": "blue"}],
    "plugins/nv-config-manager/vxlans/": [
        {"id": 5, "vnid": 10010, "vni_type": "l3", "vrf": 8, "overlay": 10, "l3_vlan_id": 4010},
        {"id": 6, "vnid": 10020, "vni_type": "l2", "vlan": 100, "overlay": 10},
    ],
    "ipam/vrfs/": [{"id": 8, "name": "Tenant-A", "rd": "65000:1"}],
    "ipam/prefixes/": [
        {
            "prefix": "10.0.0.0/24",
            "role": {"name": "Loopback", "slug": "loopback"},
            # The aggregate tag the reference filters location prefixes by; the
            # two untagged prefixes below stay out of address_space but must still
            # be found as parents of the loopback address.
            "tags": [{"name": "role-aggregate"}, {"name": "mgmt"}],
        },
        {"prefix": "10.0.0.4/31"},
        {"prefix": "10.0.1.0/31"},
    ],
    "ipam/vlans/": [
        {
            "id": 100,
            "vid": 100,
            "name": "Data",
            "custom_fields": {"nvcm_helper_addresses": ["10.0.0.53"]},
        }
    ],
}


async def _render_via_provider() -> RenderData:
    client = create_dcim_client("netbox", _SETTINGS)

    async def fake_graphql(query, variables=None):
        return {"device": _DEVICE}

    async def fake_rest_list(path, params=None):
        return _REST.get(path, [])

    client.graphql_query = fake_graphql
    client.rest_list = fake_rest_list
    try:
        return await client.get_render_data(RenderDataRequest(device_id="1"))
    finally:
        await client.close()


@pytest.fixture(scope="module")
def render_data() -> RenderData:
    """NetBox-derived RenderData, round-tripped through the portable cache envelope."""
    rd = asyncio.run(_render_via_provider())
    restored = RenderData.from_cache(rd.to_cache())
    assert restored == rd  # provider-neutral cache is lossless
    return restored


def test_identity_filters(render_data):
    dev = render_data.device
    assert device_filters.hostname(dev) == "leaf-01"
    assert device_filters.site_name(dev) == "DM-Akron"
    assert device_filters.platform(dev) == "Cumulus Linux"
    assert device_filters.model(dev) == "SN3700"
    assert device_filters.role(dev) == "Leaf"
    assert device_filters.uuid(dev) == "1"
    assert device_filters.device_tags(dev) == ["managed"]
    assert device_filters.has_tag(dev, "managed") is True
    assert device_filters.has_tag(dev, "absent") is False


def test_routing_and_vrf_filters(render_data):
    dev = render_data.device
    assert device_filters.asn(dev) == "65000"
    assert device_filters.has_vrf(dev, "Tenant-A") is True
    assert [v.name for v in device_filters.attached_vrfs(dev)] == ["Tenant-A"]


def test_service_filters(render_data):
    dev = render_data.device
    assert device_filters.dns_servers(dev) == ["10.0.0.1"]
    assert device_filters.ntp_servers(dev) == ["10.0.0.2"]


def test_addressing_filters(render_data):
    dev = render_data.device
    # lo 10.0.0.5/32 -> parent prefixes [10.0.0.4/31, 10.0.0.0/24]; loopback prefix is the /24.
    assert device_filters.loopback_prefix(dev) == "10.0.0.0/24"


def test_overlay_filters(render_data):
    dev = render_data.device
    assert device_filters.vni_mappings(dev, 100) == "10020"
    assert device_filters.l3vni_mappings(dev, "Tenant-A") == "4010"


def test_location_filters(render_data):
    loc = render_data.location
    assert location_filters.site_aggregates(loc, "loopback") == ["10.0.0.0/24"]
