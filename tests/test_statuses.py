# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""list_statuses tests (transport stubbed).

NetBox statuses are per-model choice fields read from the endpoint's OPTIONS
response, not first-class objects.
"""

from __future__ import annotations

import pytest
from nv_config_manager_dcim import DCIMInvalidDataError, create_dcim_client

_SETTINGS = {"server": "https://netbox.example", "token": "t"}

_DEVICE_OPTIONS = {
    "actions": {
        "POST": {
            "status": {
                "choices": [
                    {"value": "offline", "display_name": "Offline"},
                    {"value": "active", "display_name": "Active"},
                    {"value": "planned", "display_name": "Planned"},
                ]
            }
        }
    }
}


def _client(options_by_endpoint):
    client = create_dcim_client("netbox", _SETTINGS)
    seen = {}

    async def fake_rest_request(method, path, *, params=None, json=None):
        assert method == "OPTIONS"
        seen["endpoint"] = path
        return options_by_endpoint.get(path, {})

    client.rest_request = fake_rest_request
    client._seen = seen
    return client


async def test_list_statuses_maps_options_choices():
    client = _client({"dcim/devices/": _DEVICE_OPTIONS})
    try:
        result = await client.list_statuses("dcim.device")
    finally:
        await client.close()
    assert client._seen["endpoint"] == "dcim/devices/"
    assert [(s.id, s.name) for s in result] == [
        ("offline", "Offline"),
        ("active", "Active"),
        ("planned", "Planned"),
    ]


async def test_list_statuses_defaults_to_device():
    client = _client({"dcim/devices/": _DEVICE_OPTIONS})
    try:
        result = await client.list_statuses(None)
    finally:
        await client.close()
    assert client._seen["endpoint"] == "dcim/devices/"
    assert {s.id for s in result} == {"offline", "active", "planned"}


async def test_list_statuses_maps_content_type_to_endpoint():
    client = _client({"ipam/prefixes/": {"actions": {"POST": {"status": {"choices": []}}}}})
    try:
        result = await client.list_statuses("ipam.prefix")
    finally:
        await client.close()
    assert client._seen["endpoint"] == "ipam/prefixes/"
    assert result == []


async def test_list_statuses_unknown_content_type_raises():
    client = _client({})
    try:
        with pytest.raises(DCIMInvalidDataError):
            await client.list_statuses("dcim.nonexistent")
    finally:
        await client.close()
