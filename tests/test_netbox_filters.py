# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Pin the NetBox query-filter names the provider's reads depend on.

These spellings are the provider's real contract with NetBox: a custom-field
filter is guarded on the field existing (NetBox 400s on an unknown ``cf_*``
rather than widening the result), and several reads are only correctly scoped
because of one exact parameter name. A typo in any of them silently changes what
NetBox returns — and is invisible to a fake transport that routes on path alone,
so assert them directly.
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
async def test_managed_metadata_is_guarded_on_the_render_enabled_custom_field():
    # "Managed" must stay scoped to the custom field: without it this read returns
    # the whole fleet instead of failing closed.
    client, queries = _client()
    await client.get_managed_device_metadata()
    assert ("cf_nvcm_render_enabled", "true") in params_for(queries, _DEVICES)


@pytest.mark.inventory
async def test_list_devices_requires_primary_ip_and_maps_every_filter():
    client, queries = _client()
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
        ("cf_nvcm_render_enabled", "true"),
    ):
        assert expected in params, expected
    # Every value reaches aiohttp as a string.
    assert all(isinstance(value, str) for _, value in params)


@pytest.mark.inventory
async def test_inventory_status_flags_map_to_custom_fields():
    client, queries = _client()
    await client.get_network_devices(
        DeviceInventoryFilter(
            site="dc1", render_enabled=True, deploy_enabled=False, backup_enabled=True
        )
    )
    params = params_for(queries, _DEVICES)
    assert ("cf_nvcm_render_enabled", "true") in params
    assert ("cf_nvcm_deploy_enabled", "false") in params
    assert ("cf_nvcm_backup_enabled", "true") in params


@pytest.mark.ib
async def test_guid_lookup_uses_the_case_insensitive_custom_field_filter():
    # UFM reports GUIDs in mixed case; the ``__ie`` lookup is what makes the match
    # case-insensitive. Losing it silently stops matching real GUIDs.
    client, queries = _client()
    await client.find_ib_interfaces_by_guids(["0xABCD"])
    assert ("cf_ib_guid__ie", "0xABCD") in params_for(queries, _INTERFACES)


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
async def test_dhcp_contexts_are_guarded_on_the_ztp_custom_field():
    client, queries = _client()
    await client.get_dhcp_contexts(is_aggregate_managed=True)
    params = params_for(queries, _DEVICES)
    assert ("cf_nvcm_ztp_enabled", "true") in params
    assert ("cf_nvcm_is_aggregate_managed", "true") in params


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
