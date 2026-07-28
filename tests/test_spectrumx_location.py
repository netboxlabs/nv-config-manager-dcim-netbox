# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Spectrum-X location-name resolution (regression for the site/location bug).

NVCM passes a site/location *name*; NetBox's ``location_id`` filter and the
Overlay ``location`` FK need a numeric PK. A live NVCM SpX workflow surfaced a
400 (`location_id: "DM-Akron" is not a valid value`) because the name was sent
verbatim. These pin the name -> PK resolution.
"""

from __future__ import annotations

import pytest
from nv_config_manager_dcim import DCIMInvalidDataError, create_dcim_client

_SETTINGS = {"server": "https://netbox.example", "token": "t"}


def _client(locations, overlays):
    """A client whose rest_list answers locations + overlays by path."""
    client = create_dcim_client("netbox", _SETTINGS)
    calls = []

    async def fake_rest_list(path, params=None):
        calls.append((path, dict(params or [])))
        if path == "dcim/locations/":
            name = dict(params or []).get("name")
            return [loc for loc in locations if loc["name"] == name]
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


async def test_find_overlay_resolves_location_name_to_pk():
    client = _client(
        locations=[{"id": 7, "name": "nvcm-loc"}],
        overlays=[{"id": 3, "name": "ov"}],
    )
    try:
        result = await client._find_overlay("ov", "nvcm-loc")
    finally:
        await client.close()
    assert result == {"id": 3, "name": "ov"}
    # The overlays list was filtered by the resolved PK (7), not the name.
    overlay_call = next(c for c in client._calls if c[0].endswith("overlays/"))
    assert overlay_call[1].get("location_id") == "7"


async def test_find_overlay_unknown_location_returns_none():
    client = _client(locations=[], overlays=[{"id": 3, "name": "ov"}])
    try:
        result = await client._find_overlay("ov", "no-such-loc")
    finally:
        await client.close()
    assert result is None
    # It must NOT query overlays with an unresolved (name) location_id.
    assert not any(c[0].endswith("overlays/") for c in client._calls)


async def test_resolve_location_id_passthrough_for_pk():
    client = _client(locations=[], overlays=[])
    try:
        assert await client._resolve_location_id("42") == 42
        assert await client._resolve_location_id(None) is None
    finally:
        await client.close()


async def test_provision_raises_when_location_name_unresolved():
    # Overlay does not exist and the location name can't be resolved -> clear error
    # (was previously `int(site)` -> ValueError on a name like "DM-Akron").
    client = _client(locations=[], overlays=[])
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
