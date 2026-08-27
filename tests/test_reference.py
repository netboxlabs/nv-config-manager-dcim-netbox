# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Behavioral tests for the reference/selection list methods (stubbed transport)."""

from __future__ import annotations

import pytest
from nv_config_manager_dcim import DCIMInvalidDataError, DCIMNotFoundError

from tests._fake import fake_client as _client

# Area marker for `make <area>` / `pytest -m <area>`.
pytestmark = pytest.mark.reference


async def test_list_locations():
    client = _client(gql={"location_list": [{"id": 1, "name": "Row 1"}]})
    result = await client.list_locations()
    assert [(s.id, s.name) for s in result] == [("1", "Row 1")]


async def test_get_location_metadata():
    # NVCM resolves a workflow's location reference through this, then uses the
    # returned name as the canonical location, so the id space must match
    # list_locations. lvl1 is `location`, second in _LOCATION_RESOLUTION_ORDER.
    client = _client(gql={"lvl1": [{"id": 1, "name": "Row 1"}]})
    location = await client.get_location_metadata("1")
    assert location is not None and (location.id, location.name) == ("1", "Row 1")


async def test_get_location_metadata_resolves_every_level_in_one_query():
    """A list query filtered to a missing id returns [], so all levels fit in one
    document -- the singular form errors and forced one round-trip per level."""
    client = _client()
    queries = []

    async def _record(query, variables=None):
        queries.append(query)
        return {"lvl0": [], "lvl1": [], "lvl2": [{"id": 7, "name": "West"}], "lvl3": []}

    client.graphql_query = _record
    found = await client.get_location_metadata("7")

    assert found is not None and (found.id, found.name) == ("7", "West")
    assert len(queries) == 1
    for field in ("site_list", "location_list", "region_list", "site_group_list"):
        assert field in queries[0]


async def test_get_location_metadata_missing_returns_none():
    client = _client()

    async def _empty(query, variables=None):
        return {"lvl0": [], "lvl1": [], "lvl2": [], "lvl3": []}

    client.graphql_query = _empty
    assert await client.get_location_metadata("999999") is None


async def test_get_location_metadata_tolerates_a_not_found_error():
    # Kept for a NetBox that reports an unknown id as a GraphQL error rather than
    # an empty list: the contract wants None so NVCM reports "Unknown location".
    client = _client()

    async def _missing(query, variables=None):
        raise DCIMNotFoundError("NetBox: Location matching query does not exist.")

    client.graphql_query = _missing
    assert await client.get_location_metadata("999999") is None


async def test_get_location_metadata_propagates_real_failures():
    client = _client()

    async def _broken(query, variables=None):
        raise DCIMInvalidDataError("NetBox GraphQL errors: [...]")

    client.graphql_query = _broken
    with pytest.raises(DCIMInvalidDataError):
        await client.get_location_metadata("1")


async def test_list_tenants():
    client = _client(gql={"tenant_list": [{"id": 5, "name": "NGC"}]})
    result = await client.list_tenants()
    assert [(s.id, s.name) for s in result] == [("5", "NGC")]


async def test_list_roles():
    client = _client(gql={"device_role_list": [{"id": 2, "name": "Leaf"}]})
    result = await client.list_roles()
    assert [(s.id, s.name) for s in result] == [("2", "Leaf")]


async def test_list_overlays():
    client = _client(
        rest_list={"plugins/nv-config-manager/overlays/": [{"id": 10, "name": "blue"}]}
    )
    result = await client.list_overlays()
    assert [(s.id, s.name) for s in result] == [("10", "blue")]


async def test_list_overlays_empty():
    client = _client(rest_list={})
    assert await client.list_overlays(isolation_type="ib_pkey") == []


async def test_site_dropdown_reads_netbox_sites():
    """NVCM's site dropdown calls list_locations(("Site",)).

    NetBox has no per-location type; its hierarchy is the type, one model per
    level. The mock-topology loader maps a blueprint's Site level onto a NetBox
    Site and creates no Location objects, so querying `location_list` returned
    nothing and the dropdown came up empty.
    """
    client = _client(gql={"site_list": [{"id": 1, "name": "TRIAL01 - demo"}]})
    result = await client.list_locations(("Site",))
    assert [(s.id, s.name) for s in result] == [("1", "TRIAL01 - demo")]


async def test_location_types_select_the_matching_netbox_model():
    client = _client(gql={"region_list": [{"id": 2, "name": "DSX Air Trial Demo"}]})
    assert [s.name for s in await client.list_locations(("Region",))] == ["DSX Air Trial Demo"]


async def test_unfiltered_locations_cover_sites_and_locations():
    # Devices attach to a Site; NetBox Locations are an optional sub-grouping.
    # Both are legitimate scopes, so an unfiltered call returns both.
    client = _client(
        gql={
            "site_list": [{"id": 1, "name": "TRIAL01"}],
            "location_list": [{"id": 5, "name": "Row 1"}],
        }
    )
    assert {s.name for s in await client.list_locations()} == {"TRIAL01", "Row 1"}


async def test_locations_never_offer_two_entries_under_one_id():
    """NetBox PKs are per-model, so a Site and a Location can both be id 1.

    NVCM stores a location id as the bare integer (`is_valid_location_id` rejects
    anything else) and sends only that back, so two choices sharing an id are
    indistinguishable -- and `get_location_metadata` would resolve both to the
    Site, silently returning a different object than the one that was picked.
    Dedup used to key on (id, name), which let the pair through.
    """
    client = _client(
        gql={
            "site_list": [{"id": 1, "name": "demo-site"}],
            "location_list": [{"id": 1, "name": "rack-row-a"}, {"id": 2, "name": "rack-row-b"}],
        }
    )
    result = await client.list_locations()

    assert len({s.id for s in result}) == len(result), [(s.id, s.name) for s in result]
    # Sites win, matching the order get_location_metadata resolves in.
    assert ("1", "demo-site") in [(s.id, s.name) for s in result]
    assert ("2", "rack-row-b") in [(s.id, s.name) for s in result]


async def test_get_location_metadata_resolves_every_level():
    """A region id comes back from `list_locations(("Region",))`, so it has to
    resolve here too.

    All four levels are in _LOCATION_RESOLUTION_ORDER; omitting one resolves an
    explicitly-typed selection to None, or to whichever Site holds the same PK.
    """
    client = _client(gql={"lvl2": [{"id": 7, "name": "DSX Air Trial"}]})
    found = await client.get_location_metadata("7")
    assert found is not None and (found.id, found.name) == ("7", "DSX Air Trial")

    client = _client(gql={"lvl3": [{"id": 8, "name": "Trial Group"}]})
    found = await client.get_location_metadata("8")
    assert found is not None and found.name == "Trial Group"


async def test_a_location_type_spelled_two_ways_is_queried_once():
    # "sitegroup" and "site_group" are the same model; querying it twice would
    # duplicate every row in the dropdown.
    calls: list[str] = []
    client = _client(gql={"site_group_list": [{"id": 1, "name": "Trial Group"}]})
    inner = client.graphql_query

    async def _counting(query, variables=None):
        calls.append(query)
        return await inner(query, variables)

    client.graphql_query = _counting
    result = await client.list_locations(("SiteGroup", "site_group"))

    assert [s.name for s in result] == ["Trial Group"]
    assert len(calls) == 1, calls


async def test_an_unknown_location_type_is_skipped_not_fatal():
    client = _client(gql={"site_list": [{"id": 1, "name": "TRIAL01"}]})
    assert [s.name for s in await client.list_locations(("Site", "Rack"))] == ["TRIAL01"]


async def test_get_location_metadata_resolves_a_site_id():
    # Same id space as list_locations, so a site chosen in a form round-trips.
    client = _client(gql={"lvl0": [{"id": 1, "name": "TRIAL01 - demo"}]})
    found = await client.get_location_metadata("1")
    assert found is not None and found.name == "TRIAL01 - demo"
