# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Behavioral tests for the reference/selection list methods (stubbed transport)."""

from __future__ import annotations

from tests._fake import fake_client as _client


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
    assert await _client(gql={"location": None}).get_location_metadata("999") is None


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
