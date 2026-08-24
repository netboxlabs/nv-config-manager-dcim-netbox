# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Overlay scope resolution.

An Overlay is anchored through NetBox's ``CachedScopeMixin`` ``scope`` GFK, which
points at either a Site or a Location. The caller says which: SpectrumX anchors at
a Site, InfiniBand at a Location. ``_resolve_scope`` looks the value up in that one
model and returns the row, so callers can take the pk for ``scope_id`` and the slug
for ``cf_nvcm_site`` from a single lookup.
"""

from __future__ import annotations

import pytest
from nv_config_manager_dcim import DCIMInvalidDataError, create_dcim_client

from nv_config_manager_dcim_netbox.fabric_common import LOCATION_SCOPE_TYPE, SITE_SCOPE_TYPE

# Area marker for `make <area>` / `pytest -m <area>`.
pytestmark = pytest.mark.spectrumx

_SETTINGS = {"server": "https://netbox.example", "token": "t"}


def _client(sites=None, locations=None, overlays=None):
    """A client whose rest_list answers sites/locations/overlays by path.

    Sites and Locations are matched on the supplied ``id``, ``slug`` or ``name``
    param, mirroring ``_resolve_scope``'s lookup order. Compared as strings,
    because query params reach NetBox as strings and it matches ``?id=7`` against
    integer pk 7."""
    sites = sites or []
    locations = locations or []
    overlays = overlays or []
    client = create_dcim_client("netbox", _SETTINGS)
    calls = []

    def matches(row, p):
        key = next(iter(p), "")
        return key in row and str(row[key]) in {str(v) for v in p.values()}

    async def fake_rest_list(path, params=None):
        p = dict(params or [])
        calls.append((path, p))
        if path == "dcim/sites/":
            return [s for s in sites if matches(s, p)]
        if path == "dcim/locations/":
            return [loc for loc in locations if matches(loc, p)]
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


# -- resolution ----------------------------------------------------------------


async def test_resolve_scope_accepts_the_id_the_dropdown_handed_out():
    # list_locations returns NetBox pks and is_valid_location_id accepts only
    # those, so a workflow scoped from that dropdown sends the pk back.
    client = _client(sites=[{"id": 1, "name": "TRIAL01 - demo", "slug": "trial01-demo"}])
    try:
        row = await client._resolve_scope("1", scope_type=SITE_SCOPE_TYPE)
    finally:
        await client.close()
    assert row is not None and row["id"] == 1 and row["slug"] == "trial01-demo"


async def test_resolve_scope_looks_only_in_the_model_the_caller_named():
    """Site 1 and Location 1 are different objects.

    Searching both and preferring one is a guess; every caller knows which kind of
    scope it anchors at, so an id resolves to exactly one row.
    """
    client = _client(
        sites=[{"id": 1, "name": "SPO01", "slug": "spo01"}],
        locations=[{"id": 1, "name": "Row 1", "slug": "row-1"}],
    )
    try:
        site = await client._resolve_scope("1", scope_type=SITE_SCOPE_TYPE)
        location = await client._resolve_scope("1", scope_type=LOCATION_SCOPE_TYPE)
    finally:
        await client.close()
    assert site["slug"] == "spo01"
    assert location["slug"] == "row-1"


async def test_resolve_scope_takes_a_slug_or_a_name():
    # cf_nvcm_site stores the slug, and the InfiniBand anchor may be named.
    client = _client(sites=[{"id": 7, "name": "SPO01", "slug": "spo01"}])
    try:
        assert (await client._resolve_scope("spo01", scope_type=SITE_SCOPE_TYPE))["id"] == 7
        assert (await client._resolve_scope("SPO01", scope_type=SITE_SCOPE_TYPE))["id"] == 7
        assert await client._resolve_scope("nope", scope_type=SITE_SCOPE_TYPE) is None
        assert await client._resolve_scope(None, scope_type=SITE_SCOPE_TYPE) is None
    finally:
        await client.close()


async def test_resolve_scope_does_not_ask_netbox_to_filter_a_non_pk_by_id():
    # NetBox rejects a non-numeric `id` outright rather than not matching it.
    client = _client(sites=[{"id": 7, "name": "SPO01", "slug": "spo01"}])
    try:
        await client._resolve_scope("spo01", scope_type=SITE_SCOPE_TYPE)
    finally:
        await client.close()
    assert not any("id" in call[1] for call in client._calls if isinstance(call[1], dict))


async def test_resolve_scope_rejects_a_scope_type_that_is_not_a_scope():
    client = _client()
    try:
        with pytest.raises(DCIMInvalidDataError):
            await client._resolve_scope("1", scope_type="ipam.vrf")
    finally:
        await client.close()


async def test_resolve_any_scope_is_only_for_callers_that_cannot_know():
    # list_overlays spans both kinds of overlay. Sites first, matching
    # reference.get_location_metadata's order.
    client = _client(
        sites=[{"id": 7, "name": "SPO01", "slug": "spo01"}],
        locations=[{"id": 11, "name": "spo01", "slug": "spo01"}],
    )
    try:
        scope_type, row = await client._resolve_any_scope("spo01")
        assert (scope_type, row["id"]) == (SITE_SCOPE_TYPE, 7)

        scope_type, row = await client._resolve_any_scope("no-such-thing")
        assert (scope_type, row) == (None, None)
    finally:
        await client.close()


# -- cf_nvcm_site --------------------------------------------------------------


async def test_site_field_value_normalizes_a_pk_to_the_slug():
    """``cf_nvcm_site`` holds the Site slug -- what the plugin's loader seeds.

    NVCM sends the pk from list_locations, which matches no namespace zone.
    """
    client = _client(sites=[{"id": 1, "name": "SPO01", "slug": "spo01"}])
    try:
        assert await client._site_field_value("1") == "spo01"
        assert await client._site_field_value("spo01") == "spo01"
    finally:
        await client.close()


async def test_site_field_value_passes_an_unknown_site_through():
    # A filter on an unknown site should return no rows, not raise.
    client = _client(sites=[])
    try:
        assert await client._site_field_value("ghost") == "ghost"
    finally:
        await client.close()


# -- the overlay lookup that depends on it -------------------------------------


async def test_find_overlay_filters_on_scope_type_as_well_as_scope_id():
    # Site and Location pks are independent sequences, so scope_id alone also
    # matches the other model's overlays.
    client = _client(
        sites=[{"id": 7, "name": "SPO01", "slug": "spo01"}],
        overlays=[{"id": 3, "name": "ov"}],
    )
    try:
        result = await client._find_overlay("ov", "spo01", scope_type=SITE_SCOPE_TYPE)
    finally:
        await client.close()
    assert result == {"id": 3, "name": "ov"}
    params = next(c[1] for c in client._calls if c[0].endswith("overlays/"))
    assert params.get("scope_id") == "7"
    assert params.get("scope_type") == SITE_SCOPE_TYPE


async def test_find_overlay_resolves_a_location_scope():
    client = _client(
        locations=[{"id": 11, "name": "pod-1", "slug": "pod-1"}],
        overlays=[{"id": 4, "name": "ov"}],
    )
    try:
        result = await client._find_overlay("ov", "pod-1", scope_type=LOCATION_SCOPE_TYPE)
    finally:
        await client.close()
    assert result == {"id": 4, "name": "ov"}
    params = next(c[1] for c in client._calls if c[0].endswith("overlays/"))
    assert params.get("scope_type") == LOCATION_SCOPE_TYPE


async def test_find_overlay_unknown_scope_returns_none():
    client = _client(sites=[], locations=[], overlays=[{"id": 3, "name": "ov"}])
    try:
        result = await client._find_overlay("ov", "no-such-scope", scope_type=SITE_SCOPE_TYPE)
    finally:
        await client.close()
    assert result is None
    # It must NOT query overlays with an unresolved scope.
    assert not any(c[0].endswith("overlays/") for c in client._calls)


async def test_provision_raises_when_the_site_is_unknown():
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
