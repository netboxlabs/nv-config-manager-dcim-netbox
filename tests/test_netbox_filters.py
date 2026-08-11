# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Pin the NetBox query-filter names the provider's reads depend on.

These spellings are the provider's real contract with NetBox: several reads are
only correctly scoped because of one exact parameter name. A typo in any of them
silently changes what NetBox returns — and is invisible to a fake transport that
routes on path alone, so assert them directly.

The NVCM management flags are ``DeviceConfigStatus`` fields, never ``cf_nvcm_*``
filters on ``dcim/devices``: NetBox drops unknown params, so such a filter returns
the whole fleet.
"""

from __future__ import annotations

import pytest
from nv_config_manager_dcim import DCIMDeviceSelectionFilter
from nv_config_manager_dcim.workflow_models import DeviceInventoryFilter

from nv_config_manager_dcim_netbox.fabric_common import STATUS_PATH
from tests._fake import fake_client, params_for

_DEVICES = "dcim/devices/"
_INTERFACES = "dcim/interfaces/"
_PREFIXES = "ipam/prefixes/"
_IPS = "ipam/ip-addresses/"


def _client(**kwargs):
    queries: list[tuple[str, list]] = []
    return fake_client(queries=queries, **kwargs), queries


@pytest.mark.inventory
async def test_managed_metadata_resolves_render_enabled_from_the_status_endpoint():
    # cf_nvcm_render_enabled names no real custom field, so NetBox would drop it and
    # return the whole fleet.
    client, queries = _client(rest_list={STATUS_PATH: [{"id": 1, "device": 4}]})
    await client.get_managed_device_metadata()
    assert ("render_enabled", "true") in params_for(queries, STATUS_PATH)
    device_params = params_for(queries, _DEVICES)
    assert ("id", "4") in device_params
    assert not any(key.startswith("cf_nvcm_") for key, _ in device_params)


@pytest.mark.inventory
async def test_managed_metadata_without_status_records_never_queries_the_fleet():
    client, queries = _client(rest_list={STATUS_PATH: []})
    assert await client.get_managed_device_metadata() == []
    # No device read at all; an unfiltered listing here is the whole-fleet failure.
    assert params_for(queries, _DEVICES) == []


@pytest.mark.inventory
async def test_list_devices_requires_primary_ip_and_maps_every_filter():
    client, queries = _client(rest_list={STATUS_PATH: [{"id": 1, "device": 9}]})
    await client.list_devices(
        DCIMDeviceSelectionFilter(
            sites=("dc1",),
            statuses=("active",),
            roles=("leaf",),
            tenants=("acme",),
            device_type_ids=("7",),
            manufacturers=("nvidia",),
            platforms=("nv-os",),
            managed_only=True,
        )
    )
    params = params_for(queries, _DEVICES)
    assert ("has_primary_ip", "true") in params
    for expected in (
        ("site", "dc1"),
        ("status", "active"),
        ("role", "leaf"),
        ("tenant", "acme"),
        ("device_type_id", "7"),
        ("manufacturer", "nvidia"),
        ("platform", "nv-os"),
        # managed_only narrows to the render-enabled device ids, ANDed with the rest.
        ("id", "9"),
    ):
        assert expected in params, expected
    assert ("render_enabled", "true") in params_for(queries, STATUS_PATH)
    # Every value reaches aiohttp as a string.
    assert all(isinstance(value, str) for _, value in params)


@pytest.mark.inventory
async def test_inventory_status_flags_are_queried_on_the_status_endpoint():
    client, queries = _client(rest_list={STATUS_PATH: [{"id": 1, "device": 3}]})
    await client.get_network_devices(
        DeviceInventoryFilter(
            site="dc1", render_enabled=True, deploy_enabled=False, backup_enabled=True
        )
    )
    # One query, so the flags AND rather than selecting devices matching any of them.
    status_params = params_for(queries, STATUS_PATH)
    assert ("render_enabled", "true") in status_params
    assert ("deploy_enabled", "false") in status_params
    assert ("backup_enabled", "true") in status_params
    device_params = params_for(queries, _DEVICES)
    assert ("site", "dc1") in device_params
    assert ("id", "3") in device_params
    assert not any(key.startswith("cf_nvcm_") for key, _ in device_params)


@pytest.mark.ib
async def test_guid_lookup_uses_the_case_insensitive_custom_field_filter():
    # UFM reports GUIDs in mixed case; the ``__ie`` lookup is what makes the match
    # case-insensitive. Losing it silently stops matching real GUIDs.
    client, queries = _client()
    await client.find_ib_interfaces_by_guids(["0xABCD"])
    assert ("cf_ib_guid", "0xABCD") in params_for(queries, _INTERFACES)


@pytest.mark.dhcp
async def test_dhcp_reads_are_scoped_by_tag_and_family():
    client, queries = _client()
    await client.get_dhcp_auto_subnets(family=4)
    prefix_params = params_for(queries, _PREFIXES)
    assert ("tag", "dhcp-subnet") in prefix_params
    # Family is filtered server-side rather than over-fetching both families.
    assert ("family", "4") in prefix_params
    ip_params = params_for(queries, _IPS)
    assert ("tag", "dhcp-pool") in ip_params
    assert ("tag", "dhcp-reserve") in ip_params


@pytest.mark.dhcp
async def test_dhcp_contexts_resolve_ztp_eligibility_from_the_status_endpoint():
    client, queries = _client(rest_list={STATUS_PATH: [{"id": 1, "device": 6}]})
    await client.get_dhcp_contexts(is_aggregate_managed=True)
    status_params = params_for(queries, STATUS_PATH)
    assert ("ztp_enabled", "true") in status_params
    assert ("is_aggregate_managed", "true") in status_params
    device_params = params_for(queries, _DEVICES)
    assert ("id", "6") in device_params
    assert not any(key.startswith("cf_nvcm_") for key, _ in device_params)


@pytest.mark.state
async def test_backup_eligibility_ors_active_and_provisioned_statuses():
    # ZTP writes status=provisioned, so a freshly-provisioned device must still be
    # eligible for backup; NetBox ORs a repeated status param.
    client, queries = _client()
    await client.get_backup_enabled_device_ids(is_aggregate_managed=True)
    device_params = params_for(queries, _DEVICES)
    assert ("status", "active") in device_params
    assert ("status", "provisioned") in device_params
    status_params = params_for(queries, STATUS_PATH)
    assert ("backup_enabled", "true") in status_params
    assert ("is_aggregate_managed", "true") in status_params
