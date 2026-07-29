# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Change-event tests: envelope normalization, handler registration, and the
affected-device fan-out for each native event type the provider registers.
"""

from __future__ import annotations

from nv_config_manager_dcim import DCIMChangeEvent

from nv_config_manager_dcim_netbox.events import (
    _bgp_session_changed,
    _device_changed,
    _interface_changed,
    _overlay_assignment_changed,
    register_render_event_handlers,
)
from nv_config_manager_dcim_netbox.provider import NetBoxProvider

_OVERLAY_TYPE = "netbox_nv_config_manager.overlayassignment"


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
        _OVERLAY_TYPE,
        "netbox_bgp.bgpsession",
    }


# --- affected-device fan-out, per native event type ----------------------------


async def test_device_change_fans_out_to_itself():
    requests = await _device_changed(_event("dcim.device", "5"), None)
    assert [r.device_id for r in requests] == ["5"]
    assert requests[0].commit_message  # a non-empty render reason is set


async def test_interface_change_fans_out_to_parent_device():
    event = _event("dcim.interface", "11", {"device": {"id": 7}})
    requests = await _interface_changed(event, None)
    assert [r.device_id for r in requests] == ["7"]


async def test_interface_change_without_device_is_noop():
    requests = await _interface_changed(_event("dcim.interface", "11", {}), None)
    assert requests == ()


async def test_bgp_session_change_fans_out_to_device():
    event = _event("netbox_bgp.bgpsession", "3", {"device": {"id": 9}})
    requests = await _bgp_session_changed(event, None)
    assert [r.device_id for r in requests] == ["9"]


async def test_overlay_assignment_to_device():
    event = _event(
        _OVERLAY_TYPE, "1", {"assigned_object_type": "dcim.device", "assigned_object_id": 4}
    )
    requests = await _overlay_assignment_changed(event, None)
    assert [r.device_id for r in requests] == ["4"]


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
    assert [r.device_id for r in requests] == ["8"]


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
