# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Change-event tests: envelope normalization, handler registration, and the
affected-device fan-out for each native event type the provider registers.
"""

from __future__ import annotations

import pytest
from nv_config_manager_dcim import (
    DCIMChangeEvent,
    DCIMConnectivityError,
    DCIMError,
    DCIMInvalidDataError,
)

from nv_config_manager_dcim_netbox.events import (
    _HANDLERS,
    NetBoxUnavailableError,
    _bgp_session_changed,
    _device_changed,
    _interface_changed,
    _overlay_assignment_changed,
    _parent_device_changed,
    register_render_event_handlers,
)
from nv_config_manager_dcim_netbox.provider import NetBoxProvider

# Area marker for `make <area>` / `pytest -m <area>`.
pytestmark = pytest.mark.events
_OVERLAY_TYPE = "netbox_nv_config_manager.overlayassignment"


async def _dispatch(event, client):
    """Route an event through the registration, not a directly-imported handler.

    The cross-object handlers are built by binding a resolver in _HANDLERS, so
    going through the table is what pins each object_type to the right resolver.
    """
    return await _HANDLERS[event.object_type](event, client)


class _FanoutClient:
    """Stubbed transport for cross-object fan-out resolution."""

    def __init__(self, rest_get=None, rest_list=None):
        self._get = rest_get or {}
        self._list = rest_list or {}

    async def rest_request(self, method, path, **kwargs):
        return self._get.get(path)

    async def rest_list(self, path, params=None, **kwargs):
        return self._list.get(path, [])


class _ParamCapturingClient(_FanoutClient):
    """``_FanoutClient`` that also records the params of the last ``rest_list``."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.params = None

    async def rest_list(self, path, params=None, **kwargs):
        self.params = params
        return await super().rest_list(path, params, **kwargs)


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
        "dcim.macaddress",
        "dcim.cable",
        "extras.configcontext",
        "ipam.vrf",
        "ipam.vlan",
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


async def test_overlay_assignment_interface_resolution_error_raises():
    """See test_cross_object_resolution_error_raises: a handler that returns
    normally is recorded as a processed event, so a failure must not."""

    class FakeClient:
        async def rest_request(self, *args, **kwargs):
            raise RuntimeError("boom")

    event = _event(
        _OVERLAY_TYPE, "1", {"assigned_object_type": "dcim.interface", "assigned_object_id": 12}
    )
    with pytest.raises(DCIMInvalidDataError):
        await _overlay_assignment_changed(event, FakeClient())


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
        assert [r.device_id for r in requests] == ["7"], object_type


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
    requests = await _dispatch(_event("ipam.vrf", "9"), client)
    assert sorted(r.device_id for r in requests) == ["3", "5"]  # deduplicated


async def test_vlan_change_fans_out_to_interface_devices_by_vlan_pk():
    client = _ParamCapturingClient(
        rest_list={
            "dcim/interfaces/": [
                {"device": {"id": 3}},
                {"device": {"id": 5}},
                {"device": {"id": 3}},
            ]
        }
    )
    requests = await _dispatch(_event("ipam.vlan", "9", {"id": 9, "vid": 900}), client)

    assert client.params == [("vlan_id", "9")]
    assert sorted(r.device_id for r in requests) == ["3", "5"]  # deduplicated


async def test_vlan_custom_field_change_routes_through_the_vlan_handler():
    event = DCIMChangeEvent.from_dict(
        {
            "provider": "netbox",
            "operation": "update",
            "object_type": "ipam.vlan",
            "object_id": "9",
            "timestamp": "2026-07-29T00:00:00Z",
            "record": {
                "id": 9,
                "vid": 900,
                "custom_fields": {"nvcm_helper_addresses": ["10.0.0.53"]},
            },
            "changed_fields": ["custom_fields.nvcm_helper_addresses"],
            "contract_version": "1.0",
        }
    )

    client = _FanoutClient(rest_list={"dcim/interfaces/": [{"device": {"id": 7}}]})

    requests = await _dispatch(event, client)

    assert [request.device_id for request in requests] == ["7"]


async def test_ip_address_change_fans_out_to_assigned_device():
    client = _FanoutClient(
        rest_get={
            "ipam/ip-addresses/4/": {
                "assigned_object_type": "dcim.interface",
                "assigned_object": {"device": {"id": 6}},
            }
        }
    )
    requests = await _dispatch(_event("ipam.ipaddress", "4"), client)
    assert [r.device_id for r in requests] == ["6"]


async def test_ip_address_not_on_interface_is_noop():
    client = _FanoutClient(
        rest_get={"ipam/ip-addresses/4/": {"assigned_object_type": "virtualization.vminterface"}}
    )
    assert await _dispatch(_event("ipam.ipaddress", "4"), client) == ()


async def test_cable_change_fans_out_to_both_terminations():
    client = _FanoutClient(
        rest_get={
            "dcim/cables/2/": {
                "a_terminations": [{"object": {"device": {"id": 1}}}],
                "b_terminations": [{"object": {"device": {"id": 2}}}],
            }
        }
    )
    requests = await _dispatch(_event("dcim.cable", "2"), client)
    assert sorted(r.device_id for r in requests) == ["1", "2"]


async def test_prefix_change_fans_out_by_scope():
    client = _FanoutClient(
        rest_get={"ipam/prefixes/8/": {"scope_type": "dcim.site", "scope_id": 3}},
        rest_list={"dcim/devices/": [{"id": 10}, {"id": 11}]},
    )
    requests = await _dispatch(_event("ipam.prefix", "8"), client)
    assert sorted(r.device_id for r in requests) == ["10", "11"]


async def test_prefix_without_scope_is_noop():
    client = _FanoutClient(rest_get={"ipam/prefixes/8/": {"scope_type": None, "scope_id": None}})
    assert await _dispatch(_event("ipam.prefix", "8"), client) == ()


class _RecordingFanoutClient(_FanoutClient):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.gets: list[str] = []
        self.lists: list[tuple[str, list]] = []

    async def rest_request(self, method, path, **kwargs):
        self.gets.append(path)
        return await super().rest_request(method, path, **kwargs)

    async def rest_list(self, path, params=None, **kwargs):
        self.lists.append((path, list(params or [])))
        return await super().rest_list(path, params, **kwargs)


# The scope lists as the plugin's event serializer (NetBox's ConfigContext REST
# serializer) emits them: nested briefs with id + slug.
_CONFIG_CONTEXT_RECORD = {
    "id": 5,
    "name": "leaf-bgp",
    "regions": [],
    "site_groups": [],
    "sites": [{"id": 3, "name": "DM-Akron", "slug": "dm-akron"}],
    "locations": [],
    "device_types": [],
    "roles": [{"id": 2, "name": "Leaf", "slug": "leaf"}],
    "platforms": [{"id": 4, "name": "Cumulus Linux", "slug": "cumulus-linux"}],
    "tenant_groups": [],
    "tenants": [],
    "tags": [{"id": 9, "name": "Fabric", "slug": "fabric"}],
}


async def test_config_context_change_builds_device_filters_from_the_record():
    client = _RecordingFanoutClient(rest_list={"dcim/devices/": [{"id": 20}]})
    requests = await _dispatch(_event("extras.configcontext", "5", _CONFIG_CONTEXT_RECORD), client)
    assert [r.device_id for r in requests] == ["20"]
    assert client.gets == []  # the record is the scope; nothing is read back
    assert client.lists == [
        (
            "dcim/devices/",
            [("site_id", "3"), ("role_id", "2"), ("platform_id", "4"), ("tag", "fabric")],
        )
    ]


async def test_config_context_delete_rerenders_the_scoped_devices():
    # Parity: the reference has no delete guard here. The record is the pre-delete
    # snapshot, and the devices the context applied to must render without it.
    client = _RecordingFanoutClient(rest_list={"dcim/devices/": [{"id": 20}, {"id": 21}]})
    event = _event("extras.configcontext", "5", _CONFIG_CONTEXT_RECORD, operation="delete")
    requests = await _dispatch(event, client)
    assert sorted(r.device_id for r in requests) == ["20", "21"]
    assert client.gets == []  # a GET after delete would 404


async def test_config_context_without_record_scope_is_read_back_unless_deleted():
    client = _RecordingFanoutClient(
        rest_get={
            "extras/config-contexts/5/": {"roles": [{"id": 2}], "tags": [{"slug": "fabric"}]}
        },
        rest_list={"dcim/devices/": [{"id": 20}]},
    )
    requests = await _dispatch(_event("extras.configcontext", "5", {"id": 5}), client)
    assert [r.device_id for r in requests] == ["20"]
    assert client.gets == ["extras/config-contexts/5/"]
    with pytest.raises(DCIMInvalidDataError):
        await _dispatch(_event("extras.configcontext", "5", {}, operation="delete"), client)


# --- dcim.macaddress (NetBox 4.2+) ---------------------------------------------


async def test_mac_address_change_fans_out_to_the_assigned_interface_device():
    record = {
        "mac_address": "AA:BB:CC:00:00:01",
        "assigned_object_type": "dcim.interface",
        "assigned_object_id": 11,
        "assigned_object": {"id": 11, "name": "swp1", "device": {"id": 7, "name": "leaf-01"}},
    }
    client = _RecordingFanoutClient()
    requests = await _dispatch(_event("dcim.macaddress", "3", record), client)
    assert [r.device_id for r in requests] == ["7"]
    assert client.gets == []


async def test_mac_address_delete_uses_the_record_and_resolves_the_interface():
    # The record's assigned_object may omit the device; the interface outlives the
    # MAC, so it is still resolvable on delete.
    record = {"assigned_object_type": "dcim.interface", "assigned_object_id": 11}
    client = _RecordingFanoutClient(rest_get={"dcim/interfaces/11/": {"device": {"id": 7}}})
    event = _event("dcim.macaddress", "3", record, operation="delete")
    assert [r.device_id for r in await _dispatch(event, client)] == ["7"]
    assert client.gets == ["dcim/interfaces/11/"]


async def test_mac_address_without_record_assignment_is_fetched():
    client = _RecordingFanoutClient(
        rest_get={
            "dcim/mac-addresses/3/": {
                "assigned_object_type": "dcim.interface",
                "assigned_object_id": 11,
                "assigned_object": {"device": {"id": 8}},
            }
        }
    )
    assert [r.device_id for r in await _dispatch(_event("dcim.macaddress", "3"), client)] == ["8"]
    assert client.gets == ["dcim/mac-addresses/3/"]


async def test_mac_address_not_on_an_interface_is_noop():
    for record in (
        {"assigned_object_type": None, "assigned_object_id": None},
        {"assigned_object_type": "virtualization.vminterface", "assigned_object_id": 4},
    ):
        assert await _dispatch(_event("dcim.macaddress", "3", record), _FanoutClient()) == ()


async def test_cross_object_delete_is_noop():
    # The object is gone on delete, so it can no longer be resolved.
    assert await _dispatch(_event("ipam.vrf", "9", operation="delete"), None) == ()


async def test_cross_object_resolution_error_raises():
    """A swallowed resolution failure is worse than a noisy one.

    The dispatcher counts a handler that returns normally as processed and
    acknowledges the message, so returning () here would file a lost render under
    EVENT_PROCESSED_COUNTER: the change never renders and the failure metric shows
    nothing wrong. The reference provider raises on the same paths.
    """

    class _Boom:
        async def rest_list(self, *args, **kwargs):
            raise RuntimeError("boom")

        async def rest_request(self, *args, **kwargs):
            raise RuntimeError("boom")

    with pytest.raises(DCIMInvalidDataError):
        await _dispatch(_event("ipam.vrf", "9"), _Boom())


class _Unreachable:
    def __init__(self, exc):
        self._exc = exc

    async def rest_list(self, *args, **kwargs):
        raise self._exc

    async def rest_request(self, *args, **kwargs):
        raise self._exc


@pytest.mark.parametrize(
    "exc",
    [DCIMConnectivityError("NetBox REST request failed"), TimeoutError("read timeout")],
)
async def test_transient_netbox_failures_escape_as_a_non_dcim_error(exc):
    """NVCM's dispatcher swallows every DCIMError (the event is acked as
    processed-and-failed); only a non-DCIM exception reaches the consumer and
    naks the message. A NetBox outage must therefore not surface as a DCIMError,
    or the change is lost."""
    client = _Unreachable(exc)
    for event in (
        _event("ipam.vrf", "9"),
        _event("extras.configcontext", "5", {"roles": [{"id": 2}]}),
        _event("dcim.macaddress", "3"),
        _event(
            _OVERLAY_TYPE, "1", {"assigned_object_type": "dcim.interface", "assigned_object_id": 12}
        ),
    ):
        with pytest.raises(NetBoxUnavailableError) as info:
            await _dispatch(event, client)
        assert not isinstance(info.value, DCIMError), event.object_type
        assert info.value.__cause__ is exc


async def test_deterministic_netbox_failures_stay_dcim_errors():
    # A 400 / GraphQL data error will fail the same way on redelivery, so it must
    # stay a DCIMError for the dispatcher to ack and count as failed.
    exc = DCIMInvalidDataError("NetBox rejected the request (400)")
    with pytest.raises(DCIMInvalidDataError) as info:
        await _dispatch(_event("ipam.vrf", "9"), _Unreachable(exc))
    assert info.value is exc


async def test_a_malformed_device_id_drops_the_fanout_instead_of_raising():
    """A device id that is not a NetBox PK cannot address a device. The device and
    parent-device handlers must drop it (like the best-effort cross-object
    handlers) rather than raise into the dispatcher."""
    assert await _device_changed(_event("dcim.device", "not-a-pk"), None) == ()
    event = _event("dcim.interface", "11", {"device": {"id": "not-a-pk"}})
    assert await _parent_device_changed(event, None) == ()


async def test_parent_device_handler_reads_a_bare_device_pk():
    # The record's `device` may be a bare PK rather than a nested object.
    requests = await _parent_device_changed(_event("dcim.interface", "11", {"device": 7}), None)
    assert [r.device_id for r in requests] == ["7"]


async def test_multi_device_fanout_dedupes_and_preserves_order():
    client = _FanoutClient(
        rest_get={"dcim/cables/3/": {"a_terminations": [], "b_terminations": []}}
    )
    # Resolver order is preserved and repeats collapse to one request each.
    from nv_config_manager_dcim_netbox.events import _requests_multi

    requests = _requests_multi(_event("dcim.cable", "3"), [9, 4, 9, None, 4])
    assert [r.device_id for r in requests] == ["9", "4"]
    assert await _dispatch(_event("dcim.cable", "3"), client) == ()
