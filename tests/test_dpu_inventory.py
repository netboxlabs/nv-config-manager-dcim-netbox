# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""update_dpu_device_inventory tests (transport stubbed).

NetBox 4.x stores a MAC as a first-class ``MACAddress`` assigned to an interface,
so the update upserts the MAC and sets it as the interface's primary.
"""

from __future__ import annotations

from nv_config_manager_dcim import create_dcim_client

_SETTINGS = {"server": "https://netbox.example", "token": "t"}

_HOST_DEVICE = {
    "id": "5",
    "name": "dpu-1",
    "serial": "SN-NEW",
    "role": {"slug": "dpu"},
    "site": {"name": "Site 1"},
    "device_type": {"slug": "bluefield"},
    "interfaces": [
        {"id": "11", "name": "eth0", "primary_mac_address": {"mac_address": "AA:BB:CC:00:00:01"}},
    ],
}


def _client(existing_macs):
    client = create_dcim_client("netbox", _SETTINGS)
    calls = []

    async def fake_rest_request(method, path, *, params=None, json=None):
        calls.append((method, path, json))
        if method == "POST" and path == "dcim/mac-addresses/":
            return {"id": 99, "mac_address": json["mac_address"]}
        return {}

    async def fake_rest_list(path, params=None):
        assert path == "dcim/mac-addresses/"
        return existing_macs

    async def fake_graphql(query, variables=None):
        return {"device": _HOST_DEVICE}

    client.rest_request = fake_rest_request
    client.rest_list = fake_rest_list
    client.graphql_query = fake_graphql
    client._calls = calls
    return client


async def test_update_dpu_creates_mac_and_sets_primary():
    client = _client(existing_macs=[])  # no existing MAC -> create
    try:
        result = await client.update_dpu_device_inventory(
            "5", "SN-NEW", {"11": "aa:bb:cc:00:00:01"}
        )
    finally:
        await client.close()
    calls = client._calls
    # device serial patched first
    assert calls[0] == ("PATCH", "dcim/devices/5/", {"serial": "SN-NEW"})
    # MAC created and assigned to the interface
    assert (
        "POST",
        "dcim/mac-addresses/",
        {
            "mac_address": "aa:bb:cc:00:00:01",
            "assigned_object_type": "dcim.interface",
            "assigned_object_id": 11,
        },
    ) in calls
    # interface primary_mac_address set to the new MAC id
    assert ("PATCH", "dcim/interfaces/11/", {"primary_mac_address": 99}) in calls
    # returns the refreshed host device
    assert result.id == "5"
    assert result.serial == "SN-NEW"
    assert result.interfaces[0].mac_address == "AA:BB:CC:00:00:01"


async def test_update_dpu_reuses_existing_mac():
    # An existing MAC on the interface (different case) must be reused, not re-created.
    client = _client(existing_macs=[{"id": 42, "mac_address": "AA:BB:CC:00:00:01"}])
    try:
        await client.update_dpu_device_inventory("5", "SN-NEW", {"11": "aa:bb:cc:00:00:01"})
    finally:
        await client.close()
    calls = client._calls
    assert not any(c[0] == "POST" for c in calls), "should not create a duplicate MAC"
    assert ("PATCH", "dcim/interfaces/11/", {"primary_mac_address": 42}) in calls
