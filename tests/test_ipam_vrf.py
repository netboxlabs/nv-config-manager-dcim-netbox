# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Device/VRF association tests (transport stubbed).

NetBox has no device→VRF link: the ports stage is the association, so the
device-level calls change nothing.
"""

from __future__ import annotations

import pytest
from nv_config_manager_dcim import create_dcim_client

# Area marker for `make <area>` / `pytest -m <area>`.
pytestmark = pytest.mark.ipam
_SETTINGS = {"server": "https://netbox.example", "token": "t"}


def _client(interfaces):
    client = create_dcim_client("netbox", _SETTINGS)
    requests = []

    async def fake_rest_list(path, params=None):
        assert path == "dcim/interfaces/"
        return interfaces

    async def fake_rest_request(method, path, *, params=None, json=None):
        requests.append((method, path, json))
        return {}

    client.rest_list = fake_rest_list
    client.rest_request = fake_rest_request
    client._requests = requests
    return client


async def test_assign_vrf_to_device_touches_no_interfaces():
    # On a switch shared between tenants, writing the VRF onto every port would
    # move the other tenant's ports; the ports stage assigns the selected ones.
    interfaces = [
        {"id": 1, "name": "swp1", "vrf": {"id": 5}},  # another tenant's port
        {"id": 2, "name": "swp2", "vrf": None},
        {"id": 3, "name": "eth0", "vrf": None, "mgmt_only": True},
    ]
    client = _client(interfaces)
    try:
        await client.assign_vrf_to_device("7", "9")
    finally:
        await client.close()
    assert client._requests == []


async def test_get_device_vrfs_is_the_union_of_interface_vrfs():
    interfaces = [
        {"id": 1, "vrf": {"id": 5, "name": "SpXTenant60000"}},
        {"id": 2, "vrf": {"id": 5, "name": "SpXTenant60000"}},
        {"id": 3, "vrf": {"id": 9, "name": "SpXTenant60001"}},
        {"id": 4, "vrf": None},
    ]
    client = _client(interfaces)
    try:
        vrfs = await client.get_device_vrfs("7")
    finally:
        await client.close()
    assert sorted((v.vrf_id, v.vrf_name) for v in vrfs) == [
        ("5", "SpXTenant60000"),
        ("9", "SpXTenant60001"),
    ]


async def test_remove_unmapped_device_vrfs_is_a_noop():
    client = _client([])
    try:
        assert await client.remove_unmapped_device_vrfs("7", ["9"]) == []
    finally:
        await client.close()
    assert client._requests == []
