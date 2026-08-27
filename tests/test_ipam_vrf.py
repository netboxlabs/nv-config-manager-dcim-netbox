# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""assign_vrf_to_device tests (transport stubbed).

NetBox has no device→VRF link, so a device's VRF is set on its interfaces.
"""

from __future__ import annotations

import pytest
from nv_config_manager_dcim import create_dcim_client

# Area marker for `make <area>` / `pytest -m <area>`.
pytestmark = pytest.mark.ipam
_SETTINGS = {"server": "https://netbox.example", "token": "t"}


def _client(interfaces):
    client = create_dcim_client("netbox", _SETTINGS)
    patches = []

    async def fake_rest_list(path, params=None):
        assert path == "dcim/interfaces/"
        return interfaces

    async def fake_rest_request(method, path, *, params=None, json=None):
        patches.append((method, path, json))
        return {}

    client.rest_list = fake_rest_list
    client.rest_request = fake_rest_request
    client._patches = patches
    return client


async def test_assign_vrf_patches_interfaces_not_already_in_vrf():
    interfaces = [
        {"id": 1, "vrf": None},
        {"id": 2, "vrf": {"id": 5}},
        {"id": 3, "vrf": {"id": 9}},  # already the target -> skipped
    ]
    client = _client(interfaces)
    try:
        await client.assign_vrf_to_device("7", "9")
    finally:
        await client.close()
    patched_paths = [p[1] for p in client._patches]
    assert patched_paths == ["dcim/interfaces/1/", "dcim/interfaces/2/"]
    assert all(p[2] == {"vrf": 9} for p in client._patches)


async def test_assign_vrf_noop_when_all_interfaces_already_set():
    client = _client([{"id": 1, "vrf": {"id": 9}}, {"id": 2, "vrf": 9}])
    try:
        await client.assign_vrf_to_device("7", "9")
    finally:
        await client.close()
    assert client._patches == []


async def test_assign_vrf_leaves_management_interfaces_alone():
    # Moving eth0 into a tenant VRF severs the management plane.
    interfaces = [
        {"id": 1, "name": "swp1", "vrf": None},
        {"id": 2, "name": "eth0", "vrf": None, "mgmt_only": True},
    ]
    client = _client(interfaces)
    try:
        await client.assign_vrf_to_device("7", "9")
    finally:
        await client.close()
    assert [p[1] for p in client._patches] == ["dcim/interfaces/1/"]


async def test_remove_unmapped_device_vrfs_is_a_noop():
    client = _client([])
    try:
        assert await client.remove_unmapped_device_vrfs("7", ["9"]) == []
    finally:
        await client.close()
