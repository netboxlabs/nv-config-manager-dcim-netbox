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

import asyncio
import logging
from functools import partial
from typing import TYPE_CHECKING

from nv_config_manager_dcim import RenderEventRequest

from nv_config_manager_dcim_netbox._mappers import fk_id as _fk_id
from nv_config_manager_dcim_netbox.client import is_netbox_pk
from nv_config_manager_dcim_netbox.fabric_common import (
    BGP_SESSIONS_PATH,
    DEVICES_PATH,
    INTERFACES_PATH,
)

if TYPE_CHECKING:
    from nv_config_manager_dcim import DCIMChangeEvent, DCIMClient, DCIMRenderEventRegistry

logger = logging.getLogger(__name__)


def _commit_message(event: DCIMChangeEvent) -> str:
    return f"Rendered after {event.object_type} {event.operation}"


def _record_device_id(event: DCIMChangeEvent):
    return _fk_id((event.record or {}).get("device"))


def _valid_device_id(device_id) -> str | None:
    """Return a NetBox device id as a string, or ``None`` if it is not a NetBox PK.

    A malformed id in an event envelope must drop that fan-out, not raise into the
    dispatcher -- the cross-object handlers are already best-effort and the
    device/parent handlers should behave the same way.
    """
    if not is_netbox_pk(device_id):
        logger.warning("Ignoring unusable NetBox device id in change event: %r", device_id)
        return None
    return str(device_id)


def _requests(event: DCIMChangeEvent, device_id) -> tuple[RenderEventRequest, ...]:
    if not device_id:
        return ()
    return _requests_multi(event, [device_id])


async def _device_changed(event: DCIMChangeEvent, client: DCIMClient):
    return _requests(event, event.object_id)


def _requests_multi(event: DCIMChangeEvent, device_ids) -> tuple[RenderEventRequest, ...]:
    """One deduplicated render request per affected device (input order preserved)."""
    message = _commit_message(event)
    resolved: dict[str, str | None] = {}
    for device_id in device_ids:
        if device_id and str(device_id) not in resolved:
            resolved[str(device_id)] = _valid_device_id(device_id)
    return tuple(
        RenderEventRequest(device_id=valid_id, commit_message=message)
        for valid_id in resolved.values()
        if valid_id is not None
    )


async def _parent_device_changed(event: DCIMChangeEvent, client: DCIMClient):
    """Fan out to the device referenced by the changed record -- interface, front/
    rear port, BGP session, or device-config-status (all carry a ``device``)."""
    return _requests(event, _record_device_id(event))


# Back-compat names used directly by tests.
_interface_changed = _parent_device_changed
_bgp_session_changed = _parent_device_changed


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


# -- cross-object fan-out --------------------------------------------------------
# The changed object is not a device, so the affected devices are resolved with a
# NetBox query. Skipped on delete, and best-effort: a failed resolution logs and
# drops rather than stalling the dispatcher. The render-enabled gate is applied
# downstream, so these return candidates unfiltered.

# ConfigContext scope dimension -> device-list filter. AND across dimensions, OR
# within one -- exactly how NetBox evaluates ConfigContext applicability.
_CONFIG_CONTEXT_FILTERS = {
    "regions": "region_id",
    "site_groups": "site_group_id",
    "sites": "site_id",
    "locations": "location_id",
    "device_types": "device_type_id",
    "roles": "role_id",
    "platforms": "platform_id",
    "tenant_groups": "tenant_group_id",
    "tenants": "tenant_id",
    "clusters": "cluster_id",
    "tags": "tag",
}

# Prefix scope content-type -> device-list filter (NetBox 4.x prefix.scope GFK).
_PREFIX_SCOPE_FILTERS = {
    "dcim.region": "region_id",
    "dcim.sitegroup": "site_group_id",
    "dcim.site": "site_id",
    "dcim.location": "location_id",
}


async def _cross(event: DCIMChangeEvent, client: DCIMClient, resolver):
    if event.operation == "delete":
        return ()
    try:
        device_ids = await resolver(event, client)
    except Exception:  # noqa: BLE001 - fan-out resolution is best-effort
        logger.exception("Render fan-out failed for %s %s", event.object_type, event.object_id)
        return ()
    return _requests_multi(event, device_ids)


async def _vrf_devices(event, client):
    interfaces = await client.rest_list(INTERFACES_PATH, [("vrf_id", str(event.object_id))])
    return {_fk_id(i.get("device")) for i in interfaces}


async def _ip_address_devices(event, client):
    ip = await client.rest_request("GET", f"ipam/ip-addresses/{event.object_id}/")
    if not ip or ip.get("assigned_object_type") != "dcim.interface":
        return ()
    return {_fk_id((ip.get("assigned_object") or {}).get("device"))}


async def _cable_devices(event, client):
    cable = await client.rest_request("GET", f"dcim/cables/{event.object_id}/")
    device_ids = set()
    for side in ("a_terminations", "b_terminations"):
        for termination in (cable or {}).get(side) or []:
            device_ids.add(_fk_id((termination.get("object") or {}).get("device")))
    return device_ids


async def _prefix_devices(event, client):
    prefix = await client.rest_request("GET", f"ipam/prefixes/{event.object_id}/")
    param = _PREFIX_SCOPE_FILTERS.get((prefix or {}).get("scope_type"))
    scope_id = (prefix or {}).get("scope_id")
    if not param or not scope_id:
        return ()
    devices = await client.rest_list(DEVICES_PATH, [(param, str(scope_id))])
    return {device.get("id") for device in devices}


async def _bgp_asn_devices(event, client):
    # An ASN change arrives as ``ipam.asn`` and must re-render every device whose
    # BGP session references it. local_as_id and remote_as_id AND when combined, so
    # query each separately and union the owners.
    asn_id = str(event.object_id)
    local, remote = await asyncio.gather(
        client.rest_list(BGP_SESSIONS_PATH, [("local_as_id", asn_id)]),
        client.rest_list(BGP_SESSIONS_PATH, [("remote_as_id", asn_id)]),
    )
    return {_fk_id(session.get("device")) for session in (*local, *remote)}


async def _bgp_peer_group_devices(event, client):
    # A shared netbox-bgp ``BGPPeerGroup`` is referenced by many sessions; editing
    # it must re-render each referencing session's device. Resolve peer group ->
    # sessions (peer_group=<pk>) -> devices.
    sessions = await client.rest_list(BGP_SESSIONS_PATH, [("peer_group", str(event.object_id))])
    return {_fk_id(session.get("device")) for session in sessions}


async def _config_context_devices(event, client):
    context = await client.rest_request("GET", f"extras/config-contexts/{event.object_id}/")
    filters = []
    for field, param in _CONFIG_CONTEXT_FILTERS.items():
        for item in (context or {}).get(field) or []:
            value = item.get("slug") if param == "tag" else item.get("id")
            if value is not None:
                filters.append((param, str(value)))
    if not filters:
        # A context with no scope assignments genuinely applies to every device, so
        # this correctly fans out fleet-wide -- but that is an unbounded render
        # burst, so make it visible rather than silent.
        logger.warning(
            "Config context %s has no scope assignments; fanning out to every device",
            event.object_id,
        )
    devices = await client.rest_list(DEVICES_PATH, filters)
    return {device.get("id") for device in devices}


def _cross_handler(resolver):
    """Bind a cross-object resolver into a handler.

    One _HANDLERS entry per object type instead of an entry plus a wrapper, so a
    wrapper naming the wrong resolver cannot silently fan out to the wrong devices.
    """
    return partial(_cross, resolver=resolver)


# object_type -> handler. Mirrors the reference DCIM provider's coverage,
# mapped to NetBox: rear/front ports + device-config-status fan out to their
# device; cable/vrf/prefix/ipaddress/configcontext resolve cross-object.
_HANDLERS = {
    "dcim.device": _device_changed,
    "dcim.interface": _parent_device_changed,
    "dcim.rearport": _parent_device_changed,
    "dcim.frontport": _parent_device_changed,
    "dcim.cable": _cross_handler(_cable_devices),
    "extras.configcontext": _cross_handler(_config_context_devices),
    "ipam.vrf": _cross_handler(_vrf_devices),
    "ipam.prefix": _cross_handler(_prefix_devices),
    "ipam.ipaddress": _cross_handler(_ip_address_devices),
    # A netbox-bgp session carries only a single ``device`` FK (the local peer);
    # it has no FK to the remote peer's device -- unlike the reference provider's
    # two-ended peering -- so editing a session re-renders the local device only.
    "netbox_bgp.bgpsession": _parent_device_changed,
    # Shared BGP objects fan out to every dependent device: an ASN is core
    # ipam.asn (referenced by session local_as/remote_as); a peer group is
    # netbox_bgp.bgppeergroup. Mirrors the reference provider's ASN / peer-group
    # fan-out.
    "ipam.asn": _cross_handler(_bgp_asn_devices),
    "netbox_bgp.bgppeergroup": _cross_handler(_bgp_peer_group_devices),
    "netbox_nv_config_manager.overlayassignment": _overlay_assignment_changed,
    "netbox_nv_config_manager.deviceconfigstatus": _parent_device_changed,
}


def register_render_event_handlers(registry: DCIMRenderEventRegistry) -> None:
    """Register NetBox render-event handlers with the service dispatcher."""
    for object_type, handler in _HANDLERS.items():
        registry.register_render_event_handler(object_type, handler)
