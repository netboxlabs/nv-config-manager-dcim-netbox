# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Behavioral tests for the host / MAC-correlation methods (stubbed transport)."""

from __future__ import annotations

import pytest
from nv_config_manager_dcim import DCIMNotFoundError

from tests._fake import fake_client as _client

_HOST_GQL = {
    "id": "1",
    "name": "dpu-01",
    "serial": "S1",
    "position": 1,
    "role": {"slug": "dpu"},
    "site": {"name": "DM-Akron"},
    "rack": {"name": "R1"},
    "device_type": {"slug": "bluefield"},
    "tenant": {"name": "NGC"},
    "devicebays": [],
    "interfaces": [
        {
            "id": 11,
            "name": "p0",
            "primary_mac_address": {"mac_address": "aa:bb:cc:00:00:01"},
            "vrf": {"id": 8},
        }
    ],
}


async def test_get_interface_hosts_by_mac():
    iface = {"id": 11, "name": "p0", "device": {"name": "dpu-01"}, "vrf": {"id": 8}}
    client = _client(rest_list={"dcim/interfaces/": [iface]})
    result = await client.get_interface_hosts_by_mac(["aa:bb:cc:00:00:01"])
    assert [i.name for i in result] == ["p0"] and result[0].vrf_id == "8"


async def test_find_host_devices_by_mac():
    # interfaces -> device ids -> host_devices_by_ids (graphql device_list)
    client = _client(
        rest_list={"dcim/interfaces/": [{"id": 11, "device": {"id": 1}}]},
        gql={"device_list": [_HOST_GQL]},
    )
    result = await client.find_host_devices_by_mac("aa:bb:cc:00:00:01")
    assert [h.name for h in result] == ["dpu-01"]


async def test_get_host_metadata_by_macs():
    client = _client(
        rest_list={"dcim/interfaces/": [{"id": 11, "device": {"id": 1}}]},
        gql={"device_list": [_HOST_GQL]},
    )
    result = await client.get_host_metadata_by_macs(["aa:bb:cc:00:00:01"])
    assert [m.name for m in result] == ["dpu-01"]


async def test_get_host_metadata_by_macs_empty():
    assert await _client().get_host_metadata_by_macs([]) == []


async def test_get_host_metadata_by_names():
    client = _client(gql={"device_list": [_HOST_GQL]})
    result = await client.get_host_metadata_by_names(["dpu-01"])
    assert [m.name for m in result] == ["dpu-01"]


async def test_get_connected_switch_port_by_remote_mac():
    gql = {
        "mac_address_list": [
            {"assigned_object": {"connected_endpoints": [{"name": "swp5", "device": {"id": 2}}]}}
        ]
    }
    switch_rest = {
        "id": 2,
        "name": "leaf-02",
        "role": {"slug": "leaf"},
        "platform": {"slug": "cumulus-linux"},
        "device_type": {"slug": "sn2201"},
        "site": {"name": "DM-Akron"},
    }
    client = _client(gql=gql, rest_get={"dcim/devices/2/": switch_rest})
    switch, port = await client.get_connected_switch_port_by_remote_mac("aa:bb:cc:00:00:01")
    assert switch.name == "leaf-02" and port == "swp5"


async def test_get_connected_switch_port_not_found_raises():
    client = _client(gql={"mac_address_list": []})
    with pytest.raises(DCIMNotFoundError):
        await client.get_connected_switch_port_by_remote_mac("aa:bb:cc:00:00:01")


# get_host_device / get_host_devices live in the inventory mixin but share the
# host GraphQL shape, so they are exercised here alongside the MAC lookups.


async def test_get_host_device():
    client = _client(gql={"device": _HOST_GQL})
    host = await client.get_host_device("1")
    assert host.name == "dpu-01" and host.serial == "S1"


async def test_get_host_device_missing_raises():
    client = _client(gql={"device": None})
    with pytest.raises(DCIMNotFoundError):
        await client.get_host_device("1")


async def test_get_host_devices():
    from nv_config_manager_dcim import DeviceInventoryFilter

    client = _client(
        rest_list={"dcim/devices/": [{"id": 1, "name": "dpu-01"}]},
        gql={"device_list": [_HOST_GQL]},
    )
    result = await client.get_host_devices(DeviceInventoryFilter())
    assert [h.name for h in result] == ["dpu-01"]
