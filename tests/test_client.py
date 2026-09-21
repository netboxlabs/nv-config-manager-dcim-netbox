# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Settings validation, auth-scheme, and native-error mapping tests.

Covers the provider-contract requirements to validate required settings and map
native HTTP/GraphQL failures onto the SDK error taxonomy.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

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

from nv_config_manager_dcim_netbox.client import BaseNetBoxClient, netbox_connection_settings
from nv_config_manager_dcim_netbox.provider import NetBoxProvider

# Area marker for `make <area>` / `pytest -m <area>`.
pytestmark = pytest.mark.lifecycle

_OK = {"server": "https://netbox.example", "token": "t"}


async def test_find_switches_by_vlan_uses_direct_interface_filter():
    client = BaseNetBoxClient("https://netbox.example", "token")
    client.rest_list = AsyncMock(
        return_value=[
            {"device": {"id": 3}},
            {"device": {"id": 5}},
            {"device": {"id": 3}},
            {"device": None},
        ]
    )

    assert await client.find_switches_by_vlan(900) == ["3", "5"]
    client.rest_list.assert_awaited_once_with("dcim/interfaces/", [("vlan", "900")])


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
    assert set(resolved) == {"server", "token", "public_url", "verify", "timeout"}
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


def test_connection_settings_strip_surrounding_whitespace():
    # Validation ran on a stripped copy but stored the raw value, so a stray space
    # passed validate_settings and then made aiohttp reject every URL.
    settings = netbox_connection_settings(
        {"server": "  https://netbox.example  ", "token": " t \n", "public_url": " http://ui  "}
    )
    assert settings["server"] == "https://netbox.example"
    assert settings["token"] == "t"
    assert settings["public_url"] == "http://ui"
    client = create_dcim_client("netbox", {"server": " https://netbox.example ", "token": "t"})
    assert client._url("/graphql/") == "https://netbox.example/graphql/"


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


async def test_graphql_missing_object_maps_to_not_found():
    """NetBox reports a missing singular object as a GraphQL *error* with data=null,
    not as a null field. Verified against NetBox 4.7: querying `location(id:)` for an
    absent id returns exactly the payload below. It must be distinguishable from a
    real query failure so callers can return "no such object" instead of erroring.
    """
    client = create_dcim_client("netbox", _OK)

    async def fake_read(_response):
        return {
            "data": None,
            "errors": [
                {
                    "message": "Location matching query does not exist.",
                    "locations": [{"line": 1, "column": 20}],
                    "path": ["location"],
                }
            ],
        }

    client._session = lambda: _FakeSession()
    client._read = fake_read
    try:
        with pytest.raises(DCIMNotFoundError, match="does not exist"):
            await client.graphql_query("query { location(id: 1) { id } }")
    finally:
        await client.close()


async def test_graphql_mixed_errors_still_map_to_invalid_data():
    """Only an all-missing-object error set is a not-found; anything else is a
    genuine query failure and must not be softened into "no such object"."""
    client = create_dcim_client("netbox", _OK)

    async def fake_read(_response):
        return {
            "errors": [
                {"message": "Device matching query does not exist."},
                {"message": "Cannot query field 'nope' on type 'DeviceType'."},
            ]
        }

    client._session = lambda: _FakeSession()
    client._read = fake_read
    try:
        with pytest.raises(DCIMInvalidDataError):
            await client.graphql_query("query { device(id: 1) { nope } }")
    finally:
        await client.close()


class _RaisingCM:
    """Async context manager whose entry raises -- models a transport failure."""

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


# A connection error and a timeout -- the two native transport failures. Timeout is
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
    not a transport error -- it must surface as DCIMInvalidDataError, not leak a
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


# --- rest_list pagination -------------------------------------------------------
#
# rest_list is the read path for nearly every operation, but every behavioural
# suite stubs it wholesale, so its own offset/limit walk was never exercised.


def _paginating_client(pages):
    """Return a client whose ``rest_request`` serves ``pages`` in order, recording params."""
    client = create_dcim_client("netbox", _OK)
    calls: list[list[tuple[str, str]]] = []
    remaining = list(pages)

    async def fake_request(method, path, *, params=None, json=None):
        calls.append(list(params or []))
        return remaining.pop(0) if remaining else {"results": [], "next": None}

    client.rest_request = fake_request
    return client, calls


async def test_rest_list_walks_every_page_and_advances_the_offset():
    client, calls = _paginating_client(
        [
            {"count": 3, "next": "?offset=2", "results": [{"id": 1}, {"id": 2}]},
            {"count": 3, "next": None, "results": [{"id": 3}]},
        ]
    )
    rows = await client.rest_list("dcim/devices/", [("site", "dc1")], page_size=2)
    assert [r["id"] for r in rows] == [1, 2, 3]
    # The caller's filter is preserved on every page, and offset advances by the
    # number of rows actually returned.
    assert ("site", "dc1") in calls[0] and ("site", "dc1") in calls[1]
    assert ("limit", "2") in calls[0]
    assert ("offset", "0") in calls[0]
    assert ("offset", "2") in calls[1]


async def test_rest_list_stops_when_count_is_reached():
    """An endpoint that reports ``next`` past the end of its own ``count`` must not
    be followed -- otherwise a paginator that ignores ``offset`` loops forever
    accumulating duplicate rows."""
    client, calls = _paginating_client(
        [{"count": 2, "next": "?offset=0", "results": [{"id": 1}, {"id": 2}]}] * 5
    )
    rows = await client.rest_list("dcim/devices/", page_size=2)
    assert [r["id"] for r in rows] == [1, 2]
    assert len(calls) == 1


async def test_rest_list_tolerates_a_non_dict_page():
    client, _ = _paginating_client([None])
    assert await client.rest_list("dcim/devices/") == []


# --- provider-owned connection settings ------------------------------------------


def test_public_url_must_be_an_http_url():
    """public_url is surfaced to operators in device links, so it is held to the
    same bar as ``server`` rather than passed through unvalidated."""
    with pytest.raises(DCIMProviderConfigurationError):
        netbox_connection_settings({**_OK, "public_url": "netbox.example"})
    resolved = netbox_connection_settings({**_OK, "public_url": "https://nb.example"})
    assert resolved["public_url"] == "https://nb.example"


def test_timeout_is_configurable_and_validated():
    assert netbox_connection_settings(_OK)["timeout"] == 30.0  # default
    assert netbox_connection_settings({**_OK, "timeout": "5"})["timeout"] == 5.0
    for bad in ("nope", "0", "-1"):
        with pytest.raises(DCIMProviderConfigurationError):
            netbox_connection_settings({**_OK, "timeout": bad})


def test_unusable_ca_bundle_is_a_configuration_error():
    """A bad CA path must surface as a provider configuration error, not leak an
    OSError out of client construction."""
    with pytest.raises(DCIMProviderConfigurationError):
        create_dcim_client("netbox", {**_OK, "verify": "/nonexistent/ca.pem"})


def test_extra_headers_cannot_drop_authorization():
    """Supplied headers are merged into the generated ones; a caller passing
    headers must not silently unauthenticate the client."""
    client = BaseNetBoxClient(
        netbox_url="https://netbox.example", token="t", headers={"X-Trace": "abc"}
    )
    headers = client._request_headers()
    assert headers["Authorization"] == "Token t"
    assert headers["X-Trace"] == "abc"
