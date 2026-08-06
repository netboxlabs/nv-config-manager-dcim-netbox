# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""NetBox HTTP transport owned by the NetBox DCIM provider.

Reads use the GraphQL endpoint (``/graphql/``); writes use the REST API
(``/api/``). The transport performs no DCIM-contract logic — operation mixins
build on it. It is kept deliberately small; provider operations own their own
queries, payloads, and SDK-model construction.
"""

from __future__ import annotations

import functools
import inspect
import logging
import ssl
import uuid
from collections.abc import Callable
from typing import Any, Self
from urllib.parse import urlparse

import aiohttp
from nv_config_manager_dcim import (
    DCIMAuthenticationError,
    DCIMAuthorizationError,
    DCIMConflictError,
    DCIMConnectivityError,
    DCIMInvalidDataError,
    DCIMNotFoundError,
    DCIMProviderConfigurationError,
    ProviderSettings,
)

logger = logging.getLogger(__name__)

_CONNECTION_KEYS = ("server", "token", "public_url", "verify")
_DEFAULT_TIMEOUT = 30.0


def _parse_verify(value: object) -> bool | str:
    """Normalize a provider-owned TLS verification setting."""
    if isinstance(value, bool):
        return value
    if value is None or str(value).strip() == "":
        return True
    normalized = str(value).strip()
    if normalized.lower() in {"true", "yes", "1"}:
        return True
    if normalized.lower() in {"false", "no", "0"}:
        return False
    return normalized


def netbox_connection_settings(settings: ProviderSettings) -> dict[str, str | bool]:
    """Validate and normalize explicit NetBox provider settings."""
    values = {key: settings.get(key) for key in _CONNECTION_KEYS}
    missing = [key for key in ("server", "token") if not str(values[key] or "").strip()]
    if missing:
        raise DCIMProviderConfigurationError(
            'DCIM provider "netbox" requires ' + ", ".join(missing)
        )
    server = str(values["server"]).strip()
    parsed = urlparse(server)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise DCIMProviderConfigurationError(
            f'DCIM provider "netbox" server must be an http(s):// URL, got {server!r}'
        )
    return {
        "server": str(values["server"]),
        "token": str(values["token"]),
        "public_url": str(values["public_url"]) if values["public_url"] else "",
        "verify": _parse_verify(values["verify"]),
    }


def _build_ssl(verify: bool | str) -> bool | ssl.SSLContext:
    """Translate a verify setting into an aiohttp ``ssl`` argument."""
    if verify is True:
        return True
    if verify is False:
        return False
    return ssl.create_default_context(cafile=str(verify))


def _context_scoped_to_site(context: dict[str, Any], site: str) -> bool:
    """Return whether a config-context applies to ``site`` (by name or slug).

    A context with no location/site assignment applies everywhere (NetBox
    treats an empty assignment set as global); an assigned context must name
    the site in its ``locations`` or ``sites``.
    """
    scoped = False
    for key in ("locations", "sites"):
        for entry in context.get(key) or []:
            if not isinstance(entry, dict):
                continue
            scoped = True
            if entry.get("name") == site or entry.get("slug") == site:
                return True
    return not scoped


# --- device-id UUID codec -------------------------------------------------
#
# NetBox device PKs are integers, but some downstream NVCM services (notably the
# config-store, whose API/DB/locks assume UUID device ids) require the device
# identifier to be a UUID. Nothing in the DCIM provider *contract* requires a
# UUID, so we bridge the gap here: the provider presents each NetBox device id to
# NVCM as a deterministic, reversible UUID (`uuid.UUID(int=<pk>)`) and decodes it
# back to the integer PK before touching NetBox. Decoding tolerates raw NetBox
# ids (pass-through), so it is safe to apply defensively and idempotently.

_DEVICE_ID_PARAMS = frozenset({"device_id"})
_DEVICE_ID_LIST_PARAMS = frozenset({"device_ids", "switch_device_ids"})


def encode_device_id(pk: object) -> str:
    """Present a NetBox integer device PK as a UUID string (for UUID-keyed NVCM services)."""
    return str(uuid.UUID(int=int(pk)))


def decode_device_id(device_id: object) -> str:
    """Inverse of :func:`encode_device_id`; non-UUID ids pass through unchanged."""
    try:
        return str(uuid.UUID(str(device_id)).int)
    except (ValueError, AttributeError, TypeError):
        return str(device_id)


def _decode_device_args(sig: inspect.Signature, args: tuple, kwargs: dict) -> tuple[tuple, dict]:
    """Return (args, kwargs) with device-id params decoded to NetBox PKs."""
    bound = sig.bind_partial(*args, **kwargs)
    for name, value in list(bound.arguments.items()):
        if name in _DEVICE_ID_PARAMS and value is not None:
            bound.arguments[name] = decode_device_id(value)
        elif name in _DEVICE_ID_LIST_PARAMS and value:
            bound.arguments[name] = [decode_device_id(v) for v in value]
    return bound.args, bound.kwargs


def with_device_id_decoding(cls: type) -> type:
    """Class decorator: decode UUID-shaped device ids on every method (across the
    MRO) whose signature has a ``device_id`` / ``device_ids`` / ``switch_device_ids``
    parameter, so NetBox queries always receive integer PKs regardless of the
    UUID form NVCM passes in."""
    id_params = _DEVICE_ID_PARAMS | _DEVICE_ID_LIST_PARAMS
    handled: set[str] = set()
    for klass in cls.__mro__:
        if klass is object:
            continue
        for name, method in list(vars(klass).items()):
            if name in handled or name.startswith("__") or not callable(method):
                continue
            try:
                sig = inspect.signature(method)
            except (TypeError, ValueError):
                continue
            if not id_params & set(sig.parameters):
                continue
            handled.add(name)
            if inspect.iscoroutinefunction(method):

                @functools.wraps(method)
                async def wrapper(*args, _m=method, _s=sig, **kwargs):
                    a, kw = _decode_device_args(_s, args, kwargs)
                    return await _m(*a, **kw)
            else:

                @functools.wraps(method)
                def wrapper(*args, _m=method, _s=sig, **kwargs):
                    a, kw = _decode_device_args(_s, args, kwargs)
                    return _m(*a, **kw)

            setattr(cls, name, wrapper)
    return cls


class BaseNetBoxClient:
    """Async NetBox HTTP transport shared by the provider's operation mixins."""

    def __init__(
        self,
        netbox_url: str,
        token: str = "",
        verify: bool | str = True,
        public_url: str | None = None,
        headers: dict[str, str] | Callable[[], dict[str, str]] | None = None,
    ) -> None:
        self._base_url = netbox_url.rstrip("/")
        self._token = token
        self._ssl = _build_ssl(verify)
        self._public_url = (public_url or netbox_url).rstrip("/")
        self._headers_factory = headers
        self._session_obj: aiohttp.ClientSession | None = None

    @classmethod
    def from_settings(cls, settings: ProviderSettings) -> Self:
        """Create the client from an explicit, already-assembled settings mapping."""
        conn = netbox_connection_settings(settings)
        return cls(
            netbox_url=str(conn["server"]),
            token=str(conn["token"]),
            verify=conn["verify"],
            public_url=str(conn["public_url"]) or None,
        )

    # -- lifecycle -----------------------------------------------------------

    def _session(self) -> aiohttp.ClientSession:
        if self._session_obj is None or self._session_obj.closed:
            self._session_obj = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=_DEFAULT_TIMEOUT)
            )
        return self._session_obj

    async def close(self) -> None:
        """Close the underlying HTTP session."""
        if self._session_obj is not None and not self._session_obj.closed:
            await self._session_obj.close()
        self._session_obj = None

    def get_device_ui_url(self, device_id: str) -> str:
        """Build the user-facing NetBox device URL."""
        return f"{self._public_url}/dcim/devices/{device_id}/"

    # -- transport helpers (used by operation mixins as they are implemented) -

    def _url(self, path: str) -> str:
        return f"{self._base_url}/{path.lstrip('/')}"

    def _request_headers(self) -> dict[str, str]:
        if callable(self._headers_factory):
            return dict(self._headers_factory())
        if self._headers_factory:
            return dict(self._headers_factory)
        headers = {"Accept": "application/json"}
        if self._token:
            # v2 tokens ("nbt_<key>.<secret>") use the Bearer scheme; v1 use Token.
            scheme = "Bearer" if self._token.startswith("nbt_") else "Token"
            headers["Authorization"] = f"{scheme} {self._token}"
        return headers

    def _raise_status(self, status: int, body: str) -> None:
        if status == 400:
            raise DCIMInvalidDataError(f"NetBox rejected the request ({status}): {body[:200]}")
        if status == 401:
            raise DCIMAuthenticationError(f"NetBox authentication failed ({status})")
        if status == 403:
            raise DCIMAuthorizationError(f"NetBox authorization failed ({status})")
        if status == 404:
            raise DCIMNotFoundError("NetBox record not found")
        if status == 409:
            raise DCIMConflictError(f"NetBox reported a conflict ({status}): {body[:200]}")
        raise DCIMConnectivityError(f"NetBox request failed ({status}): {body[:200]}")

    async def _read(self, response: aiohttp.ClientResponse) -> Any:
        text = await response.text()
        if response.status >= 400:
            self._raise_status(response.status, text)
        if response.status == 204 or not text.strip():
            return None
        try:
            return await response.json(content_type=None)
        except ValueError as exc:  # JSONDecodeError — NetBox returned a non-JSON body
            raise DCIMInvalidDataError(
                f"NetBox returned a non-JSON response: {text[:200]}"
            ) from exc

    async def graphql_query(
        self, query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Execute a GraphQL query and return its ``data`` payload."""
        try:
            async with self._session().post(
                self._url("/graphql/"),
                json={"query": query, "variables": variables or {}},
                headers=self._request_headers(),
                ssl=self._ssl,
            ) as response:
                payload = await self._read(response)
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise DCIMConnectivityError(f"NetBox GraphQL request failed: {exc}") from exc
        payload = payload or {}
        if payload.get("errors"):
            raise DCIMInvalidDataError(f"NetBox GraphQL errors: {payload['errors']}")
        return payload.get("data", {})

    async def rest_request(
        self,
        method: str,
        path: str,
        *,
        params: Any = None,
        json: Any = None,
    ) -> Any:
        """Execute a REST request against ``/api/<path>`` and return parsed JSON.

        ``params`` may be a dict or a list of ``(key, value)`` tuples (for
        repeated multi-value filters). Values must be strings.
        """
        try:
            async with self._session().request(
                method,
                self._url(f"/api/{path.lstrip('/')}"),
                params=params,
                json=json,
                headers=self._request_headers(),
                ssl=self._ssl,
            ) as response:
                return await self._read(response)
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise DCIMConnectivityError(f"NetBox REST request failed: {exc}") from exc

    async def rest_list(
        self, path: str, params: list[tuple[str, str]] | None = None, page_size: int = 250
    ) -> list[dict[str, Any]]:
        """Return all results from a paginated NetBox REST list endpoint."""
        base = list(params or [])
        offset = 0
        out: list[dict[str, Any]] = []
        while True:
            page = await self.rest_request(
                "GET",
                path,
                params=base + [("limit", str(page_size)), ("offset", str(offset))],
            )
            if not isinstance(page, dict):
                break
            results = page.get("results") or []
            out.extend(results)
            if not page.get("next") or not results:
                break
            offset += len(results)
        return out

    async def config_contexts_for_profile(
        self, profile_name: str, site: str | None = None
    ) -> list[dict[str, Any]]:
        """Return active config-contexts whose profile is named ``profile_name``.

        When ``site`` is given the results are scoped to that site: a context is
        kept when it is assigned to the site (via its ``locations`` / ``sites``)
        or is unscoped. An empty assignment set means "applies everywhere",
        matching NetBox's own config-context rendering.
        """
        contexts = await self.rest_list("extras/config-contexts/", [("is_active", "true")])
        out = []
        for context in contexts:
            profile = context.get("profile")
            name = profile.get("name") if isinstance(profile, dict) else None
            if name != profile_name:
                continue
            if site is not None and not _context_scoped_to_site(context, site):
                continue
            out.append(context)
        return out
