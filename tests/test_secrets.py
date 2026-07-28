# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Config-context-driven secret-reference reads (transport stubbed).

These surface secret references (names / versions) from the device config
context; no secret store is involved.
"""

from __future__ import annotations

from nv_config_manager_dcim import create_dcim_client

_SETTINGS = {"server": "https://netbox.example", "token": "t"}


def _client_with_context(config_context):
    client = create_dcim_client("netbox", _SETTINGS)

    async def fake_graphql(query, variables=None):
        return {"device": {"config_context": config_context}}

    client.graphql_query = fake_graphql
    return client


async def test_secret_versions_maps_context():
    client = _client_with_context({"secrets_versions": {"bmc": 3, "os": "v2", "skip": None}})
    try:
        result = await client.get_device_secret_versions("1")
    finally:
        await client.close()
    assert result == {"bmc": "3", "os": "v2"}  # None dropped, values stringified


async def test_secret_versions_absent_is_empty():
    client = _client_with_context({})
    try:
        assert await client.get_device_secret_versions("1") == {}
    finally:
        await client.close()


async def test_password_secret_names_maps_context():
    client = _client_with_context(
        {
            "password_mappings": {
                "admin": {"password": "type1", "rotation": "90"},
                "root": {"password": "type2", "rotation": ""},
                "bad": "not-a-dict",
            }
        }
    )
    try:
        result = await client.get_device_password_secret_names("1")
    finally:
        await client.close()
    assert result == {"admin": "type1_90", "root": "type2_"}  # non-dict entry skipped


async def test_password_secret_names_absent_is_empty():
    client = _client_with_context({"password_mappings": "nope"})
    try:
        assert await client.get_device_password_secret_names("1") == {}
    finally:
        await client.close()
