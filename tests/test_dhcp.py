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
    # Keyed by the UUID-encoded device id so it matches the (UUID-encoded) device_id
    # the confgen reads from reservations/option-candidates when resolving options.
    assert await client.get_dhcp_contexts() == {"00000000-0000-0000-0000-000000000001": {"x": 1}}


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
    assert str(subnet["prefix"]) == "10.0.0.0/24"
    assert str(subnet["gateway"]) == "10.0.0.1"
    assert [str(ip) for ip in subnet["pool_ips"]] == ["10.0.0.50"]
    assert subnet["reservations"] == []


async def test_get_dhcp_auto_subnets_derives_gateway_when_unset():
    # Matches the reference DCIM provider: with no nvcm_gateway, derive the gateway
    # (/31,/127 -> network address; else first usable .1). nvcm_gateway is an override.
    client = _client(
        rest_list={
            "ipam/prefixes/": [
                {"id": 1, "prefix": "10.120.1.0/24", "family": {"value": 4}},  # derive -> .1
                {"id": 2, "prefix": "10.120.0.0/31", "family": {"value": 4}},  # derive -> .0
                {
                    "id": 3,
                    "prefix": "10.0.0.0/24",
                    "family": {"value": 4},
                    "custom_fields": {"nvcm_gateway": {"address": "10.0.0.254/24"}},  # override
                },
            ],
        }
    )
    subnets = {str(s["prefix"]): str(s["gateway"]) for s in await client.get_dhcp_auto_subnets()}
    assert subnets == {
        "10.120.1.0/24": "10.120.1.1",  # first usable, derived
        "10.120.0.0/31": "10.120.0.0",  # network address for /31
        "10.0.0.0/24": "10.0.0.254",  # explicit override wins
    }


async def test_get_dhcp_auto_subnets_resolves_interface_reservation():
    """A dhcp-reserve IP assigned to a device interface resolves through
    interface -> device into a reservation entry (the device-resolution branch,
    previously uncovered)."""
    reserve_ip = {
        "address": "10.0.0.60/24",
        "assigned_object_type": "dcim.interface",
        "assigned_object": {"id": 11},
    }
    client = fake_client(
        rest_list={
            "ipam/prefixes/": [
                {
                    "id": 1,
                    "prefix": "10.0.0.0/24",
                    "family": {"value": 4},
                    "custom_fields": {"nvcm_gateway": {"address": "10.0.0.1/24"}},
                }
            ],
            "ipam/ip-addresses/": [reserve_ip],
            # ZTP-enabled comes from the DeviceConfigStatus model, not a device CF.
            "plugins/nv-config-manager/device-config-statuses/": [
                {"device": 2, "ztp_enabled": True}
            ],
        },
        rest_get={
            "dcim/interfaces/11/": {
                "id": 11,
                "mac_address": "aa:bb:cc:00:00:09",
                "device": {"id": 2},
            },
            "dcim/devices/2/": {
                "id": 2,
                "serial": "SN2",
                "platform": {"name": "cumulus-linux"},
            },
        },
    )
    subnets = await client.get_dhcp_auto_subnets()
    reservations = subnets[0]["reservations"]
    assert len(reservations) == 1
    assert reservations[0]["serial"] == "SN2"
    assert reservations[0]["mac_address"] == "aa:bb:cc:00:00:09"
    # device_id is UUID-encoded so the confgen's ZTP boot-file URL resolves in the
    # UUID-keyed config-store.
    assert reservations[0]["device_id"] == "00000000-0000-0000-0000-000000000002"


async def test_get_dhcp_auto_subnets_builds_option_candidate_from_pool_ip():
    """A dhcp-pool IP assigned to an interface becomes an option candidate
    carrying the interface role (read from the nvcm_interface_role custom field,
    since NetBox has no native interface role). This is what lets a pool/DHCP
    client — e.g. an OOB management leaf's Uplink — receive subnet-level ZTP
    options it can't get from a reservation."""
    pool_ip = {
        "address": "10.0.0.5/24",
        "assigned_object_type": "dcim.interface",
        "assigned_object": {"id": 21},
    }
    client = fake_client(
        rest_list={
            "ipam/prefixes/": [
                {
                    "id": 1,
                    "prefix": "10.0.0.0/24",
                    "family": {"value": 4},
                    "custom_fields": {"nvcm_gateway": {"address": "10.0.0.1/24"}},
                }
            ],
            # Same fake returns this list for both the dhcp-pool and dhcp-reserve
            # queries; the reserve path drops it (no dcim.interface reserve here is
            # distinct), so assert on the candidate specifically.
            "ipam/ip-addresses/": [pool_ip],
            "plugins/nv-config-manager/device-config-statuses/": [
                {"device": 3, "ztp_enabled": True}
            ],
        },
        rest_get={
            "dcim/interfaces/21/": {
                "id": 21,
                "name": "swp1",
                "mac_address": "aa:bb:cc:00:00:21",
                "device": {"id": 3},
                "custom_fields": {"nvcm_interface_role": "Uplink"},
            },
            "dcim/devices/3/": {
                "id": 3,
                "name": "oob-mleaf-01",
                "serial": "SN3",
                "platform": {"name": "cumulus-linux"},
            },
        },
    )
    subnets = await client.get_dhcp_auto_subnets()
    candidates = subnets[0]["option_candidates"]
    assert len(candidates) == 1
    assert candidates[0]["interface_role"] == "Uplink"
    assert candidates[0]["interface_name"] == "swp1"
    assert str(candidates[0]["address"]) == "10.0.0.5"
    assert candidates[0]["device_id"] == "00000000-0000-0000-0000-000000000003"
