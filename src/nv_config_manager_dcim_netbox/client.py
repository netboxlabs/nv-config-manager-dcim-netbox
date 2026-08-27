# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""NetBox HTTP transport owned by the NetBox DCIM provider.

Reads use the GraphQL endpoint (``/graphql/``); writes use the REST API
(``/api/``). The transport performs no DCIM-contract logic -- operation mixins
build on it. It is kept deliberately small; provider operations own their own
queries, payloads, and SDK-model construction.
"""

from __future__ import annotations

import asyncio
import logging
import re
import ssl
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

_CONNECTION_KEYS = ("server", "token", "public_url", "verify", "timeout")
# NetBox's wording when a singular GraphQL query resolves no object, e.g.
# "Device matching query does not exist." (Django's ObjectDoesNotExist surfaced
# by strawberry-django).
_MISSING_OBJECT_RE = re.compile(r"matching query does not exist", re.IGNORECASE)
_DEFAULT_TIMEOUT = 30.0
# Upper bound on pages walked by ``rest_list``. Reached only when an endpoint
# reports a further page without honouring ``offset`` (see ``rest_list``).
_MAX_LIST_PAGES = 1000


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


def _require_http_url(value: str, setting: str) -> str:
    """Return ``value`` when it is an http(s) URL, else raise a config error."""
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise DCIMProviderConfigurationError(
            f'DCIM provider "netbox" {setting} must be an http(s):// URL, got {value!r}'
        )
    return value


def _parse_timeout(value: object) -> float:
    """Normalize the request-timeout setting (seconds), falling back to the default."""
    if value is None or str(value).strip() == "":
        return _DEFAULT_TIMEOUT
    try:
        timeout = float(str(value).strip())
    except ValueError as exc:
        raise DCIMProviderConfigurationError(
            f'DCIM provider "netbox" timeout must be a number of seconds, got {value!r}'
        ) from exc
    if timeout <= 0:
        raise DCIMProviderConfigurationError(
            f'DCIM provider "netbox" timeout must be greater than zero, got {value!r}'
        )
    return timeout


def netbox_connection_settings(settings: ProviderSettings) -> dict[str, str | bool | float]:
    """Validate and normalize explicit NetBox provider settings."""
    values = {key: settings.get(key) for key in _CONNECTION_KEYS}
    missing = [key for key in ("server", "token") if not str(values[key] or "").strip()]
    if missing:
        raise DCIMProviderConfigurationError(
            'DCIM provider "netbox" requires ' + ", ".join(missing)
        )
    # Return the stripped values, not the raw ones: validating a stripped copy and
    # storing the original let whitespace through, and aiohttp then rejects every
    # assembled URL as invalid.
    server = str(values["server"]).strip()
    _require_http_url(server, "server")
    public_url = str(values["public_url"]).strip() if values["public_url"] else ""
    if public_url:
        # Surfaced to operators in device UI links, so hold it to the same bar.
        _require_http_url(public_url, "public_url")
    return {
        "server": server,
        "token": str(values["token"]).strip(),
        "public_url": public_url,
        "verify": _parse_verify(values["verify"]),
        "timeout": _parse_timeout(values["timeout"]),
    }


def _build_ssl(verify: bool | str) -> bool | ssl.SSLContext:
    """Translate a verify setting into an aiohttp ``ssl`` argument.

    A string is a CA-bundle path. An unusable bundle is reported as a provider
    configuration error rather than leaking ``OSError``/``SSLError`` out of client
    construction, where callers expect the SDK taxonomy.
    """
    if verify is True:
        return True
    if verify is False:
        logger.warning(
            "NetBox TLS certificate verification is DISABLED (verify=false); "
            "connections to NetBox are not authenticated"
        )
        return False
    try:
        return ssl.create_default_context(cafile=str(verify))
    except (OSError, ssl.SSLError) as exc:
        raise DCIMProviderConfigurationError(
            f'DCIM provider "netbox" could not load the CA bundle at {verify!r}: {exc}'
        ) from exc


def _context_scoped_to_site(context: dict[str, Any], site: str) -> bool:
    """Return whether a config-context's *site* scoping admits ``site``.

    This evaluates the site dimension only -- a context with no ``locations`` /
    ``sites`` assignment is admitted, because NetBox treats an empty assignment
    set for a dimension as "any". The other dimensions NetBox ANDs in (region,
    site-group, role, platform, tenant, tenant-group, cluster, tags) are
    deliberately not evaluated here: this filter exists to keep a *site*-scoped
    profile from leaking across sites, and cannot rule a context in or out on
    dimensions the caller has not supplied. Callers therefore get a superset --
    every context that could apply to the site.
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


# --- provider-owned identifiers -------------------------------------------
#
# NetBox object PKs are integers, and NVCM stores them verbatim: the Config Store
# keeps device identifiers as TEXT and delegates the shape check to this provider
# through ``DCIMClient.is_valid_device_id`` / ``is_valid_location_id``. So the
# provider hands NVCM the native NetBox id (as a string) and receives it back
# unchanged -- no encoding at the boundary.


def is_netbox_pk(value: object) -> bool:
    """Return whether *value* is a canonical NetBox integer primary key.

    Canonical means it round-trips through its own text form, the integer analog
    of the reference provider's ``str(UUID(value)) == value``: ``"7"`` is accepted
    while ``"007"``, ``" 7"``, ``"7.0"`` and ``"abc"`` are not. NetBox PKs are
    positive, so ``0`` and negatives are rejected too.
    """
    text = str(value)
    try:
        number = int(text)
    except (TypeError, ValueError):
        return False
    return number > 0 and str(number) == text


class BaseNetBoxClient:
    """Async NetBox HTTP transport shared by the provider's operation mixins."""

    def __init__(
        self,
        netbox_url: str,
        token: str = "",
        verify: bool | str = True,
        public_url: str | None = None,
        headers: dict[str, str] | Callable[[], dict[str, str]] | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._base_url = netbox_url.rstrip("/")
        self._token = token
        self._ssl = _build_ssl(verify)
        self._public_url = (public_url or netbox_url).rstrip("/")
        # Extra headers merged into (never replacing) the generated ones, so a
        # caller cannot accidentally drop Authorization. Accepts a callable for
        # values that must be recomputed per request.
        self._extra_headers = headers
        self._timeout = timeout
        self._session_obj: aiohttp.ClientSession | None = None
        # Reference data read at most once per client. See
        # NetBoxInventoryMixin._known_platform_slugs for why platforms in
        # particular have to be checked before they are used as a filter.
        self._platform_slugs: set[str] | None = None

    @classmethod
    def from_settings(cls, settings: ProviderSettings) -> Self:
        """Create the client from an explicit, already-assembled settings mapping."""
        conn = netbox_connection_settings(settings)
        return cls(
            netbox_url=str(conn["server"]),
            token=str(conn["token"]),
            verify=conn["verify"],
            public_url=str(conn["public_url"]) or None,
            timeout=float(conn["timeout"]),
        )

    # -- lifecycle -----------------------------------------------------------

    def _session(self) -> aiohttp.ClientSession:
        if self._session_obj is None or self._session_obj.closed:
            self._session_obj = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout)
            )
        return self._session_obj

    async def close(self) -> None:
        """Close the underlying HTTP session."""
        if self._session_obj is not None and not self._session_obj.closed:
            await self._session_obj.close()
        self._session_obj = None

    async def __aenter__(self) -> Self:
        """Enter the client's async context; the session opens lazily per request."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        """Close the HTTP session on context exit, including on error."""
        await self.close()

    # -- provider-owned identifier validation --------------------------------

    def is_valid_device_id(self, value: str) -> bool:
        """Return whether *value* is a NetBox device PK."""
        return is_netbox_pk(value)

    def is_valid_location_id(self, value: str) -> bool:
        """Return whether *value* is a NetBox location PK."""
        return is_netbox_pk(value)

    def get_device_ui_url(self, device_id: str) -> str:
        """Build the user-facing NetBox device URL."""
        return f"{self._public_url}/dcim/devices/{device_id}/"

    # -- transport helpers (used by operation mixins as they are implemented) -

    def _url(self, path: str) -> str:
        return f"{self._base_url}/{path.lstrip('/')}"

    def _request_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._token:
            # v2 tokens ("nbt_<key>.<secret>") use the Bearer scheme; v1 use Token.
            scheme = "Bearer" if self._token.startswith("nbt_") else "Token"
            headers["Authorization"] = f"{scheme} {self._token}"
        extra = self._extra_headers() if callable(self._extra_headers) else self._extra_headers
        if extra:
            headers.update(extra)
        return headers

    def _raise_status(self, status: int, body: str) -> None:
        if status == 400:
            raise DCIMInvalidDataError(f"NetBox rejected the request ({status}): {body[:200]}")
        if status == 401:
            raise DCIMAuthenticationError(f"NetBox authentication failed ({status})")
        if status == 403:
            raise DCIMAuthorizationError(f"NetBox authorization failed ({status})")
        if status == 404:
            # Include the body: render's ``_safe_list`` treats 404 as "optional
            # plugin absent", so an operator diagnosing empty render data needs to
            # be able to tell that apart from a genuinely missing record.
            raise DCIMNotFoundError(f"NetBox record not found: {body[:200]}")
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
        except ValueError as exc:  # JSONDecodeError -- NetBox returned a non-JSON body
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
        if errors := payload.get("errors"):
            # Singular queries report a missing object as a GraphQL error, not a
            # null field (list queries return empty). Separate that from a real
            # failure so callers can tell "no such object" apart.
            if all(_MISSING_OBJECT_RE.search(str(e.get("message", ""))) for e in errors):
                raise DCIMNotFoundError(f"NetBox: {errors[0].get('message')}")
            raise DCIMInvalidDataError(f"NetBox GraphQL errors: {errors}")
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
        """Return all results from a paginated NetBox REST list endpoint.

        Walks ``offset``/``limit`` until the endpoint reports no further page.
        Stops early -- with a warning -- if a page does not advance the offset or if
        more results arrive than the endpoint's own ``count``, so an endpoint that
        reports ``next`` without honouring ``offset`` cannot spin forever
        accumulating duplicate rows.
        """
        base = list(params or [])
        first = await self.rest_request(
            "GET", path, params=base + [("limit", str(page_size)), ("offset", "0")]
        )
        if not isinstance(first, dict):
            return []
        out: list[dict[str, Any]] = list(first.get("results") or [])
        count = first.get("count")
        if not first.get("next") or not out:
            return out

        # The first page reports the total, so the rest are fetched together
        # instead of walked. Rows added or removed mid-read can shift the offset
        # window either way -- the serial walk had the same exposure.
        if isinstance(count, int) and 0 < count <= _MAX_LIST_PAGES * page_size:
            offsets = range(page_size, count, page_size)
            pages = await asyncio.gather(
                *(
                    self.rest_request(
                        "GET", path, params=base + [("limit", str(page_size)), ("offset", str(o))]
                    )
                    for o in offsets
                )
            )
            for page in pages:
                if isinstance(page, dict):
                    out.extend(page.get("results") or [])
            return out

        # No usable count: walk it.
        offset = len(out)
        for _ in range(_MAX_LIST_PAGES - 1):
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
        else:
            logger.warning(
                "Stopped paginating %s after %d pages (%d rows); the endpoint kept "
                "reporting a next page -- it may not honour the offset parameter",
                path,
                _MAX_LIST_PAGES,
                len(out),
            )
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
