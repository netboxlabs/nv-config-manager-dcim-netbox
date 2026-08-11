# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Shared stubbed-transport client factory for the behavioral test suites.

One routable fake keeps the per-area tests DRY: pass canned GraphQL / REST
payloads keyed by path, optionally record writes, and patch any provider helper
(e.g. ``_find_overlay``, ``config_contexts_for_profile``) via ``patch=``.
"""

from __future__ import annotations

from nv_config_manager_dcim import create_dcim_client

SETTINGS = {"server": "https://netbox.example", "token": "t"}


def fake_client(
    *,
    gql=None,
    rest_list=None,
    rest_get=None,
    post=None,
    record=None,
    patch=None,
    queries=None,
):
    """Return a NetBox client with a routable, stubbed transport.

    * ``gql`` — dict returned by ``graphql_query`` (single canned payload).
    * ``rest_list`` — ``{path: [rows]}`` for ``rest_list``; rows are filtered by
      the ``assigned_object_id`` query param when present (so per-object lookups
      like IB assignment resolution behave faithfully).
    * ``rest_get`` — ``{path: obj}`` returned by ``rest_request('GET', path)``.
    * ``post`` — ``{path: obj}`` returned by non-GET ``rest_request`` (default
      ``{'id': 99}``) so ``["id"]`` reads on create succeed.
    * ``record`` — a list that receives ``(method, path, json)`` for every write.
    * ``patch`` — ``{attr: async_callable}`` to override provider helper methods.
    * ``queries`` — a list that receives ``(path, params)`` for every ``rest_list``
      call. Use it to assert the NetBox *filter names* a read depends on: those
      spellings (``cf_ib_guid__ie``, ``has_primary_ip``, ``name__isw``, …) are this
      provider's contract with NetBox, and a typo in one is invisible to a fake that
      only matches on path.
    """
    client = create_dcim_client("netbox", SETTINGS)

    async def _graphql(query, variables=None):
        return gql or {}

    async def _rest_list(path, params=None, **kwargs):
        if queries is not None:
            queries.append((path, list(params or [])))
        rows = list((rest_list or {}).get(path, []))
        # NetBox ORs a repeated param, so collect the values and match with `in`.
        # Re-filtering per value ANDed them, so any multi-value query returned
        # nothing — including the batched shape render._assigned_overlays relies on.
        wanted = {str(value) for key, value in params or [] if key == "assigned_object_id"}
        if wanted:
            rows = [r for r in rows if str(r.get("assigned_object_id")) in wanted]
        return rows

    async def _rest_request(method, path, **kwargs):
        if record is not None:
            record.append((method, path, kwargs.get("json")))
        if method == "GET":
            return (rest_get or {}).get(path)
        return (post or {}).get(path, {"id": 99})

    client.graphql_query = _graphql
    client.rest_list = _rest_list
    client.rest_request = _rest_request
    for attr, fn in (patch or {}).items():
        setattr(client, attr, fn)
    return client


def params_for(queries, path):
    """Return the flattened ``(key, value)`` params of every recorded call to ``path``."""
    return [param for called_path, params in queries if called_path == path for param in params]
