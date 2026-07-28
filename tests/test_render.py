# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Assembly tests for ``get_render_data`` (transport stubbed).

These exercise the mapping from NetBox's native GraphQL/REST shapes into the
provider-neutral ``RenderData`` contract without a live NetBox: the transport
methods (``graphql_query`` / ``rest_list``) are replaced with canned responses.
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
    "config_context": {"ntp_servers": ["10.0.0.1"]},
    "site": {"id": "2", "name": "DM-Akron", "slug": "dm-akron", "region": {"name": "Ohio"}},
    "location": {"id": "9", "name": "Rack-Row-A", "parent": {"id": "8", "name": "Hall-1"}},
    "interfaces": [
        {"id": "11", "name": "swp1", "enabled": True, "vrf": {"name": "Tenant-A", "rd": "65000:1"}},
        {"id": "12", "name": "swp2", "enabled": True, "vrf": {"name": "Tenant-A", "rd": "65000:1"}},
    ],
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


async def test_get_render_data_assembles_identity_intent_and_folds_fabric():
    client = create_dcim_client("netbox", _SETTINGS)
    rest_map = {
        "plugins/bgp/session/": [{"id": 1, "name": "peer-1"}],
        client.OVERLAYS_PATH: [{"id": 10, "name": "blue"}],
        client.VXLANS_PATH: [{"id": 5, "vnid": 10010}],
        client.ASSIGNMENTS_PATH: [
            {"id": 1, "assigned_object_type": "dcim.device", "assigned_object_id": 1},
            {"id": 2, "assigned_object_type": "dcim.interface", "assigned_object_id": 11},
            # GFK-id collision: an interface #99 that is NOT on this device.
            {"id": 3, "assigned_object_type": "dcim.interface", "assigned_object_id": 99},
            # device-id collision: a *device* whose id equals an interface id here.
            {"id": 4, "assigned_object_type": "dcim.device", "assigned_object_id": 11},
        ],
        "ipam/prefixes/": [{"prefix": "10.0.0.0/24"}],
        "ipam/vlans/": [{"vid": 10}],
    }

    async def fake_graphql(query, variables=None):
        return {"device": _DEVICE}

    async def fake_rest_list(path, params=None):
        return rest_map.get(path, [])

    client.graphql_query = fake_graphql
    client.rest_list = fake_rest_list
    try:
        rd = await client.get_render_data(RenderDataRequest(device_id="1"))
    finally:
        await client.close()

    assert isinstance(rd, RenderData)
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

    # config_context becomes the device intent verbatim.
    assert rd.device.intent == {"ntp_servers": ["10.0.0.1"]}
    assert len(rd.device.interfaces) == 2

    inv = rd.device.inventory
    assert inv["serial"] == "ABC123"
    assert inv["tenant"] == "Fabric"
    assert inv["manufacturer"] == "NVIDIA"
    # Two interfaces share one VRF -> deduped to a single entry.
    assert inv["vrfs"] == [{"name": "Tenant-A", "rd": "65000:1"}]
    assert len(inv["bgp_sessions"]) == 1
    assert len(inv["overlays"]) == 1
    assert len(inv["vxlans"]) == 1
    # Only the device (#1) and its interface (#11) assignments survive; the
    # unrelated interface #99 and the same-id device #11 are filtered out.
    kept = {
        (a["assigned_object_type"], a["assigned_object_id"])
        for a in inv["overlay_assignments"]
    }
    assert kept == {("dcim.device", 1), ("dcim.interface", 11)}

    # Location render data: Site node + site-scoped inventory.
    assert rd.location.location.kind == "Site"
    assert rd.location.location.name == "DM-Akron"
    assert rd.location.inventory == {
        "prefixes": [{"prefix": "10.0.0.0/24"}],
        "vlans": [{"vid": 10}],
    }
    assert rd.location.intent == {}


async def test_get_render_data_requires_platform_role_model():
    incomplete = {**_DEVICE, "platform": None}
    client = _client_with(incomplete, {})
    try:
        with pytest.raises(DCIMInvalidDataError):
            await client.get_render_data(RenderDataRequest(device_id="1"))
    finally:
        await client.close()


async def test_get_render_data_without_netbox_location_uses_site():
    no_loc = {**_DEVICE, "location": None}
    client = _client_with(no_loc, {})
    try:
        rd = await client.get_render_data(RenderDataRequest(device_id="1"))
    finally:
        await client.close()
    # With no NetBox location, the device location IS the Site.
    assert rd.device.identity.location.kind == "Site"
    assert rd.device.identity.location.name == "DM-Akron"
