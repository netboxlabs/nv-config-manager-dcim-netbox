# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""NetBox data shapes that are legal but awkward, and must not crash a read.

``Device.name`` is nullable, related FKs arrive nested or bare, and records can be
deleted between a list and the follow-up fetch. Each of those used to escape as a
``KeyError`` / ``AttributeError`` / unhandled 404 rather than an SDK error or a
skipped row.
"""

from __future__ import annotations

import pytest
from nv_config_manager_dcim import DCIMInvalidDataError, DCIMNotFoundError
from nv_config_manager_dcim.workflow_models import DeviceInventoryFilter

from nv_config_manager_dcim_netbox._mappers import fk_id, require_device_name
from tests._fake import fake_client

# Area marker for `make <area>` / `pytest -m <area>`.
pytestmark = pytest.mark.inventory

_DEVICES = "dcim/devices/"
_FILTER = DeviceInventoryFilter(site="dc1")


def _device(**overrides):
    base = {
        "id": 1,
        "name": "leaf1",
        "platform": {"slug": "nv-os"},
        "role": {"slug": "leaf"},
        "site": {"name": "dc1"},
        "device_type": {"slug": "sn5600"},
    }
    return {**base, **overrides}


@pytest.mark.inventory
async def test_unnamed_devices_are_skipped_not_crashed():
    # NetBox permits Device.name = null; such a device cannot be keyed by name, so
    # it is skipped with a warning rather than raising KeyError.
    client = fake_client(
        rest_list={_DEVICES: [_device(id=1, name=None), _device(id=2, name="leaf2")]}
    )
    devices = await client.get_network_devices(_FILTER)
    assert [d.name for d in devices] == ["leaf2"]


@pytest.mark.inventory
async def test_two_unnamed_devices_are_not_a_duplicate_name_conflict():
    # Both are unnamed, which is not the same as two devices sharing a name.
    client = fake_client(rest_list={_DEVICES: [_device(id=1, name=None), _device(id=2, name=None)]})
    assert await client.get_network_devices(_FILTER) == []


@pytest.mark.inventory
async def test_genuinely_duplicated_names_still_raise():
    client = fake_client(rest_list={_DEVICES: [_device(id=1), _device(id=2)]})
    with pytest.raises(DCIMInvalidDataError):
        await client.get_network_devices(_FILTER)


@pytest.mark.inventory
async def test_single_device_read_reports_a_missing_name_as_invalid_data():
    # A read of one specific device cannot skip it, so it must fail in the SDK
    # taxonomy rather than with a KeyError.
    client = fake_client(rest_get={f"{_DEVICES}1/": _device(name=None)})
    with pytest.raises(DCIMInvalidDataError):
        await client.get_network_device("1")


def test_require_device_name_names_the_device_in_the_error():
    with pytest.raises(DCIMInvalidDataError, match="7"):
        require_device_name({"id": 7})


def test_fk_id_reads_both_serializer_shapes():
    assert fk_id({"id": 5, "name": "x"}) == 5
    assert fk_id(5) == 5
    assert fk_id(None) is None


@pytest.mark.dhcp
async def test_dhcp_skips_an_ip_whose_device_was_deleted():
    """A stale interface->device reference must skip that address, not fail the
    whole subnet fetch (the interface path already behaved this way)."""
    client = fake_client(
        rest_list={
            "ipam/prefixes/": [{"id": 1, "prefix": "10.0.0.0/24", "family": 4}],
            "ipam/ip-addresses/": [
                {
                    "address": "10.0.0.10/24",
                    "assigned_object_type": "dcim.interface",
                    "assigned_object": {"id": 11},
                }
            ],
            "dcim/interfaces/": [{"id": 11, "name": "swp1", "device": {"id": 99}}],
        },
    )

    async def missing_device(method, path, **kwargs):
        if path.startswith(_DEVICES):
            raise DCIMNotFoundError("gone")
        return None

    client.rest_request = missing_device
    subnets = await client.get_dhcp_auto_subnets()
    assert len(subnets) == 1
    assert subnets[0]["reservations"] == []
    assert subnets[0]["option_candidates"] == []
