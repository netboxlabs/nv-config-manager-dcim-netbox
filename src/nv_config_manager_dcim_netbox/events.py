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

import aiohttp
from nv_config_manager_dcim import (
    DCIMConnectivityError,
    DCIMError,
    DCIMInvalidDataError,
    RenderEventRequest,
)

from nv_config_manager_dcim_netbox._mappers import fk_id as _fk_id
from nv_config_manager_dcim_netbox.client import is_netbox_pk
from nv_config_manager_dcim_netbox.fabric_common import (
    BGP_ASN_FIELD,
    BGP_SESSIONS_PATH,
    DEVICES_PATH,
    INTERFACES_PATH,
)

if TYPE_CHECKING:
    from nv_config_manager_dcim import DCIMChangeEvent, DCIMClient, DCIMRenderEventRegistry

logger = logging.getLogger(__name__)


class NetBoxUnavailableError(Exception):
    """NetBox could not be reached; deliberately *not* a DCIMError so the render
    consumer naks the message for redelivery instead of acknowledging it."""


# Failures that a retry can fix. ``client`` maps aiohttp/timeouts to
# DCIMConnectivityError already; the raw forms are listed in case one escapes.
_TRANSIENT_ERRORS = (DCIMConnectivityError, TimeoutError, aiohttp.ClientConnectionError)


async def _resolve(event: DCIMChangeEvent, what: str, awaitable):
    """Await a NetBox lookup for ``event``, classifying its failures.

    A transient failure (NetBox down, 5xx, timeout) is re-raised as
    ``NetBoxUnavailableError``: the dispatcher swallows every DCIMError as a
    processed-and-failed event, and only a non-DCIM exception makes the consumer
    nak so the change is redelivered once NetBox is back. Any other DCIMError
    (400, GraphQL errors, not found) is deterministic and propagates as-is;
    anything else is wrapped in DCIMInvalidDataError rather than left to escape,
    which would redeliver a deterministic failure forever.
    """
    try:
        return await awaitable
    except _TRANSIENT_ERRORS as exc:
        raise NetBoxUnavailableError(
            f"NetBox unavailable while resolving {what} for {event.object_type} "
            f"{event.object_id}: {exc}"
        ) from exc
    except DCIMError:
        raise
    except Exception as exc:  # noqa: BLE001 - external client
        raise DCIMInvalidDataError(
            f"Render fan-out failed for {event.object_type} {event.object_id}: {exc}"
        ) from exc


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


async def _interface_device_id(event: DCIMChangeEvent, client: DCIMClient, interface_id):
    """Device id of a NetBox interface, looked up by pk."""
    interface = await _resolve(
        event,
        f"interface {interface_id}",
        client.rest_request("GET", f"dcim/interfaces/{interface_id}/"),
    )
    return _fk_id((interface or {}).get("device"))


async def _overlay_assignment_changed(event: DCIMChangeEvent, client: DCIMClient):
    record = event.record or {}
    assigned_type = record.get("assigned_object_type")
    assigned_id = record.get("assigned_object_id")
    if not assigned_id:
        return ()
    if assigned_type == "dcim.device":
        return _requests(event, assigned_id)
    if assigned_type == "dcim.interface":
        return _requests(event, await _interface_device_id(event, client, assigned_id))
    return ()


async def _mac_address_changed(event: DCIMChangeEvent, client: DCIMClient):
    """Fan out a ``dcim.macaddress`` change (NetBox 4.2+) to the interface's device.

    The record carries the GFK (``assigned_object_type`` / ``assigned_object_id``,
    with the assigned interface's brief ``device`` under ``assigned_object``). A
    record without it is fetched -- except on delete, where the row is gone and
    the pre-delete snapshot is all there is.
    """
    record = event.record or {}
    if "assigned_object_type" not in record and event.operation != "delete":
        record = (
            await _resolve(
                event,
                "MAC address",
                client.rest_request("GET", f"dcim/mac-addresses/{event.object_id}/"),
            )
            or {}
        )
    if record.get("assigned_object_type") != "dcim.interface":
        return ()
    assigned_id = record.get("assigned_object_id")
    if not assigned_id:
        return ()
    device_id = _fk_id((record.get("assigned_object") or {}).get("device"))
    if device_id is None:
        # The interface outlives the MAC on delete, so this is safe on every path.
        device_id = await _interface_device_id(event, client, assigned_id)
    return _requests(event, device_id)


# -- cross-object fan-out --------------------------------------------------------
# The changed object is not a device, so the affected devices are resolved with a
# NetBox query. On delete the object can no longer be read back, so the devices
# are resolved from the event record instead -- the plugin serializes the object
# before the row goes, and for the FKs NetBox nulls on delete (VLAN, VRF, BGP peer
# group) it adds the device ids it was on -- or the delete is skipped where the
# record cannot name them. The render-enabled gate is applied downstream, so
# these return candidates unfiltered; ``_recorded_devices`` is the one exception.

# Record field the plugin fills at pre_delete with the ids of the devices a
# deleted VLAN / VRF / BGP peer group was on (``events_pipeline
# .AFFECTED_DEVICES_FIELD`` in netbox-nv-config-manager).
AFFECTED_DEVICES_FIELD = "nvcm_affected_device_ids"

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


async def _cross(event: DCIMChangeEvent, client: DCIMClient, resolver, on_delete=None):
    """Resolve the devices a non-device change affects, and fail loudly if it cannot.

    A failed resolution is *not* swallowed. The dispatcher counts a handler that
    returns normally as a processed event and acknowledges it, so catching here
    would file a lost render under EVENT_PROCESSED_COUNTER -- the change is gone
    and the failure metric says nothing happened. A deterministic failure raises
    a DCIMError (EVENT_FAILED_COUNTER, acked), as the reference provider does; a
    transient one raises NetBoxUnavailableError so the message is redelivered.
    See ``_resolve``.

    ``on_delete`` resolves a delete from the pre-delete event record; without one
    the delete is skipped, as the object can no longer be read back.
    """
    if event.operation == "delete":
        if on_delete is None:
            return ()
        resolver = on_delete
    device_ids = await _resolve(event, "affected devices", resolver(event, client))
    return _requests_multi(event, device_ids)


async def _vrf_devices(event, client):
    interfaces = await client.rest_list(INTERFACES_PATH, [("vrf_id", str(event.object_id))])
    return {_fk_id(i.get("device")) for i in interfaces}


async def _vlan_devices(event, client):
    # ``vlan_id`` is the VLAN PK; ``vlan`` would match by VID, which is not unique.
    interfaces = await client.rest_list(INTERFACES_PATH, [("vlan_id", str(event.object_id))])
    return {_fk_id(i.get("device")) for i in interfaces}


async def _recorded_devices(event, client):
    """Delete fan-out for a VLAN, VRF or BGP peer group.

    NetBox lets these be deleted while interfaces / sessions still carry them and
    nulls the FKs, so after the delete nothing names the devices; the plugin
    records them at pre_delete into ``AFFECTED_DEVICES_FIELD``. A record without
    the field (a plugin predating it) falls back to every render-enabled device:
    a missed render never reaches the network until an unrelated change comes
    along, while a render with no diff is a no-op. That fallback is the one path
    here that pre-filters to render-enabled -- as a bound on a fleet-wide burst,
    not as policy; the downstream gate still applies.
    """
    device_ids = (event.record or {}).get(AFFECTED_DEVICES_FIELD)
    if isinstance(device_ids, list):
        return device_ids
    logger.warning(
        "%s %s delete event carries no %s (plugin too old?); fanning out to every "
        "render-enabled device",
        event.object_type,
        event.object_id,
        AFFECTED_DEVICES_FIELD,
    )
    return await client.get_render_enabled_device_ids(None)


def _ip_interface_device(ip):
    if not ip or ip.get("assigned_object_type") != "dcim.interface":
        return ()
    return {_fk_id((ip.get("assigned_object") or {}).get("device"))}


async def _ip_address_devices(event, client):
    return _ip_interface_device(
        await client.rest_request("GET", f"ipam/ip-addresses/{event.object_id}/")
    )


async def _ip_address_deleted(event, client):
    return _ip_interface_device(event.record)


def _cable_termination_devices(cable):
    device_ids = set()
    for side in ("a_terminations", "b_terminations"):
        for termination in (cable or {}).get(side) or []:
            device_ids.add(_fk_id((termination.get("object") or {}).get("device")))
    return device_ids


async def _cable_devices(event, client):
    return _cable_termination_devices(
        await client.rest_request("GET", f"dcim/cables/{event.object_id}/")
    )


async def _cable_deleted(event, client):
    return _cable_termination_devices(event.record)


async def _prefix_scope_devices(prefix, client):
    param = _PREFIX_SCOPE_FILTERS.get((prefix or {}).get("scope_type"))
    scope_id = (prefix or {}).get("scope_id")
    if not param or not scope_id:
        return ()
    devices = await client.rest_list(DEVICES_PATH, [(param, str(scope_id))])
    return {device.get("id") for device in devices}


async def _prefix_devices(event, client):
    return await _prefix_scope_devices(
        await client.rest_request("GET", f"ipam/prefixes/{event.object_id}/"), client
    )


async def _prefix_deleted(event, client):
    return await _prefix_scope_devices(event.record, client)


async def _bgp_asn_devices(event, client):
    # An ASN change arrives as ``ipam.asn`` and must re-render every device using
    # it. local_as_id and remote_as_id AND when combined, so query each separately
    # and union the owners.
    #
    # Sessions alone are not enough: they only cover devices that peer with
    # someone, so a spine holding an ASN and no sessions would never re-render.
    # The device's own ASN lives in a custom field for exactly this reason -- it
    # is also in config context, which is computed and cannot be filtered.
    asn_id = str(event.object_id)
    local, remote, owners = await asyncio.gather(
        client.rest_list(BGP_SESSIONS_PATH, [("local_as_id", asn_id)]),
        client.rest_list(BGP_SESSIONS_PATH, [("remote_as_id", asn_id)]),
        client.rest_list(DEVICES_PATH, [(f"cf_{BGP_ASN_FIELD}", asn_id)]),
    )
    # NetBox drops an unknown query param rather than rejecting it, so before the
    # bootstrap creates the field this filter matches the whole fabric. Re-checking
    # the value degrades that to the session-only answer instead of a fleet-wide
    # render burst.
    return {_fk_id(session.get("device")) for session in (*local, *remote)} | {
        device.get("id")
        for device in owners
        if str(_fk_id((device.get("custom_fields") or {}).get(BGP_ASN_FIELD))) == asn_id
    }


async def _bgp_peer_group_devices(event, client):
    # A shared netbox-bgp ``BGPPeerGroup`` is referenced by many sessions; editing
    # it must re-render each referencing session's device. Resolve peer group ->
    # sessions (peer_group=<pk>) -> devices.
    sessions = await client.rest_list(BGP_SESSIONS_PATH, [("peer_group", str(event.object_id))])
    return {_fk_id(session.get("device")) for session in sessions}


def _config_context_filters(context: dict) -> list[tuple[str, str]]:
    filters = []
    for field, param in _CONFIG_CONTEXT_FILTERS.items():
        for item in context.get(field) or []:
            if not isinstance(item, dict):
                continue
            value = item.get("slug") if param == "tag" else item.get("id")
            if value is not None:
                filters.append((param, str(value)))
    return filters


async def _config_context_changed(event: DCIMChangeEvent, client: DCIMClient):
    """Fan out an ``extras.configcontext`` change to every device in its scope.

    Not routed through ``_cross``: the scope lives on the event record (the
    plugin serializes the context itself, and on delete that is the pre-delete
    snapshot), so a deleted context can still re-render the devices it applied
    to -- as the reference provider does. The record is read back from NetBox
    only when it carries no scope at all and the context still exists.
    """
    record = event.record or {}
    context = record if any(field in record for field in _CONFIG_CONTEXT_FILTERS) else None
    if context is None:
        if event.operation == "delete":
            raise DCIMInvalidDataError(
                f"Config context {event.object_id} delete event carries no scope; "
                "cannot resolve the devices it applied to"
            )
        context = (
            await _resolve(
                event,
                "config context",
                client.rest_request("GET", f"extras/config-contexts/{event.object_id}/"),
            )
            or {}
        )
    filters = _config_context_filters(context)
    if not filters:
        # A context with no scope assignments genuinely applies to every device, so
        # this correctly fans out fleet-wide -- but that is an unbounded render
        # burst, so make it visible rather than silent.
        logger.warning(
            "Config context %s has no scope assignments; fanning out to every device",
            event.object_id,
        )
    devices = await _resolve(event, "scoped devices", client.rest_list(DEVICES_PATH, filters))
    return _requests_multi(event, (device.get("id") for device in devices))


def _cross_handler(resolver, on_delete=None):
    """Bind a cross-object resolver (and optional delete resolver) into a handler.

    One _HANDLERS entry per object type instead of an entry plus a wrapper, so a
    wrapper naming the wrong resolver cannot silently fan out to the wrong devices.
    """
    return partial(_cross, resolver=resolver, on_delete=on_delete)


# object_type -> handler. Mirrors the reference DCIM provider's coverage,
# mapped to NetBox: rear/front ports + device-config-status fan out to their
# device; cable/vrf/prefix/ipaddress/configcontext resolve cross-object.
_HANDLERS = {
    "dcim.device": _device_changed,
    "dcim.interface": _parent_device_changed,
    "dcim.rearport": _parent_device_changed,
    "dcim.frontport": _parent_device_changed,
    # NetBox 4.2+ holds MACs on a MACAddress row, so editing one is not an
    # interface change; it fans out to the assigned interface's device.
    "dcim.macaddress": _mac_address_changed,
    "dcim.cable": _cross_handler(_cable_devices, on_delete=_cable_deleted),
    "extras.configcontext": _config_context_changed,
    "ipam.vrf": _cross_handler(_vrf_devices, on_delete=_recorded_devices),
    "ipam.vlan": _cross_handler(_vlan_devices, on_delete=_recorded_devices),
    "ipam.prefix": _cross_handler(_prefix_devices, on_delete=_prefix_deleted),
    "ipam.ipaddress": _cross_handler(_ip_address_devices, on_delete=_ip_address_deleted),
    # A netbox-bgp session carries only a single ``device`` FK (the local peer);
    # it has no FK to the remote peer's device -- unlike the reference provider's
    # two-ended peering -- so editing a session re-renders the local device only.
    "netbox_bgp.bgpsession": _parent_device_changed,
    # Shared BGP objects fan out to every dependent device: an ASN is core
    # ipam.asn (referenced by session local_as/remote_as); a peer group is
    # netbox_bgp.bgppeergroup. Mirrors the reference provider's ASN / peer-group
    # fan-out.
    # An ASN in use cannot be deleted (netbox-bgp PROTECTs a session's
    # local_as/remote_as), so its delete has nothing to fan out to; a peer group
    # can (SET_NULL), so the plugin records the sessions' devices.
    "ipam.asn": _cross_handler(_bgp_asn_devices),
    "netbox_bgp.bgppeergroup": _cross_handler(_bgp_peer_group_devices, on_delete=_recorded_devices),
    "netbox_nv_config_manager.overlayassignment": _overlay_assignment_changed,
    "netbox_nv_config_manager.deviceconfigstatus": _parent_device_changed,
}


def register_render_event_handlers(registry: DCIMRenderEventRegistry) -> None:
    """Register NetBox render-event handlers with the service dispatcher."""
    for object_type, handler in _HANDLERS.items():
        registry.register_render_event_handler(object_type, handler)
