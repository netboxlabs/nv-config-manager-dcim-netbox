# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Scope resolution across NetBox's four location models.

The plugin's Overlay scope is a ``CachedScopeMixin`` GFK, so it accepts a Region,
SiteGroup, Site or Location. PKs are per-model, so an id alone cannot say which
was meant -- a typed ``DCIMLocationReference`` names it, and an untyped value is
tried against each level in turn.
"""

from __future__ import annotations

import pytest
from nv_config_manager_dcim import DCIMLocationReference

from nv_config_manager_dcim_netbox.fabric_common import (
    LOCATION_SCOPE_TYPE,
    REGION_SCOPE_TYPE,
    SITE_SCOPE_TYPE,
    SITEGROUP_SCOPE_TYPE,
    scope_type_for,
)
from tests._fake import fake_client as _client

pytestmark = pytest.mark.reference

# One row per model, all holding pk 1 -- the collision the type has to resolve.
_ROWS = {
    "dcim/regions/": [{"id": 1, "name": "NA-East", "slug": "na-east"}],
    "dcim/site-groups/": [{"id": 1, "name": "Colo", "slug": "colo"}],
    "dcim/sites/": [{"id": 1, "name": "DC01", "slug": "dc01"}],
    "dcim/locations/": [{"id": 1, "name": "Module1", "slug": "module1"}],
}


@pytest.mark.parametrize(
    ("location_type", "expected"),
    [
        ("Site", SITE_SCOPE_TYPE),
        ("Region", REGION_SCOPE_TYPE),
        ("SiteGroup", SITEGROUP_SCOPE_TYPE),
        ("Site Group", SITEGROUP_SCOPE_TYPE),
        ("Location", LOCATION_SCOPE_TYPE),
        # The topology loader maps a source Provider level onto a NetBox Region.
        ("Provider", REGION_SCOPE_TYPE),
        # Every other source type is a Location told apart by its custom field.
        ("Module", LOCATION_SCOPE_TYPE),
        ("Rack", LOCATION_SCOPE_TYPE),
        (None, None),
        ("", None),
    ],
)
def test_scope_type_for(location_type, expected):
    assert scope_type_for(location_type) == expected


@pytest.mark.parametrize(
    ("location_type", "expected_name"),
    [
        ("Region", "NA-East"),
        ("SiteGroup", "Colo"),
        ("Site", "DC01"),
        ("Module", "Module1"),
    ],
)
async def test_a_typed_reference_resolves_in_its_own_model(location_type, expected_name):
    """All four hold pk 1; only the type says which one is meant."""
    client = _client(rest_list=_ROWS)

    scope_type, row = await client._resolve_any_scope(
        DCIMLocationReference(id="1", location_type=location_type)
    )

    assert scope_type == scope_type_for(location_type)
    assert row is not None and row["name"] == expected_name


async def test_an_untyped_id_falls_back_to_the_site():
    """No type: Sites first, because /parameter/site is what hands ids out."""
    client = _client(rest_list=_ROWS)

    scope_type, row = await client._resolve_any_scope("1")

    assert (scope_type, row["name"]) == (SITE_SCOPE_TYPE, "DC01")


async def test_an_untyped_id_still_finds_a_region_when_nothing_else_holds_it():
    client = _client(rest_list={"dcim/regions/": _ROWS["dcim/regions/"]})

    scope_type, row = await client._resolve_any_scope("1")

    assert (scope_type, row["name"]) == (REGION_SCOPE_TYPE, "NA-East")


async def test_a_typed_reference_that_matches_nothing_resolves_to_none():
    """A Region-typed id is not looked for among Sites."""
    client = _client(rest_list={"dcim/sites/": _ROWS["dcim/sites/"]})

    scope_type, row = await client._resolve_any_scope(
        DCIMLocationReference(id="1", location_type="Region")
    )

    assert (scope_type, row) == (None, None)


async def test_resolve_scope_honours_the_reference_over_the_callers_default():
    """SpectrumX passes SITE_SCOPE_TYPE; a Location-typed value overrides it."""
    client = _client(rest_list=_ROWS)

    row = await client._resolve_scope(
        DCIMLocationReference(id="1", location_type="Module"), scope_type=SITE_SCOPE_TYPE
    )

    assert row is not None and row["name"] == "Module1"


async def test_resolve_scope_accepts_a_bare_id():
    client = _client(rest_list=_ROWS)

    row = await client._resolve_scope("1", scope_type=LOCATION_SCOPE_TYPE)

    assert row is not None and row["name"] == "Module1"


async def test_a_reference_with_an_empty_id_resolves_to_none():
    """A reference is truthy whatever its id, so the unwrap has to precede the
    emptiness guard -- otherwise an empty id reaches slug=/name= lookups."""
    seen: list[tuple[str, list]] = []
    client = _client(rest_list=_ROWS)
    inner = client.rest_list

    async def spy(path, params=None, **kwargs):
        seen.append((path, list(params or [])))
        return await inner(path, params, **kwargs)

    client.rest_list = spy

    row = await client._resolve_scope(
        DCIMLocationReference(id="", location_type="Site"), scope_type=SITE_SCOPE_TYPE
    )

    assert row is None
    assert not seen, "an empty id must not be looked up"
