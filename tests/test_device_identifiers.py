# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Provider-owned identifiers: NVCM stores NetBox PKs verbatim.

The Config Store keeps device identifiers as TEXT and delegates the shape check
to this provider, so ids cross the boundary unchanged in both directions and
``is_valid_device_id`` / ``is_valid_location_id`` decide what NVCM will accept.
"""

from __future__ import annotations

import asyncio

from nv_config_manager_dcim import RenderDataRequest

from nv_config_manager_dcim_netbox.client import is_netbox_pk
from tests._fake import fake_client as _client

_STATUS = "plugins/nv-config-manager/device-config-statuses/"


def test_is_netbox_pk_accepts_canonical_pks():
    assert all(is_netbox_pk(v) for v in ("1", "53", "999999", 53))


def test_is_netbox_pk_rejects_non_canonical_and_invalid():
    # Non-canonical text forms are rejected so one device cannot be addressed
    # under several spellings, mirroring the Nautobot provider's canonical-UUID
    # check. Zero/negative are not NetBox PKs; a UUID is what the old encoding
    # produced and must no longer be accepted.
    for value in (
        "007",
        " 7",
        "7.0",
        "abc",
        "",
        "0",
        "-3",
        None,
        "00000000-0000-0000-0000-000000000035",
    ):
        assert not is_netbox_pk(value), value


def test_client_validates_device_and_location_ids():
    client = _client()
    assert client.is_valid_device_id("53") and client.is_valid_location_id("53")
    assert not client.is_valid_device_id("not-a-pk")
    assert not client.is_valid_location_id("not-a-pk")


def test_render_enabled_device_ids_are_native_pks():
    client = _client(rest_list={_STATUS: [{"device": 5}, {"device": 7}]})
    assert set(asyncio.run(client.get_render_enabled_device_ids(None))) == {"5", "7"}


def test_render_data_queries_netbox_by_the_id_nvcm_passed():
    # No translation at the boundary: the id NVCM supplies is the id NetBox is
    # queried with.
    seen = {}

    class _StopQuery(Exception):
        pass

    client = _client()

    async def _capture(query, variables=None):
        seen["id"] = (variables or {}).get("id")
        raise _StopQuery

    client.graphql_query = _capture
    try:
        asyncio.run(client.get_render_data(RenderDataRequest(device_id="53")))
    except _StopQuery:
        pass
    assert seen["id"] == "53"


def test_client_is_an_async_context_manager():
    # NVCM drives the client with `async with client:` (workflow reference
    # resolution), so entry must yield the client and exit must close it.
    client = _client()
    closed = []
    client.close = lambda: _noop(closed)

    async def _use():
        async with client as entered:
            assert entered is client

    asyncio.run(_use())
    assert closed == ["closed"]


async def _noop(closed):
    closed.append("closed")
