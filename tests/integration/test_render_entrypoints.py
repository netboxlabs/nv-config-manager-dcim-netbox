# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Entrypoint-render compatibility (checklist item 6, partial).

Builds ``RenderData`` from a NetBox device through the provider, round-trips it
through the portable cache, and drives the real ``nv_config_manager_templates``
``Renderer``:

* entrypoint **discovery** works from the provider's identity + firmware output
  (`Renderer.list_entrypoints`), and
* every discovered entrypoint is **structurally consumable** — rendering either
  produces a string or raises ``FilterException`` (the expected signal for a
  ``RenderData`` section this provider does not yet populate; see
  ``docs/netbox-data-mapping.mdx``). Any other exception means the provider
  produced a structurally wrong ``RenderData`` and fails the test.

Fully rendering every reference entrypoint to golden config for every mock device
additionally requires populating the currently-defaulted sections (firmware
bundles, users, EVPN, …) — tracked as the remaining part of item 6.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from nv_config_manager_dcim import RenderData, RenderDataRequest, create_dcim_client

pytest.importorskip("nv_config_manager_templates")
from nv_config_manager_templates.render import Renderer  # noqa: E402

# A device profile that maps to a bundled reference template dir:
# platform "Cumulus Linux" -> cumulus-linux, role "Border Leaf" -> border-leaf,
# desired firmware 5.13.1  (see templates/cumulus-linux/border-leaf/5.13.1/entrypoint).
_DEVICE = {
    "id": "1",
    "name": "border-leaf-01",
    "role": {"name": "Border Leaf"},
    "platform": {"name": "Cumulus Linux"},
    "device_type": {"model": "SN5600", "manufacturer": {"name": "NVIDIA"}},
    "tags": [],
    "config_context": {
        "intended-firmware": {"version": "5.13.1"},
        "dns": {"ipv4": ["10.0.0.1"]},
        "ntp": {"ipv4": ["10.0.0.2"]},
        "ztp": {"ipv4": ["10.0.0.9"]},
        "bgp": {"asn": "65000"},
        "password_mappings": {
            "admin": {"password": "root_password", "rotation": "P30D", "role": "admin"},
        },
        "evpn_esi_base_mac": "00:11:22:33:44:55",
        "fabric-mac": "00:aa:bb:cc:dd:ee",
        "evpn": {"df-preference": 100},
    },
    "site": {"id": "2", "name": "DM-Akron", "slug": "dm-akron", "region": {"name": "Ohio"}},
    "location": None,
    "interfaces": [
        {
            "id": "10",
            "name": "lo",
            "enabled": True,
            "type": "virtual",
            "ip_addresses": [{"address": "10.0.0.5/32", "role": None}],
        },
        {
            "id": "11",
            "name": "eth0",
            "enabled": True,
            "type": "1000base-t",
            "mgmt_only": True,
            "ip_addresses": [{"address": "10.10.10.10/24", "role": None}],
        },
    ],
}
_REST = {
    "ipam/prefixes/": [{"prefix": "10.0.0.0/24"}, {"prefix": "10.0.0.4/31"}],
}

_EXPECTED_PATH = "cumulus-linux/border-leaf/5.13.1/entrypoint/"


async def _render_via_provider() -> RenderData:
    client = create_dcim_client("netbox", {"server": "https://netbox.example", "token": "t"})

    async def fake_graphql(query, variables=None):
        return {"device": _DEVICE}

    async def fake_rest_list(path, params=None):
        return _REST.get(path, [])

    client.graphql_query = fake_graphql
    client.rest_list = fake_rest_list
    try:
        return await client.get_render_data(RenderDataRequest(device_id="1"))
    finally:
        await client.close()


@pytest.fixture(scope="module")
def render_data() -> RenderData:
    rd = asyncio.run(_render_via_provider())
    return RenderData.from_cache(rd.to_cache())  # provider-neutral cached fixture


@pytest.fixture(scope="module")
def renderer() -> Renderer:
    os.environ["NV_CONFIG_MANAGER_SKIP_VAULT"] = "1"
    return Renderer()


def test_entrypoints_discovered_from_provider_output(render_data, renderer):
    entrypoints = renderer.list_entrypoints(render_data.device)
    assert entrypoints, "no reference entrypoints discovered for the device profile"
    assert all(ep.startswith(_EXPECTED_PATH) for ep in entrypoints)


def test_all_entrypoints_render_from_provider_data(render_data, renderer):
    """Every discovered entrypoint renders to real config from our RenderData.

    The mock device carries a complete config context (services, firmware,
    routing/ASN, EVPN, credentials) plus a management interface, so the shared
    templates render end-to-end with no provider-specific branch. A
    ``FilterException`` here means a required ``RenderData`` field is unmapped or
    the mock is missing data; any other exception means a structural mapping bug.
    """
    entrypoints = renderer.list_entrypoints(render_data.device)
    assert entrypoints
    for entrypoint in entrypoints:
        output = renderer.render(entrypoint, render_data)
        assert isinstance(output, str) and output.strip(), f"{entrypoint} rendered empty"
