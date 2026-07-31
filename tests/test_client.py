# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Settings validation, auth-scheme, and native-error mapping tests.

Covers the provider-contract requirements to validate required settings and map
native HTTP/GraphQL failures onto the SDK error taxonomy.
"""

from __future__ import annotations

import json

import aiohttp
import pytest
from nv_config_manager_dcim import (
    DCIMAuthenticationError,
    DCIMAuthorizationError,
    DCIMConflictError,
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


def test_extra_settings_keys_are_ignored():
    # NVCM's installer may pass provider-specific keys (e.g. dcim.options.*).
    # The provider reads only its connection keys and must ignore the rest, so
    # unknown options never break validation or client creation.
    settings = {
        **_OK,
        "options": {"tenant": "lab"},
        "tenant": "lab",
        "display_name": "NetBox",
        "unknown": "x",
    }
    NetBoxProvider().validate_settings(settings)  # no raise
    resolved = netbox_connection_settings(settings)
    assert resolved["server"] == _OK["server"]
    assert set(resolved) == {"server", "token", "public_url", "verify"}
    # create_client tolerates the extra keys too.
    client = create_dcim_client("netbox", settings)
    assert client._base_url == "https://netbox.example"


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
        (400, DCIMInvalidDataError),
        (401, DCIMAuthenticationError),
        (403, DCIMAuthorizationError),
        (404, DCIMNotFoundError),
        (409, DCIMConflictError),
        (500, DCIMConnectivityError),
    ],
)
def test_raise_status_maps_http_to_sdk_errors(status, expected):
    client = create_dcim_client("netbox", _OK)
    with pytest.raises(expected):
        client._raise_status(status, "response body")


def test_validate_settings_rejects_malformed_url():
    """A ``server`` without an http(s) scheme is a configuration error, not a
    late opaque transport failure at request time."""
    provider = NetBoxProvider()
    with pytest.raises(DCIMProviderConfigurationError):
        provider.validate_settings({"server": "not-a-url", "token": "t"})
    with pytest.raises(DCIMProviderConfigurationError):
        provider.validate_settings({"server": "netbox.example", "token": "t"})  # no scheme
    with pytest.raises(DCIMProviderConfigurationError):
        provider.validate_settings({"server": "ftp://netbox.example", "token": "t"})


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


class _RaisingCM:
    """Async context manager whose entry raises — models a transport failure."""

    def __init__(self, exc):
        self._exc = exc

    async def __aenter__(self):
        raise self._exc

    async def __aexit__(self, *exc):
        return False


class _RaisingSession:
    def __init__(self, exc):
        self._exc = exc

    def post(self, *args, **kwargs):
        return _RaisingCM(self._exc)

    def request(self, *args, **kwargs):
        return _RaisingCM(self._exc)


# A connection error and a timeout — the two native transport failures. Timeout is
# a builtin TimeoutError (== asyncio.TimeoutError), NOT an aiohttp.ClientError, so
# it would leak unmapped without the widened except.
_TRANSPORT_FAILURES = [aiohttp.ClientError("connection refused"), TimeoutError()]


@pytest.mark.parametrize("exc", _TRANSPORT_FAILURES)
async def test_graphql_transport_failure_maps_to_connectivity(exc):
    client = create_dcim_client("netbox", _OK)
    client._session = lambda: _RaisingSession(exc)
    try:
        with pytest.raises(DCIMConnectivityError):
            await client.graphql_query("query { device { id } }")
    finally:
        await client.close()


@pytest.mark.parametrize("exc", _TRANSPORT_FAILURES)
async def test_rest_transport_failure_maps_to_connectivity(exc):
    client = create_dcim_client("netbox", _OK)
    client._session = lambda: _RaisingSession(exc)
    try:
        with pytest.raises(DCIMConnectivityError):
            await client.rest_request("GET", "dcim/devices/")
    finally:
        await client.close()


class _FakeResponse:
    def __init__(self, status, text, *, json_exc=None, json_val=None):
        self.status = status
        self._text = text
        self._json_exc = json_exc
        self._json_val = json_val

    async def text(self):
        return self._text

    async def json(self, content_type=None):
        if self._json_exc is not None:
            raise self._json_exc
        return self._json_val


async def test_malformed_json_maps_to_invalid_data():
    """A 200 with a non-JSON body (e.g. an HTML error page) is contract-invalid,
    not a transport error — it must surface as DCIMInvalidDataError, not leak a
    raw ValueError."""
    client = create_dcim_client("netbox", _OK)
    response = _FakeResponse(
        200, "<html>not json</html>", json_exc=json.JSONDecodeError("x", "", 0)
    )
    try:
        with pytest.raises(DCIMInvalidDataError):
            await client._read(response)
    finally:
        await client.close()
