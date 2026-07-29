# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Settings validation, auth-scheme, and native-error mapping tests.

Covers the provider-contract requirements to validate required settings and map
native HTTP/GraphQL failures onto the SDK error taxonomy.
"""

from __future__ import annotations

import pytest
from nv_config_manager_dcim import (
    DCIMAuthenticationError,
    DCIMAuthorizationError,
    DCIMConnectivityError,
    DCIMInvalidDataError,
    DCIMNotFoundError,
    DCIMProviderConfigurationError,
    create_dcim_client,
)

from nv_config_manager_dcim_netbox.client import netbox_connection_settings
from nv_config_manager_dcim_netbox.provider import NetBoxProvider

_OK = {"server": "https://netbox.example", "token": "t"}


# --- required-settings validation ----------------------------------------------


def test_validate_settings_requires_server_and_token():
    provider = NetBoxProvider()
    provider.validate_settings(_OK)  # valid → no raise
    with pytest.raises(DCIMProviderConfigurationError):
        provider.validate_settings({})
    with pytest.raises(DCIMProviderConfigurationError):
        provider.validate_settings({"server": "https://netbox.example"})  # no token
    with pytest.raises(DCIMProviderConfigurationError):
        provider.validate_settings({"token": "t"})  # no server
    with pytest.raises(DCIMProviderConfigurationError):
        provider.validate_settings({"server": "  ", "token": "t"})  # blank server


def test_connection_settings_parses_verify():
    assert netbox_connection_settings({**_OK, "verify": "false"})["verify"] is False
    assert netbox_connection_settings({**_OK, "verify": "true"})["verify"] is True
    assert netbox_connection_settings({**_OK, "verify": "/etc/ca.pem"})["verify"] == "/etc/ca.pem"
    assert netbox_connection_settings(_OK)["verify"] is True  # default


# --- auth scheme (v1 Token vs v2 Bearer) ---------------------------------------


def test_auth_scheme_bearer_for_v2_token():
    client = create_dcim_client(
        "netbox", {"server": "https://netbox.example", "token": "nbt_key.secret"}
    )
    assert client._request_headers()["Authorization"] == "Bearer nbt_key.secret"


def test_auth_scheme_token_for_v1_token():
    client = create_dcim_client("netbox", {"server": "https://netbox.example", "token": "0123abcd"})
    assert client._request_headers()["Authorization"] == "Token 0123abcd"


# --- native failure -> SDK error mapping ---------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, DCIMAuthenticationError),
        (403, DCIMAuthorizationError),
        (404, DCIMNotFoundError),
        (500, DCIMConnectivityError),
        (400, DCIMConnectivityError),
    ],
)
def test_raise_status_maps_http_to_sdk_errors(status, expected):
    client = create_dcim_client("netbox", _OK)
    with pytest.raises(expected):
        client._raise_status(status, "response body")


class _FakeCM:
    async def __aenter__(self):
        return object()  # _read is stubbed, so the response object is unused

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def post(self, *args, **kwargs):
        return _FakeCM()


async def test_graphql_errors_map_to_invalid_data():
    """A GraphQL response carrying ``errors`` becomes ``DCIMInvalidDataError``."""
    client = create_dcim_client("netbox", _OK)

    async def fake_read(_response):
        return {"errors": [{"message": "boom"}]}

    client._session = lambda: _FakeSession()
    client._read = fake_read
    try:
        with pytest.raises(DCIMInvalidDataError):
            await client.graphql_query("query { device { id } }")
    finally:
        await client.close()
