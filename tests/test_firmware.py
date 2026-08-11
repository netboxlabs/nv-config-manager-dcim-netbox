# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Behavioral tests for the firmware methods (stubbed transport)."""

from __future__ import annotations

import pytest

from tests._fake import fake_client

# Area marker for `make <area>` / `pytest -m <area>`.
pytestmark = pytest.mark.firmware
_DEVICE_PATH = "dcim/devices/1/"


async def test_get_os_image_versions():
    device = {
        "id": 1,
        "role": {"slug": "leaf"},
        "platform": {"slug": "cumulus-linux"},
        "config_context": {
            "intended-firmware": {"version": "5.1.0"},
            "ztp": {"ipv4": ["10.0.0.9"]},
        },
    }

    async def targets(name, site=None):
        return [{"data": {"firmware-targets": {"leaf": {"cumulus-linux": "5.2.0"}}}}]

    client = fake_client(
        rest_get={_DEVICE_PATH: device}, patch={"config_contexts_for_profile": targets}
    )
    assert await client.get_os_image_versions("1") == ("5.1.0", "5.2.0", "10.0.0.9")


async def test_get_firmware_bundle():
    device = {
        "config_context": {
            "firmware_bundle_version": "b1",
            "firmware_bundles": {
                "b1": {
                    "nv_os": {"version": "5.1.0"},
                    "firmware": {"BMC": {"file": "bmc.bin", "s3_path": "s3://x"}},
                }
            },
        }
    }
    bundle = await fake_client(rest_get={_DEVICE_PATH: device}).get_firmware_bundle("1")
    assert bundle.version == "b1" and bundle.desired_os_version == "5.1.0"
    assert "bmc" in bundle.components and bundle.components["bmc"].file_name == "bmc.bin"


async def test_set_intended_os_image_merges_local_context():
    record = []
    client = fake_client(
        rest_get={_DEVICE_PATH: {"local_context_data": {"existing": True}}}, record=record
    )
    await client.set_intended_os_image("1", "5.3.0")
    patch = next(body for method, _, body in record if method == "PATCH")
    local = patch["local_context_data"]
    assert local["existing"] is True  # deep-merge preserves siblings
    assert local["intended-firmware"]["version"] == "5.3.0"


async def test_set_device_firmware_intent_merges_local_context():
    record = []
    client = fake_client(rest_get={_DEVICE_PATH: {}}, record=record)
    await client.set_device_firmware_intent("1", "b2", "5.4.0")
    patch = next(body for method, _, body in record if method == "PATCH")
    local = patch["local_context_data"]
    assert local["firmware_bundle_version"] == "b2"
    assert local["intended-firmware"]["version"] == "5.4.0"
