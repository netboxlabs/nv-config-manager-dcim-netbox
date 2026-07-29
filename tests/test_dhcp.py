# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Behavioral tests for the DHCP methods (stubbed transport)."""

from __future__ import annotations

from tests._fake import fake_client


def _client(profiles=None, rest_list=None):
    async def fake_profiles(name):
        return (profiles or {}).get(name, [])

    return fake_client(rest_list=rest_list, patch={"config_contexts_for_profile": fake_profiles})


async def test_get_dhcp_site_options():
    client = _client(
        profiles={"nv-config-manager-site-dhcp-option-def": [{"data": {"option-def": [1]}}]}
    )
    assert await client.get_dhcp_site_options() == {"option-def": [1]}


async def test_get_dhcp_static_data():
    client = _client(
        profiles={"nv-config-manager-kea-static-data": [{"data": {"a": 1}}, {"data": {"b": 2}}]}
    )
    assert await client.get_dhcp_static_data() == [{"a": 1}, {"b": 2}]


async def test_get_dhcp_contexts():
    client = _client(rest_list={"dcim/devices/": [{"id": 1, "config_context": {"x": 1}}]})
    assert await client.get_dhcp_contexts() == {"1": {"x": 1}}


async def test_get_dhcp_auto_subnets():
    client = _client(
        rest_list={
            "ipam/prefixes/": [
                {
                    "id": 1,
                    "prefix": "10.0.0.0/24",
                    "family": {"value": 4},
                    "custom_fields": {"nvcm_gateway": {"address": "10.0.0.1/24"}},
                }
            ],
            "ipam/ip-addresses/": [{"address": "10.0.0.50/24"}],
        }
    )
    subnets = await client.get_dhcp_auto_subnets()
    assert len(subnets) == 1
    subnet = subnets[0]
    assert subnet["prefix"] == "10.0.0.0/24"
    assert subnet["gateway"] == "10.0.0.1"
    assert subnet["pool_ips"] == ["10.0.0.50"]
    assert subnet["reservations"] == []
