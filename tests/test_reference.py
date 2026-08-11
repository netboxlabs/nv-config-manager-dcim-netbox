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
    # list_locations.
    client = _client(gql={"location": {"id": 1, "name": "Row 1"}})
    location = await client.get_location_metadata("1")
    assert location is not None and (location.id, location.name) == ("1", "Row 1")


async def test_get_location_metadata_missing_returns_none():
    # NetBox signals the missing location as a GraphQL error, which the transport
    # classifies as DCIMNotFoundError; the contract wants None so NVCM reports
    # "Unknown location" (422) rather than failing the request.
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
