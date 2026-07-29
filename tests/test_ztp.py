# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Behavioral tests for the ZTP methods (stubbed transport)."""

from __future__ import annotations

import pytest
from nv_config_manager_dcim import DCIMNotFoundError

from tests._fake import fake_client


async def test_get_ztp_device():
    device = {
        "id": "1",
        "name": "leaf-01",
        "platform": {"name": "Cumulus Linux"},
        "config_context": {"intended-firmware": {"version": "5.1.0"}},
        "interfaces": [{"ip_addresses": [{"address": "10.0.0.5/24"}]}],
    }
    ztp = await fake_client(gql={"device": device}).get_ztp_device("1")
    assert ztp.name == "leaf-01"
    assert ztp.platform_name == "Cumulus Linux"
    assert ztp.firmware_version == "5.1.0"
    assert ztp.addresses == ["10.0.0.5"]


async def test_get_ztp_device_missing_raises():
    with pytest.raises(DCIMNotFoundError):
        await fake_client(gql={"device": None}).get_ztp_device("1")


async def test_mark_ztp_device_provisioned_patches_status():
    calls = []
    await fake_client(record=calls).mark_ztp_device_provisioned("7")
    assert len(calls) == 1
    method, path, body = calls[0]
    assert method == "PATCH" and path == "dcim/devices/7/"
    assert "status" in body and body["status"]
