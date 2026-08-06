# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Parity fixes for DHCP defaulting, backup status, and shared-BGP fan-out.

Covers the provider-parity corrections that align the NetBox provider with the
reference DCIM provider's semantics:

* DHCP reservations default ZTP to enabled (only an explicit ``ztp_enabled=False``
  drops them) and treat a missing aggregate-managed flag as unconstrained.
* Explicit ``nvcm_gateway`` overrides are validated to lie within the subnet.
* Nested ``dhcp-subnet`` prefixes assign each IP to its most-specific subnet only.
* Backup eligibility includes ``provisioned`` devices, not just ``active``.
* Editing a shared BGP ASN (core ``ipam.asn``) or peer group re-renders every
  device whose ``netbox-bgp`` session references it.
"""

from __future__ import annotations

import ipaddress

import pytest
from nv_config_manager_dcim import DCIMChangeEvent, DCIMInvalidDataError

from nv_config_manager_dcim_netbox.client import decode_device_id, encode_device_id
from nv_config_manager_dcim_netbox.events import (
    _bgp_asn_changed,
    _bgp_peer_group_changed,
    register_render_event_handlers,
)
from tests._fake import fake_client

# --- DHCP: ZTP defaulting (Fix 1) -----------------------------------------------


def _reserve_prefix_client(statuses):
    """Client with one /24 subnet and one interface-assigned dhcp-reserve IP."""
    reserve_ip = {
        "address": "10.0.0.60/24",
        "assigned_object_type": "dcim.interface",
        "assigned_object": {"id": 11},
    }
    return fake_client(
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
            "plugins/nv-config-manager/device-config-statuses/": statuses,
        },
        rest_get={
            "dcim/interfaces/11/": {
                "id": 11,
                "mac_address": "aa:bb:cc:00:00:09",
                "device": {"id": 2},
            },
            "dcim/devices/2/": {"id": 2, "serial": "SN2", "platform": {"name": "cumulus-linux"}},
        },
    )


async def test_reservation_defaults_ztp_enabled_when_status_missing():
    # No DeviceConfigStatus row: ZTP defaults to enabled, so the reservation is
    # kept (previously ``not status.get("ztp_enabled")`` wrongly dropped it).
    client = _reserve_prefix_client(statuses=[])
    reservations = (await client.get_dhcp_auto_subnets())[0]["reservations"]
    assert [r["serial"] for r in reservations] == ["SN2"]


async def test_reservation_dropped_only_when_ztp_explicitly_disabled():
    client = _reserve_prefix_client(statuses=[{"device": 2, "ztp_enabled": False}])
    reservations = (await client.get_dhcp_auto_subnets())[0]["reservations"]
    assert reservations == []


# --- DHCP: aggregate-managed matching (Fix 2) -----------------------------------


async def test_aggregate_matches_treats_missing_flag_as_unconstrained():
    client = fake_client()
    # No mode requested, or no/absent flag: always a match (unconstrained).
    assert client._aggregate_matches({}, None) is True
    assert client._aggregate_matches({}, True) is True
    assert client._aggregate_matches({}, False) is True
    assert client._aggregate_matches({"render_enabled": True}, True) is True
    # Flag present: enforce the exact match.
    assert client._aggregate_matches({"is_aggregate_managed": True}, True) is True
    assert client._aggregate_matches({"is_aggregate_managed": False}, True) is False
    assert client._aggregate_matches({"is_aggregate_managed": True}, False) is False


async def test_reservation_kept_in_aggregate_mode_without_status():
    # Aggregate mode requested but the device has no status row: it must not be
    # excluded (the empty-status device is unconstrained).
    client = _reserve_prefix_client(statuses=[])
    subnets = await client.get_dhcp_auto_subnets(is_aggregate_managed=True)
    assert [r["serial"] for r in subnets[0]["reservations"]] == ["SN2"]


# --- DHCP: explicit gateway validation (Fix 3) ----------------------------------


async def test_explicit_gateway_within_subnet_is_returned():
    client = fake_client()
    gateway = await client._gateway_address(
        {"prefix": "10.0.0.0/24", "custom_fields": {"nvcm_gateway": {"address": "10.0.0.5/24"}}}
    )
    assert gateway == "10.0.0.5"


async def test_explicit_gateway_outside_subnet_raises():
    client = fake_client()
    with pytest.raises(DCIMInvalidDataError):
        await client._gateway_address(
            {
                "prefix": "10.0.0.0/24",
                "custom_fields": {"nvcm_gateway": {"address": "10.9.9.9/24"}},
            }
        )


async def test_explicit_gateway_by_ip_id_is_resolved_and_validated():
    # nvcm_gateway given as an IP-address id: resolve via REST, then validate.
    client = fake_client(rest_get={"ipam/ip-addresses/42/": {"address": "10.0.0.254/24"}})
    gateway = await client._gateway_address(
        {"prefix": "10.0.0.0/24", "custom_fields": {"nvcm_gateway": 42}}
    )
    assert gateway == "10.0.0.254"

    bad = fake_client(rest_get={"ipam/ip-addresses/42/": {"address": "192.168.0.1/24"}})
    with pytest.raises(DCIMInvalidDataError):
        await bad._gateway_address(
            {"prefix": "10.0.0.0/24", "custom_fields": {"nvcm_gateway": 42}}
        )


async def test_gateway_derived_when_unset():
    client = fake_client()
    assert await client._gateway_address({"prefix": "10.1.0.0/24"}) == "10.1.0.1"
    assert await client._gateway_address({"prefix": "10.1.0.0/31"}) == "10.1.0.0"
    # A malformed CIDR yields None (the caller skips such a prefix).
    assert await client._gateway_address({"prefix": "not-a-cidr"}) is None


# --- DHCP: nested-prefix most-specific assignment (Fix 4) -----------------------


async def test_most_specific_subnet_id_prefers_longest_prefix():
    client = fake_client()
    candidates = [
        ({"id": 1}, ipaddress.ip_network("10.0.0.0/24")),
        ({"id": 2}, ipaddress.ip_network("10.0.0.64/26")),
    ]
    # Inside the /26 -> assigned to the more specific subnet only.
    assert client._most_specific_subnet_id({"address": "10.0.0.70/24"}, candidates) == 2
    # Inside the /24 but outside the /26 -> the /24.
    assert client._most_specific_subnet_id({"address": "10.0.0.5/24"}, candidates) == 1
    # Contained by neither.
    assert client._most_specific_subnet_id({"address": "10.9.9.9/24"}, candidates) is None


async def test_nested_subnets_assign_pool_ip_to_most_specific_only():
    # 10.0.0.70 is contained by both the /24 and the nested /26; it must appear in
    # the /26's pool only, never duplicated into the enclosing /24.
    client = fake_client(
        rest_list={
            "ipam/prefixes/": [
                {"id": 1, "prefix": "10.0.0.0/24", "family": {"value": 4}},
                {"id": 2, "prefix": "10.0.0.64/26", "family": {"value": 4}},
            ],
            "ipam/ip-addresses/": [{"address": "10.0.0.70/24"}],
        }
    )
    by_id = {s["id"]: s for s in await client.get_dhcp_auto_subnets()}
    assert [str(ip) for ip in by_id[2]["pool_ips"]] == ["10.0.0.70"]
    assert by_id[1]["pool_ips"] == []


# --- Backup eligibility includes provisioned devices (Fix 5) --------------------


async def test_backup_enabled_includes_provisioned_status():
    client = fake_client(
        rest_list={
            "plugins/nv-config-manager/device-config-statuses/": [{"device": 1}, {"device": 2}],
            # Only device 1 is returned by the (status active|provisioned) query.
            "dcim/devices/": [{"id": 1}],
        }
    )
    calls: list[tuple[str, list]] = []
    inner = client.rest_list

    async def spy(path, params=None, **kwargs):
        calls.append((path, list(params or [])))
        return await inner(path, params, **kwargs)

    client.rest_list = spy

    result = await client.get_backup_enabled_device_ids(is_aggregate_managed=False)

    # Device 2 is backup-enabled but not in-service -> excluded; device 1 kept.
    assert result == {encode_device_id("1")}
    device_params = next(p for path, p in calls if path == "dcim/devices/")
    assert ("status", "active") in device_params
    assert ("status", "provisioned") in device_params


# --- Shared BGP object fan-out (Fix 6) ------------------------------------------


class _BGPSessionClient:
    """Fake that routes ``plugins/bgp/session/`` queries by filter param."""

    def __init__(self, sessions_by_param):
        self._sessions = sessions_by_param
        self.calls: list[tuple[str, list]] = []

    async def rest_list(self, path, params=None, **kwargs):
        self.calls.append((path, list(params or [])))
        rows: list[dict] = []
        for key, value in params or []:
            rows.extend(self._sessions.get((key, value), []))
        return rows

    async def rest_request(self, *args, **kwargs):
        return None


def _event(object_type, object_id, operation="update"):
    return DCIMChangeEvent.from_dict(
        {
            "provider": "netbox",
            "operation": operation,
            "object_type": object_type,
            "object_id": str(object_id),
            "timestamp": "2026-07-29T00:00:00Z",
            "record": {},
            "changed_fields": [],
            "contract_version": "1.0",
        }
    )


async def test_asn_change_fans_out_to_local_and_remote_sessions():
    # A session references an ASN as local_as OR remote_as; both are queried and
    # the owning devices unioned.
    client = _BGPSessionClient(
        {
            ("local_as_id", "7"): [{"device": {"id": 1}}],
            ("remote_as_id", "7"): [{"device": {"id": 2}}, {"device": {"id": 1}}],
        }
    )
    requests = await _bgp_asn_changed(_event("ipam.asn", "7"), client)
    assert sorted(decode_device_id(r.device_id) for r in requests) == ["1", "2"]
    queried = {key for _, params in client.calls for key, _ in params}
    assert queried == {"local_as_id", "remote_as_id"}


async def test_peer_group_change_fans_out_to_referencing_devices():
    client = _BGPSessionClient(
        {("peer_group", "5"): [{"device": {"id": 3}}, {"device": {"id": 3}}]}
    )
    requests = await _bgp_peer_group_changed(_event("netbox_bgp.bgppeergroup", "5"), client)
    # Deduplicated to a single render request for device 3.
    assert [decode_device_id(r.device_id) for r in requests] == ["3"]


async def test_shared_bgp_fanout_delete_is_noop():
    # On delete the shared object is gone and can no longer be resolved.
    client = _BGPSessionClient({("local_as_id", "7"): [{"device": {"id": 1}}]})
    assert await _bgp_asn_changed(_event("ipam.asn", "7", operation="delete"), client) == ()


async def test_shared_bgp_fanout_resolution_error_is_noop():
    class _Boom:
        async def rest_list(self, *args, **kwargs):
            raise RuntimeError("boom")

        async def rest_request(self, *args, **kwargs):
            raise RuntimeError("boom")

    assert await _bgp_asn_changed(_event("ipam.asn", "7"), _Boom()) == ()


async def test_registration_includes_shared_bgp_handlers():
    registered = {}

    class Registry:
        def register_render_event_handler(self, object_type, handler):
            registered[object_type] = handler

    register_render_event_handlers(Registry())
    # Core ipam.asn (netbox-bgp ASN) and the netbox-bgp peer group are now handled.
    assert "ipam.asn" in registered
    assert "netbox_bgp.bgppeergroup" in registered
