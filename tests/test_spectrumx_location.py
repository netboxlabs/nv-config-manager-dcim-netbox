# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Spectrum-X overlay scope resolution.

The Overlay is anchored via NetBox's ``CachedScopeMixin`` ``scope`` GFK, which
may point to a Site or a Location. NVCM passes a single site/location value;
``_resolve_scope`` resolves it (Site first, then Location; by slug or name) to a
``(scope_type, scope_id)`` pair the plugin's overlay POST + ``scope_id`` filter
need. These pin that resolution + the overlay lookup that depends on it.
"""

from __future__ import annotations

import pytest
from nv_config_manager_dcim import DCIMInvalidDataError, create_dcim_client

# Area marker for `make <area>` / `pytest -m <area>`.
pytestmark = pytest.mark.spectrumx

_SETTINGS = {"server": "https://netbox.example", "token": "t"}


def _client(sites=None, locations=None, overlays=None):
    """A client whose rest_list answers sites/locations/overlays by path.

    Sites and Locations are matched on the supplied ``slug`` or ``name`` param
    (mirroring ``_resolve_scope``'s slug-then-name lookup)."""
    sites = sites or []
    locations = locations or []
    overlays = overlays or []
    client = create_dcim_client("netbox", _SETTINGS)
    calls = []

    async def fake_rest_list(path, params=None):
        p = dict(params or [])
        calls.append((path, p))
        if path == "dcim/sites/":
            return [s for s in sites if s.get(next(iter(p), "")) in p.values()]
        if path == "dcim/locations/":
            return [loc for loc in locations if loc.get(next(iter(p), "")) in p.values()]
        if path.endswith("overlays/"):
            return overlays
        return []

    async def fake_rest_request(method, path, *, params=None, json=None):
        calls.append((method, path, json))
        return {"id": 999}

    client.rest_list = fake_rest_list
    client.rest_request = fake_rest_request
    client._calls = calls
    return client


async def test_find_overlay_resolves_site_scope_to_pk():
    # A Site named/slugged 'spo01' -> overlays filtered by that Site's scope_id.
    client = _client(
        sites=[{"id": 7, "name": "SPO01", "slug": "spo01"}],
        overlays=[{"id": 3, "name": "ov"}],
    )
    try:
        result = await client._find_overlay("ov", "spo01")
    finally:
        await client.close()
    assert result == {"id": 3, "name": "ov"}
    overlay_call = next(c for c in client._calls if c[0].endswith("overlays/"))
    assert overlay_call[1].get("scope_id") == "7"


async def test_find_overlay_resolves_location_scope_when_no_site():
    # No Site matches; a Location does -> overlays filtered by the Location scope.
    client = _client(
        sites=[],
        locations=[{"id": 11, "name": "pod-1", "slug": "pod-1"}],
        overlays=[{"id": 4, "name": "ov"}],
    )
    try:
        result = await client._find_overlay("ov", "pod-1")
    finally:
        await client.close()
    assert result == {"id": 4, "name": "ov"}
    overlay_call = next(c for c in client._calls if c[0].endswith("overlays/"))
    assert overlay_call[1].get("scope_id") == "11"


async def test_find_overlay_unknown_scope_returns_none():
    client = _client(sites=[], locations=[], overlays=[{"id": 3, "name": "ov"}])
    try:
        result = await client._find_overlay("ov", "no-such-scope")
    finally:
        await client.close()
    assert result is None
    # It must NOT query overlays with an unresolved scope.
    assert not any(c[0].endswith("overlays/") for c in client._calls)


async def test_resolve_scope_prefers_site_over_location():
    client = _client(
        sites=[{"id": 7, "name": "SPO01", "slug": "spo01"}],
        locations=[{"id": 11, "name": "spo01", "slug": "spo01"}],
    )
    try:
        assert await client._resolve_scope("spo01") == ("dcim.site", 7)
        assert await client._resolve_scope(None) == (None, None)
    finally:
        await client.close()


async def test_provision_raises_when_scope_unresolved():
    # Overlay does not exist and the site/location can't be resolved -> clear error.
    client = _client(sites=[], locations=[], overlays=[])
    try:
        with pytest.raises(DCIMInvalidDataError):
            await client.provision_spectrum_x_vrf(
                namespaces=["ns"],
                route_distinguisher="*:1000",
                vnid=1000,
                overlay_name="spx",
                site="DM-Akron",
                tenant="Acme",
            )
    finally:
        await client.close()
