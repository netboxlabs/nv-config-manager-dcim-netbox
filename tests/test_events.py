# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Change-event tests: envelope normalization, handler registration, and the
affected-device fan-out for each native event type the provider registers.
"""

from __future__ import annotations

from nv_config_manager_dcim import DCIMChangeEvent

from nv_config_manager_dcim_netbox.client import decode_device_id
from nv_config_manager_dcim_netbox.events import (
    _bgp_session_changed,
    _cable_changed,
    _config_context_changed,
    _device_changed,
    _interface_changed,
    _ip_address_changed,
    _overlay_assignment_changed,
    _parent_device_changed,
    _prefix_changed,
    _vrf_changed,
    register_render_event_handlers,
)
from nv_config_manager_dcim_netbox.provider import NetBoxProvider

_OVERLAY_TYPE = "netbox_nv_config_manager.overlayassignment"


class _FanoutClient:
    """Stubbed transport for cross-object fan-out resolution."""

    def __init__(self, rest_get=None, rest_list=None):
        self._get = rest_get or {}
        self._list = rest_list or {}

    async def rest_request(self, method, path, **kwargs):
        return self._get.get(path)

    async def rest_list(self, path, params=None, **kwargs):
        return self._list.get(path, [])


def _event(object_type, object_id, record=None, operation="update"):
    return DCIMChangeEvent.from_dict(
        {
            "provider": "netbox",
            "operation": operation,
            "object_type": object_type,
            "object_id": str(object_id),
            "timestamp": "2026-07-29T00:00:00Z",
            "record": record or {},
            "changed_fields": [],
            "contract_version": "1.0",
        }
    )


# --- normalization + registration ----------------------------------------------


def test_normalize_event_parses_envelope():
    event = NetBoxProvider().normalize_event(
        {
            "provider": "netbox",
            "operation": "update",
            "object_type": "dcim.device",
            "object_id": "1",
            "timestamp": "2026-07-29T00:00:00Z",
            "record": {},
            "changed_fields": ["status"],
            "contract_version": "1.0",
        }
    )
    assert isinstance(event, DCIMChangeEvent)
    assert (event.object_type, event.object_id, event.operation) == ("dcim.device", "1", "update")


def test_register_render_event_handlers_covers_every_native_type():
    registered = {}

    class Registry:
        def register_render_event_handler(self, object_type, handler):
            registered[object_type] = handler

    register_render_event_handlers(Registry())
    assert set(registered) == {
        "dcim.device",
        "dcim.interface",
        "dcim.rearport",
        "dcim.frontport",
        "dcim.cable",
        "extras.configcontext",
        "ipam.vrf",
        "ipam.prefix",
        "ipam.ipaddress",
        "ipam.asn",
        "netbox_bgp.bgpsession",
        "netbox_bgp.bgppeergroup",
        _OVERLAY_TYPE,
        "netbox_nv_config_manager.deviceconfigstatus",
    }


# --- affected-device fan-out, per native event type ----------------------------


async def test_device_change_fans_out_to_itself():
    requests = await _device_changed(_event("dcim.device", "5"), None)
    assert [decode_device_id(r.device_id) for r in requests] == ["5"]
    assert requests[0].commit_message  # a non-empty render reason is set


async def test_interface_change_fans_out_to_parent_device():
    event = _event("dcim.interface", "11", {"device": {"id": 7}})
    requests = await _interface_changed(event, None)
    assert [decode_device_id(r.device_id) for r in requests] == ["7"]


async def test_interface_change_without_device_is_noop():
    requests = await _interface_changed(_event("dcim.interface", "11", {}), None)
    assert requests == ()


async def test_bgp_session_change_fans_out_to_device():
    event = _event("netbox_bgp.bgpsession", "3", {"device": {"id": 9}})
    requests = await _bgp_session_changed(event, None)
    assert [decode_device_id(r.device_id) for r in requests] == ["9"]


async def test_overlay_assignment_to_device():
    event = _event(
        _OVERLAY_TYPE, "1", {"assigned_object_type": "dcim.device", "assigned_object_id": 4}
    )
    requests = await _overlay_assignment_changed(event, None)
    assert [decode_device_id(r.device_id) for r in requests] == ["4"]


async def test_overlay_assignment_to_interface_resolves_device_via_client():
    calls = {}

    class FakeClient:
        async def rest_request(self, method, path):
            calls["path"] = path
            return {"device": {"id": 8}}

    event = _event(
        _OVERLAY_TYPE, "1", {"assigned_object_type": "dcim.interface", "assigned_object_id": 12}
    )
    requests = await _overlay_assignment_changed(event, FakeClient())
    assert calls["path"] == "dcim/interfaces/12/"
    assert [decode_device_id(r.device_id) for r in requests] == ["8"]


async def test_overlay_assignment_interface_resolution_error_is_noop():
    class FakeClient:
        async def rest_request(self, *args, **kwargs):
            raise RuntimeError("boom")

    event = _event(
        _OVERLAY_TYPE, "1", {"assigned_object_type": "dcim.interface", "assigned_object_id": 12}
    )
    requests = await _overlay_assignment_changed(event, FakeClient())
    assert requests == ()


async def test_overlay_assignment_without_assigned_id_is_noop():
    event = _event(_OVERLAY_TYPE, "1", {"assigned_object_type": "dcim.device"})
    requests = await _overlay_assignment_changed(event, None)
    assert requests == ()


# --- cross-object fan-out (parity with the reference DCIM provider handlers) ---------


async def test_port_and_config_status_fan_out_to_parent_device():
    for object_type in (
        "dcim.rearport",
        "dcim.frontport",
        "netbox_nv_config_manager.deviceconfigstatus",
    ):
        event = _event(object_type, "1", {"device": {"id": 7}})
        requests = await _parent_device_changed(event, None)
        assert [decode_device_id(r.device_id) for r in requests] == ["7"], object_type


async def test_vrf_change_fans_out_to_interface_devices():
    client = _FanoutClient(
        rest_list={
            "dcim/interfaces/": [
                {"device": {"id": 3}},
                {"device": {"id": 5}},
                {"device": {"id": 3}},
            ]
        }
    )
    requests = await _vrf_changed(_event("ipam.vrf", "9"), client)
    assert sorted(decode_device_id(r.device_id) for r in requests) == ["3", "5"]  # deduplicated


async def test_ip_address_change_fans_out_to_assigned_device():
    client = _FanoutClient(
        rest_get={
            "ipam/ip-addresses/4/": {
                "assigned_object_type": "dcim.interface",
                "assigned_object": {"device": {"id": 6}},
            }
        }
    )
    requests = await _ip_address_changed(_event("ipam.ipaddress", "4"), client)
    assert [decode_device_id(r.device_id) for r in requests] == ["6"]


async def test_ip_address_not_on_interface_is_noop():
    client = _FanoutClient(
        rest_get={"ipam/ip-addresses/4/": {"assigned_object_type": "virtualization.vminterface"}}
    )
    assert await _ip_address_changed(_event("ipam.ipaddress", "4"), client) == ()


async def test_cable_change_fans_out_to_both_terminations():
    client = _FanoutClient(
        rest_get={
            "dcim/cables/2/": {
                "a_terminations": [{"object": {"device": {"id": 1}}}],
                "b_terminations": [{"object": {"device": {"id": 2}}}],
            }
        }
    )
    requests = await _cable_changed(_event("dcim.cable", "2"), client)
    assert sorted(decode_device_id(r.device_id) for r in requests) == ["1", "2"]


async def test_prefix_change_fans_out_by_scope():
    client = _FanoutClient(
        rest_get={"ipam/prefixes/8/": {"scope_type": "dcim.site", "scope_id": 3}},
        rest_list={"dcim/devices/": [{"id": 10}, {"id": 11}]},
    )
    requests = await _prefix_changed(_event("ipam.prefix", "8"), client)
    assert sorted(decode_device_id(r.device_id) for r in requests) == ["10", "11"]


async def test_prefix_without_scope_is_noop():
    client = _FanoutClient(rest_get={"ipam/prefixes/8/": {"scope_type": None, "scope_id": None}})
    assert await _prefix_changed(_event("ipam.prefix", "8"), client) == ()


async def test_config_context_change_fans_out_to_matching_devices():
    client = _FanoutClient(
        rest_get={
            "extras/config-contexts/5/": {"roles": [{"id": 2}], "tags": [{"slug": "fabric"}]}
        },
        rest_list={"dcim/devices/": [{"id": 20}]},
    )
    requests = await _config_context_changed(_event("extras.configcontext", "5"), client)
    assert [decode_device_id(r.device_id) for r in requests] == ["20"]


async def test_cross_object_delete_is_noop():
    # The object is gone on delete, so it can no longer be resolved.
    assert await _vrf_changed(_event("ipam.vrf", "9", operation="delete"), None) == ()


async def test_cross_object_resolution_error_is_noop():
    class _Boom:
        async def rest_list(self, *args, **kwargs):
            raise RuntimeError("boom")

        async def rest_request(self, *args, **kwargs):
            raise RuntimeError("boom")

    assert await _vrf_changed(_event("ipam.vrf", "9"), _Boom()) == ()


async def test_a_malformed_device_id_drops_the_fanout_instead_of_raising():
    """A device id that is not a NetBox PK cannot be UUID-encoded. The device and
    parent-device handlers must drop it (like the best-effort cross-object
    handlers) rather than raise into the dispatcher."""
    assert await _device_changed(_event("dcim.device", "not-a-pk"), None) == ()
    event = _event("dcim.interface", "11", {"device": {"id": "not-a-pk"}})
    assert await _parent_device_changed(event, None) == ()


async def test_parent_device_handler_reads_a_bare_device_pk():
    # The record's `device` may be a bare PK rather than a nested object.
    requests = await _parent_device_changed(_event("dcim.interface", "11", {"device": 7}), None)
    assert [decode_device_id(r.device_id) for r in requests] == ["7"]


async def test_multi_device_fanout_dedupes_and_preserves_order():
    client = _FanoutClient(
        rest_get={"dcim/cables/3/": {"a_terminations": [], "b_terminations": []}}
    )
    # Resolver order is preserved and repeats collapse to one request each.
    from nv_config_manager_dcim_netbox.events import _requests_multi

    requests = _requests_multi(_event("dcim.cable", "3"), [9, 4, 9, None, 4])
    assert [decode_device_id(r.device_id) for r in requests] == ["9", "4"]
    assert await _cable_changed(_event("dcim.cable", "3"), client) == ()
