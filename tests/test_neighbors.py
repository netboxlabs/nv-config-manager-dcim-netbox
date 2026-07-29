# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Behavioral tests for get_intended_interface_neighbors (stubbed transport)."""

from __future__ import annotations

import pytest
from nv_config_manager_dcim import DCIMInvalidDataError

from tests._fake import fake_client


def _client(device):
    return fake_client(gql={"device": device})


async def test_neighbors_with_connected_endpoint():
    device = {
        "interfaces": [
            {
                "name": "swp1",
                "enabled": True,
                "tags": [{"name": "uplink"}],
                "connected_endpoints": [
                    {
                        "name": "swp10",
                        "primary_mac_address": {"mac_address": "aa:bb:cc:00:00:02"},
                        "device": {
                            "name": "spine-01",
                            "serial": "S9",
                            "position": 2,
                            "role": {"name": "Spine"},
                            "rack": {"name": "R2"},
                        },
                    }
                ],
            }
        ]
    }
    result = await _client(device).get_intended_interface_neighbors("1")
    assert len(result) == 1
    neighbor = result[0]
    assert neighbor.name == "swp1" and neighbor.tags == ("uplink",)
    assert neighbor.connected_interface_name == "swp10"
    assert neighbor.connected_device.name == "spine-01"
    assert neighbor.connected_device.role == "Spine"


async def test_neighbors_without_endpoint():
    device = {"interfaces": [{"name": "swp2", "enabled": True, "tags": []}]}
    result = await _client(device).get_intended_interface_neighbors("1")
    assert len(result) == 1 and result[0].name == "swp2"
    assert result[0].connected_device is None


async def test_neighbors_skips_disabled():
    device = {"interfaces": [{"name": "swp3", "enabled": False}]}
    assert await _client(device).get_intended_interface_neighbors("1") == []


async def test_neighbors_endpoint_without_device_name_raises():
    device = {
        "interfaces": [
            {"name": "swp4", "enabled": True, "connected_endpoints": [{"name": "x", "device": {}}]}
        ]
    }
    with pytest.raises(DCIMInvalidDataError):
        await _client(device).get_intended_interface_neighbors("1")


async def test_neighbors_no_device_returns_empty():
    assert await _client(None).get_intended_interface_neighbors("1") == []
