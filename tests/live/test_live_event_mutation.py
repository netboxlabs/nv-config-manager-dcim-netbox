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


def _patch_device(device_id, payload):
    request = urllib.request.Request(
        f"{SERVER}/api/dcim/devices/{device_id}/",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Token {TOKEN}", "Content-Type": "application/json"},
        method="PATCH",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        assert response.status == 200, response.status
        return json.load(response)


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
