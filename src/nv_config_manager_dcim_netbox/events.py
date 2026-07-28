# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""NetBox change-event integration with the render dispatcher.

The render service owns the dispatcher/queue and hands each ``DCIMChangeEvent``
to the handler registered for its ``object_type``. Each handler returns the
device renders that change should trigger. The event envelope is produced by the
companion plugin's ``EVENTS_PIPELINE`` bridge and validated by
``NetBoxProvider.normalize_event`` (``DCIMChangeEvent.from_dict``).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from nv_config_manager_dcim import RenderEventRequest

if TYPE_CHECKING:
    from nv_config_manager_dcim import DCIMChangeEvent, DCIMClient, DCIMRenderEventRegistry

logger = logging.getLogger(__name__)


def _commit_message(event: DCIMChangeEvent) -> str:
    return f"Rendered after {event.object_type} {event.operation}"


def _record_device_id(event: DCIMChangeEvent):
    device = (event.record or {}).get("device")
    return device.get("id") if isinstance(device, dict) else device


def _requests(event: DCIMChangeEvent, device_id) -> tuple[RenderEventRequest, ...]:
    if not device_id:
        return ()
    return (RenderEventRequest(device_id=str(device_id), commit_message=_commit_message(event)),)


async def _device_changed(event: DCIMChangeEvent, client: DCIMClient):
    return _requests(event, event.object_id)


async def _interface_changed(event: DCIMChangeEvent, client: DCIMClient):
    return _requests(event, _record_device_id(event))


async def _bgp_session_changed(event: DCIMChangeEvent, client: DCIMClient):
    return _requests(event, _record_device_id(event))


async def _overlay_assignment_changed(event: DCIMChangeEvent, client: DCIMClient):
    record = event.record or {}
    assigned_type = record.get("assigned_object_type")
    assigned_id = record.get("assigned_object_id")
    if not assigned_id:
        return ()
    if assigned_type == "dcim.device":
        return _requests(event, assigned_id)
    if assigned_type == "dcim.interface":
        try:
            interface = await client.rest_request("GET", f"dcim/interfaces/{assigned_id}/")
        except Exception:  # noqa: BLE001 - fan-out resolution is best-effort
            logger.exception("Failed to resolve interface %s for render fan-out", assigned_id)
            return ()
        device = (interface or {}).get("device") or {}
        return _requests(event, device.get("id") if isinstance(device, dict) else device)
    return ()


# object_type -> handler. Object types whose fan-out needs cross-object queries
# (prefix/vrf/ipaddress/cable/configcontext) are intentionally not registered yet.
_HANDLERS = {
    "dcim.device": _device_changed,
    "dcim.interface": _interface_changed,
    "netbox_nv_config_manager.overlayassignment": _overlay_assignment_changed,
    "netbox_bgp.bgpsession": _bgp_session_changed,
}


def register_render_event_handlers(registry: DCIMRenderEventRegistry) -> None:
    """Register NetBox render-event handlers with the service dispatcher."""
    for object_type, handler in _HANDLERS.items():
        registry.register_render_event_handler(object_type, handler)
