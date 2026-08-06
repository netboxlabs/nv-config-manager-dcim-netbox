# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Behavioral tests for core inventory methods (stubbed transport)."""

from __future__ import annotations

import pytest
from nv_config_manager_dcim import (
    DCIMConflictError,
    DCIMDeviceSelectionFilter,
    DCIMNotFoundError,
    DeviceInventoryFilter,
)

from tests._fake import fake_client as _client

_GQL_DEVICE = {
    "id": "1",
    "name": "leaf-01",
    "role": {"name": "Leaf", "slug": "leaf"},
    "platform": {"name": "Cumulus Linux", "slug": "cumulus-linux"},
    "rack": {"name": "R1"},
    "site": {"name": "DM-Akron"},
    "primary_ip4": {"address": "10.0.0.1/24"},
    "serial": "ABC123",
}
_REST_DEVICE = {
    "id": 1,
    "name": "leaf-01",
    "role": {"slug": "leaf"},
    "platform": {"slug": "cumulus-linux"},
    "device_type": {"slug": "sn2201"},
    "site": {"name": "DM-Akron"},
    "rack": {"name": "R1"},
    "primary_ip4": {"address": "10.0.0.1/24"},
}


async def test_get_device_metadata():
    md = await _client(gql={"device": _GQL_DEVICE}).get_device_metadata("1")
    assert md is not None and md.name == "leaf-01" and md.site == "DM-Akron"


async def test_get_device_metadata_missing_returns_none():
    assert await _client(gql={"device": None}).get_device_metadata("1") is None


async def test_get_device_serial():
    assert await _client(gql={"device": {"serial": "ABC123"}}).get_device_serial("1") == "ABC123"


async def test_get_device_serial_missing_raises():
    with pytest.raises(DCIMNotFoundError):
        await _client(gql={"device": {"serial": None}}).get_device_serial("1")


async def test_get_device_selection_by_name():
    gql = {"device_list": [{"id": "1", "name": "leaf-01", "platform": {"slug": "cumulus-linux"}}]}
    sel = await _client(gql=gql).get_device_selection_by_name("leaf-01")
    assert (sel.id, sel.name, sel.platform) == ("1", "leaf-01", "cumulus-linux")


async def test_get_device_selection_by_name_none_raises():
    with pytest.raises(DCIMNotFoundError):
        await _client(gql={"device_list": []}).get_device_selection_by_name("nope")


async def test_get_device_selection_by_name_multiple_raises():
    gql = {"device_list": [{"id": "1", "name": "x"}, {"id": "2", "name": "x"}]}
    with pytest.raises(DCIMConflictError):
        await _client(gql=gql).get_device_selection_by_name("x")


async def test_list_devices():
    client = _client(rest_list={"dcim/devices/": [_REST_DEVICE]})
    result = await client.list_devices(DCIMDeviceSelectionFilter())
    assert [(s.id, s.name, s.platform) for s in result] == [("1", "leaf-01", "cumulus-linux")]


async def test_get_managed_device_metadata():
    client = _client(rest_list={"dcim/devices/": [_REST_DEVICE]})
    result = await client.get_managed_device_metadata()
    assert [m.name for m in result] == ["leaf-01"]


async def test_get_network_device():
    client = _client(rest_get={"dcim/devices/1/": _REST_DEVICE})
    dev = await client.get_network_device("1")
    assert dev.name == "leaf-01" and dev.site == "DM-Akron"


async def test_get_network_device_missing_raises():
    with pytest.raises(DCIMNotFoundError):
        await _client(rest_get={}).get_network_device("1")


async def test_get_network_devices():
    client = _client(rest_list={"dcim/devices/": [_REST_DEVICE]})
    # An entirely unfiltered request now raises; supply a real filter (the mock
    # returns the canned device regardless of the params sent).
    result = await client.get_network_devices(DeviceInventoryFilter(roles=["leaf"]))
    assert [d.name for d in result] == ["leaf-01"]


async def test_get_device_interfaces():
    iface = {"id": 11, "name": "swp1", "device": {"name": "leaf-01"}, "vrf": {"id": 8}}
    client = _client(rest_list={"dcim/interfaces/": [iface]})
    result = await client.get_device_interfaces("1")
    assert [i.name for i in result] == ["swp1"] and result[0].vrf_id == "8"
