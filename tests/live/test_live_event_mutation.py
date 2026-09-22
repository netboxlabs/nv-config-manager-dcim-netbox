# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Mutate real objects and assert the change events NetBox emits.

NETBOX_COLLAB.md's validation checklist asks for event mutation tests, run only
against a disposable environment. This is the change->envelope half; render
dispatch needs NVCM's consumers and stays manual.

Why a real mutation and not a constructed event: `test_events.py` in the plugin
feeds `to_envelope()` a plain dict, which cannot reproduce how NetBox actually
delivers one. Its `EventContext` serializes `data` lazily in `__getitem__`, so
reading it with `dict.get()` returned None and every change event was published
with a null record -- and NVCM's dispatcher drops those before it looks up a
handler, silently. Nothing short of a real write surfaces that.

The envelope is read back from the `log` publisher's output
(`scripts/live-netbox.sh` points that logger at events.log), so no NATS broker
is involved.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

import pytest

pytestmark = pytest.mark.live

SERVER = os.environ.get("NVCM_LIVE_SERVER")
TOKEN = os.environ.get("NVCM_LIVE_TOKEN")
EVENT_LOG = os.environ.get("NVCM_LIVE_EVENT_LOG")

_MARKER = "nvcm-change "


@pytest.fixture(scope="module")
def event_log():
    if not EVENT_LOG:
        pytest.skip("set NVCM_LIVE_EVENT_LOG (scripts/live-netbox.sh prints it)")
    return EVENT_LOG


def _api(method, path, payload=None):
    request = urllib.request.Request(
        f"{SERVER}/api/{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Token {TOKEN}", "Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        assert response.status in (200, 201, 204), response.status
        return json.load(response) if response.status != 204 else None


def _patch_device(device_id, payload):
    return _api("PATCH", f"dcim/devices/{device_id}/", payload)


def _envelopes(path, since=0):
    """Envelopes logged after byte offset ``since``, newest last."""
    if not os.path.exists(path):
        return []
    with open(path) as handle:
        handle.seek(since)
        return [json.loads(line.split(_MARKER, 1)[1]) for line in handle if _MARKER in line]


def _wait_for_envelope(path, since, match, timeout=30):
    """Publishing happens after the response is sent, so poll rather than read once."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for envelope in _envelopes(path, since):
            if match(envelope):
                return envelope
        time.sleep(0.5)
    return None


def _offset(path):
    return os.path.getsize(path) if os.path.exists(path) else 0


def test_updating_a_device_emits_a_complete_envelope(event_log, trial_devices):
    device = trial_devices["tan-leaf-03"]
    since = _offset(event_log)

    _patch_device(device.id, {"comments": "event mutation test"})

    envelope = _wait_for_envelope(
        event_log,
        since,
        lambda e: e.get("object_type") == "dcim.device" and e.get("object_id") == device.id,
    )
    assert envelope is not None, "no envelope logged for the device update"

    assert envelope["provider"] == "netbox"
    assert envelope["operation"] == "update"
    assert envelope["contract_version"]
    assert envelope["timestamp"]
    # 'system' would mean the authenticated user never reached the envelope.
    assert envelope["actor"] != "system"


def test_the_record_survives_netboxs_lazy_serialization(event_log, trial_devices):
    """The regression that made every event a no-op: record must not be null."""
    device = trial_devices["tan-leaf-04"]
    since = _offset(event_log)

    _patch_device(device.id, {"comments": "record materialization check"})

    envelope = _wait_for_envelope(
        event_log,
        since,
        lambda e: e.get("object_type") == "dcim.device" and e.get("object_id") == device.id,
    )
    assert envelope is not None

    assert envelope["record"] is not None, (
        "record is null; NVCM's dispatcher discards these without logging"
    )
    assert envelope["record"].get("name") == device.name


def test_changed_fields_names_what_actually_changed(event_log, trial_devices):
    device = trial_devices["tan-leaf-05"]
    since = _offset(event_log)

    _patch_device(device.id, {"comments": f"changed-fields check {time.time()}"})

    envelope = _wait_for_envelope(
        event_log,
        since,
        lambda e: (
            e.get("object_type") == "dcim.device"
            and e.get("object_id") == device.id
            and "comments" in (e.get("changed_fields") or [])
        ),
    )
    assert envelope is not None, "no envelope reported `comments` as changed"


# -- deletes -----------------------------------------------------------------------
# The provider skips a VLAN / VRF / cable delete because NetBox re-saves the
# interfaces that carried it, and resolves an IP or prefix delete from the record
# because nothing else names the device. Both halves are inferred from NetBox
# source (core.signals.handle_deleted_object, the REST serializers); these pin
# them against a real NetBox.


@pytest.fixture
def scratch_interface(trial_devices):
    """A throwaway interface on a trial device, deleted after the test."""
    device = trial_devices["tan-leaf-03"]
    interface = _api(
        "POST",
        "dcim/interfaces/",
        {"device": int(device.id), "name": f"nvcm-live-{int(time.time())}", "type": "virtual"},
    )
    try:
        yield device, interface
    finally:
        _api("DELETE", f"dcim/interfaces/{interface['id']}/")


def test_deleting_a_vlan_emits_an_interface_update_naming_the_device(event_log, scratch_interface):
    device, interface = scratch_interface
    vlan = _api("POST", "ipam/vlans/", {"name": f"nvcm-live-{interface['id']}", "vid": 3999})
    _api(
        "PATCH",
        f"dcim/interfaces/{interface['id']}/",
        {"mode": "access", "untagged_vlan": vlan["id"]},
    )
    since = _offset(event_log)

    _api("DELETE", f"ipam/vlans/{vlan['id']}/")

    deleted = _wait_for_envelope(
        event_log,
        since,
        lambda e: e.get("object_type") == "ipam.vlan" and e.get("object_id") == str(vlan["id"]),
    )
    assert deleted is not None, "no envelope logged for the VLAN delete"
    assert deleted["operation"] == "delete"

    # NetBox clears untagged_vlan by re-saving the interface, so the same batch
    # carries the interface update the provider re-renders the device from.
    updated = _wait_for_envelope(
        event_log,
        since,
        lambda e: (
            e.get("object_type") == "dcim.interface"
            and e.get("object_id") == str(interface["id"])
            and e.get("operation") == "update"
        ),
    )
    assert updated is not None, "VLAN delete did not emit the interface update"
    assert (updated["record"].get("device") or {}).get("id") == int(device.id)
    assert updated["record"].get("untagged_vlan") is None


def test_deleting_an_ip_address_leaves_the_interface_device_in_the_record(
    event_log, scratch_interface
):
    device, interface = scratch_interface
    ip = _api(
        "POST",
        "ipam/ip-addresses/",
        {
            "address": "198.51.100.77/32",
            "assigned_object_type": "dcim.interface",
            "assigned_object_id": interface["id"],
        },
    )
    since = _offset(event_log)

    _api("DELETE", f"ipam/ip-addresses/{ip['id']}/")

    envelope = _wait_for_envelope(
        event_log,
        since,
        lambda e: e.get("object_type") == "ipam.ipaddress" and e.get("object_id") == str(ip["id"]),
    )
    assert envelope is not None, "no envelope logged for the IP delete"
    # Nothing else names the device (the assignment is a GenericForeignKey), so
    # _ip_address_deleted reads it from here.
    record = envelope["record"]
    assert record["assigned_object_type"] == "dcim.interface"
    assert record["assigned_object"]["device"]["id"] == int(device.id)


def test_deleting_a_prefix_leaves_its_scope_in_the_record(event_log, trial_devices):
    device = _api("GET", f"dcim/devices/{trial_devices['tan-leaf-03'].id}/")
    prefix = _api(
        "POST",
        "ipam/prefixes/",
        {
            "prefix": "198.51.100.128/29",
            "scope_type": "dcim.site",
            "scope_id": device["site"]["id"],
        },
    )
    since = _offset(event_log)

    _api("DELETE", f"ipam/prefixes/{prefix['id']}/")

    envelope = _wait_for_envelope(
        event_log,
        since,
        lambda e: e.get("object_type") == "ipam.prefix" and e.get("object_id") == str(prefix["id"]),
    )
    assert envelope is not None, "no envelope logged for the prefix delete"
    # _prefix_deleted fans out by these two fields.
    assert envelope["record"]["scope_type"] == "dcim.site"
    assert envelope["record"]["scope_id"] == device["site"]["id"]
