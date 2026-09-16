# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Behavioral tests for the ZTP methods (stubbed transport)."""

from __future__ import annotations

import pytest
from nv_config_manager_dcim import DCIMInvalidDataError, DCIMNotFoundError

from tests._fake import fake_client

# Area marker for `make <area>` / `pytest -m <area>`.
pytestmark = pytest.mark.ztp
_STATUS = "plugins/nv-config-manager/device-config-statuses/"
_INTENDED = "plugins/nv-config-manager/intended-configs/"


def _device(**overrides):
    device = {
        "id": "1",
        "name": "leaf-01",
        "platform": {"name": "Cumulus Linux"},
        "config_context": {"intended-firmware": {"version": "5.1.0"}},
        "interfaces": [{"ip_addresses": [{"address": "10.0.0.5/24"}]}],
    }
    device.update(overrides)
    return device


def _client(device, *, managed=True, intended=None):
    rest_list = {_STATUS: [{"id": 10, "device": 1}] if managed else []}
    if intended is not None:
        rest_list[_INTENDED] = intended
    return fake_client(gql={"device": device}, rest_list=rest_list)


async def test_get_ztp_device():
    client = _client(
        _device(), intended=[{"config_store_instance": "https://api-mtls.nvcm.example"}]
    )
    ztp = await client.get_ztp_device("1")
    assert ztp.name == "leaf-01"
    assert ztp.platform_name == "Cumulus Linux"
    assert ztp.firmware_version == "5.1.0"
    assert ztp.addresses == ["10.0.0.5"]
    # device_id is the NetBox pk keying the config-store entries (boot-script /
    # startup.yaml) that the ZTP service reads.
    assert ztp.device_id == "1"
    # config_store_instance is the intended-config's value verbatim, as the
    # reference passes it; NVCM only tests it for None before using it.
    assert ztp.config_store_instance == "https://api-mtls.nvcm.example"


async def test_get_ztp_device_without_intended_config_has_no_store_instance():
    ztp = await _client(_device(), intended=[]).get_ztp_device("1")
    assert ztp.config_store_instance is None


async def test_get_ztp_device_missing_raises():
    with pytest.raises(DCIMNotFoundError):
        await _client(None).get_ztp_device("1")


async def test_get_ztp_device_requires_a_managed_record():
    # Parity: the reference reads `config_manager_device`, so a device with no
    # DeviceConfigStatus row is not found for ZTP even though the device exists.
    with pytest.raises(DCIMNotFoundError):
        await _client(_device(), managed=False).get_ztp_device("1")


async def test_get_ztp_device_requires_a_platform():
    # ZTPDevice.platform_name drives the boot-script selection; an empty string
    # would silently serve the wrong one.
    with pytest.raises(DCIMInvalidDataError):
        await _client(_device(platform=None)).get_ztp_device("1")


async def test_mark_ztp_device_provisioned_patches_status():
    calls = []
    await fake_client(record=calls).mark_ztp_device_provisioned("7")
    assert len(calls) == 1
    method, path, body = calls[0]
    assert method == "PATCH" and path == "dcim/devices/7/"
    assert body["status"] == "provisioned"


async def test_get_ztp_device_unnamed_device_raises_the_sdk_error():
    # Device.name is nullable and ZTPDevice.name is required; pydantic raises a
    # ValueError, which the (KeyError, TypeError) handler does not catch.
    with pytest.raises(DCIMInvalidDataError):
        await _client(_device(name=None)).get_ztp_device("1")
