# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tests for NetBox cable-validation status persistence."""

from __future__ import annotations

import pytest
from nv_config_manager_dcim import (
    CableStatus,
    CableStatusUpdate,
    DCIMCableStatusClient,
    DCIMInvalidDataError,
    DCIMNotFoundError,
)

from tests._fake import fake_client


def _update(status: CableStatus = CableStatus.INVALID) -> CableStatusUpdate:
    return CableStatusUpdate(
        device_id="17",
        interface_name="Ethernet1/1",
        status=status,
        workflow_id="cable-validation-1",
    )


@pytest.mark.asyncio
async def test_update_cable_status_sets_custom_field_and_preserves_others():
    writes = []
    queries = []
    client = fake_client(
        rest_list={"dcim/interfaces/": [{"id": 31, "name": "Ethernet1/1", "cable": {"id": 42}}]},
        rest_get={
            "dcim/cables/42/": {
                "id": 42,
                "status": {"value": "connected", "label": "Connected"},
                "custom_fields": {"existing": "keep-me"},
            }
        },
        record=writes,
        queries=queries,
    )

    await client.update_cable_status(_update())

    assert queries == [("dcim/interfaces/", [("device_id", "17"), ("name", "Ethernet1/1")])]
    assert writes == [
        ("GET", "dcim/cables/42/", None),
        (
            "PATCH",
            "dcim/cables/42/",
            {"custom_fields": {"validation_status": "Invalid"}},
        ),
    ]


@pytest.mark.asyncio
async def test_update_cable_status_is_idempotent():
    writes = []
    client = fake_client(
        rest_list={
            "dcim/interfaces/": [{"id": 31, "cable": {"id": 42}}],
        },
        rest_get={
            "dcim/cables/42/": {
                "id": 42,
                "custom_fields": {"validation_status": "Connected"},
            }
        },
        record=writes,
    )

    await client.update_cable_status(_update(CableStatus.CONNECTED))

    assert writes == [("GET", "dcim/cables/42/", None)]


@pytest.mark.asyncio
async def test_update_cable_status_rejects_interface_without_cable():
    client = fake_client(
        rest_list={"dcim/interfaces/": [{"id": 31, "cable": None}]},
    )

    with pytest.raises(DCIMNotFoundError, match="has no cable"):
        await client.update_cable_status(_update())


@pytest.mark.asyncio
async def test_update_cable_status_rejects_unknown_interface():
    writes = []
    client = fake_client(rest_list={"dcim/interfaces/": []}, record=writes)

    with pytest.raises(DCIMNotFoundError, match="No interface named 'Ethernet1/1'"):
        await client.update_cable_status(_update())

    assert writes == []


@pytest.mark.asyncio
async def test_update_cable_status_rejects_non_mapping_custom_fields():
    writes = []
    client = fake_client(
        rest_list={"dcim/interfaces/": [{"id": 31, "cable": {"id": 42}}]},
        rest_get={"dcim/cables/42/": {"id": 42, "custom_fields": ["validation_status"]}},
        record=writes,
    )

    with pytest.raises(DCIMInvalidDataError, match="invalid cable custom-field data"):
        await client.update_cable_status(_update())

    assert writes == [("GET", "dcim/cables/42/", None)]


def test_netbox_exposes_cable_status_capability():
    assert isinstance(fake_client(), DCIMCableStatusClient)
