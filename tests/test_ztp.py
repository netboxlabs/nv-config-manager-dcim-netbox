# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Behavioral tests for the ZTP methods (stubbed transport)."""

from __future__ import annotations

import pytest
from nv_config_manager_dcim import DCIMInvalidDataError, DCIMNotFoundError

from tests._fake import fake_client

# Area marker for `make <area>` / `pytest -m <area>`.
pytestmark = pytest.mark.ztp


async def test_get_ztp_device():
    device = {
        "id": "1",
        "name": "leaf-01",
        "platform": {"name": "Cumulus Linux"},
        "config_context": {"intended-firmware": {"version": "5.1.0"}},
        "interfaces": [{"ip_addresses": [{"address": "10.0.0.5/24"}]}],
    }
    client = fake_client(
        gql={"device": device},
        rest_list={
            "plugins/nv-config-manager/intended-configs/": [
                {"config_store_instance": "https://ui.nvcm.example"}
            ]
        },
    )
    ztp = await client.get_ztp_device("1")
    assert ztp.name == "leaf-01"
    assert ztp.platform_name == "Cumulus Linux"
    assert ztp.firmware_version == "5.1.0"
    assert ztp.addresses == ["10.0.0.5"]
    # device_id is the NetBox pk keying the config-store entries (boot-script /
    # startup.yaml) that the ZTP service reads.
    assert ztp.device_id == "1"
    # config_store_instance is read from the intended-config (render pipeline set it)
    # and ui->api-mtls transformed, matching the reference DCIM provider. Without it,
    # ZTP load_file 404s ("No config store file found").
    assert ztp.config_store_instance == "https://api-mtls.nvcm.example"


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        # The canonical form, matching the reference provider.
        ("https://ui.config-store.example.com/", "https://api-mtls.config-store.example.com/"),
        # `ui` inside a later label must survive: an unanchored replace turned this
        # into `https://config-api-mtls.build-sapi-mtlste.example.com`, a host that
        # does not resolve, and ZTP failed with no indication the URL was rewritten.
        ("https://config-ui.build-suite.example.com", None),
        ("https://ui.guilford.example.com", "https://api-mtls.guilford.example.com"),
        # Only the first label is swapped, even when a later one also matches.
        ("https://ui.ui.example.com", "https://api-mtls.ui.example.com"),
        # No leading `ui` label at all: returned unchanged (and warned about).
        ("https://config-store.example.com", None),
    ],
)
async def test_ztp_config_store_endpoint_rewrites_only_the_leading_ui_label(stored, expected):
    client = fake_client(
        gql={"device": {"id": "1", "name": "leaf-01", "interfaces": []}},
        rest_list={
            "plugins/nv-config-manager/intended-configs/": [{"config_store_instance": stored}]
        },
    )
    ztp = await client.get_ztp_device("1")
    assert ztp.config_store_instance == (expected if expected is not None else stored)


async def test_get_ztp_device_missing_raises():
    with pytest.raises(DCIMNotFoundError):
        await fake_client(gql={"device": None}).get_ztp_device("1")


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
    client = fake_client(gql={"device": {"id": "1", "name": None, "interfaces": []}})
    with pytest.raises(DCIMInvalidDataError):
        await client.get_ztp_device("1")
