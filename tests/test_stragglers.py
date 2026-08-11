# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Behavioral tests for the remaining small methods across ipam/secrets/client."""

from __future__ import annotations

import pytest
from nv_config_manager_dcim import DeviceVRF

from tests._fake import fake_client

# Area marker for `make <area>` / `pytest -m <area>`.
pytestmark = [pytest.mark.ipam, pytest.mark.secrets]


async def test_get_device_vrfs_dedupes_by_id():
    rows = [
        {"vrf": {"id": 8, "name": "Tenant-A"}},
        {"vrf": {"id": 8, "name": "Tenant-A"}},  # duplicate collapses
        {"vrf": None},
    ]
    result = await fake_client(rest_list={"dcim/interfaces/": rows}).get_device_vrfs("1")
    assert result == [DeviceVRF(vrf_id="8", vrf_name="Tenant-A")]
    assert isinstance(result[0], DeviceVRF)


async def test_assign_vrf_to_interface_patches_vrf():
    calls = []
    await fake_client(record=calls).assign_vrf_to_interface("11", "8")
    assert calls == [("PATCH", "dcim/interfaces/11/", {"vrf": 8})]


async def test_get_device_password_mapping_users():
    async def context(device_id):
        return {"password_mappings": {"admin": {}, "api_user": {}}}

    client = fake_client(patch={"_device_config_context": context})
    assert await client.get_device_password_mapping_users("1") == {"admin", "api_user"}


async def test_get_device_password_mapping_users_empty():
    async def context(device_id):
        return {}

    client = fake_client(patch={"_device_config_context": context})
    assert await client.get_device_password_mapping_users("1") == set()


async def test_config_contexts_for_profile_filters_by_profile_name():
    rows = [
        {"profile": {"name": "wanted"}, "data": {"a": 1}},
        {"profile": {"name": "other"}, "data": {"b": 2}},
        {"profile": None},
    ]
    client = fake_client(rest_list={"extras/config-contexts/": rows})
    result = await client.config_contexts_for_profile("wanted")
    assert [c["data"] for c in result] == [{"a": 1}]
