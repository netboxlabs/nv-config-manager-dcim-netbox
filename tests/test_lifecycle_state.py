# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Lifecycle-state tests (transport stubbed).

Per-device state is stored in first-class plugin models
(DeviceConfigStatus / IntendedConfig / BackupConfig) read/written over REST, not
device custom fields.
"""

from __future__ import annotations

from nv_config_manager_dcim import (
    ConfigurationBackupIntent,
    IntendedConfigurationUpdate,
    create_dcim_client,
)

_SETTINGS = {"server": "https://netbox.example", "token": "t"}
_STATUS = "plugins/nv-config-manager/device-config-statuses/"
_INTENDED = "plugins/nv-config-manager/intended-configs/"
_BACKUP = "plugins/nv-config-manager/backup-configs/"


def _client(list_map):
    client = create_dcim_client("netbox", _SETTINGS)
    calls = []

    async def fake_rest_list(path, params=None):
        params = dict(params or [])
        rows = list_map.get(path, [])
        if "device_id" in params:
            rows = [r for r in rows if str(r.get("device")) == str(params["device_id"])]
        if params.get("render_enabled") == "true":
            rows = [r for r in rows if r.get("render_enabled")]
        if params.get("backup_enabled") == "true":
            rows = [r for r in rows if r.get("backup_enabled")]
        return rows

    async def fake_rest_request(method, path, *, params=None, json=None):
        calls.append((method, path, json))
        return {"id": 123}

    client.rest_list = fake_rest_list
    client.rest_request = fake_rest_request
    client._calls = calls
    return client


async def test_get_render_device_status_reads_model():
    client = _client(
        {_STATUS: [{"id": 1, "device": 5, "render_enabled": True, "is_aggregate_managed": False}]}
    )
    try:
        status = await client.get_render_device_status("5")
    finally:
        await client.close()
    assert status is not None
    assert status.render_enabled is True and status.is_aggregate_managed is False


async def test_get_render_device_status_none_when_no_record():
    client = _client({_STATUS: []})
    try:
        assert await client.get_render_device_status("5") is None
    finally:
        await client.close()


async def test_render_enabled_device_ids():
    client = _client({_STATUS: [
        {"id": 1, "device": 5, "render_enabled": True},
        {"id": 2, "device": 6, "render_enabled": False},
        {"id": 3, "device": 7, "render_enabled": True},
    ]})
    try:
        ids = await client.get_render_enabled_device_ids(None)
    finally:
        await client.close()
    assert set(ids) == {"5", "7"}


async def test_upsert_intended_creates_then_patches():
    # No existing intended-config -> POST with device set.
    client = _client({_INTENDED: []})
    update = IntendedConfigurationUpdate(
        device_id="5", config_store_instance="http://cs", path="/d.cfg", commit_id="abc",
        updated="2026-01-01T00:00:00Z", updated_by="u", commit_message="m", template_version="v1",
    )
    try:
        await client.upsert_intended_configuration(update)
    finally:
        await client.close()
    method, path, body = client._calls[-1]
    assert method == "POST" and path == _INTENDED
    assert body["device"] == 5 and body["commit_id"] == "abc" and body["template_version"] == "v1"

    # Existing -> PATCH the existing row, no device in body.
    client = _client({_INTENDED: [{"id": 99, "device": 5}]})
    try:
        await client.upsert_intended_configuration(update)
    finally:
        await client.close()
    method, path, body = client._calls[-1]
    assert method == "PATCH" and path == f"{_INTENDED}99/" and "device" not in body


async def test_record_backup_upserts():
    client = _client({_BACKUP: []})
    intent = ConfigurationBackupIntent(
        device_id="5", config_store_url="http://cs", commit_id="c1", filename="d.cfg",
        user="u", commit_message="m", workflow_id="wf1", deployed_commit_id="c0",
    )
    try:
        await client.record_configuration_backup(intent)
    finally:
        await client.close()
    method, path, body = client._calls[-1]
    assert method == "POST" and path == _BACKUP
    assert body["workflow_id"] == "wf1" and body["deployed_commit_id"] == "c0"


async def test_config_drift():
    # intended.commit_id != backup.deployed_commit_id -> drift
    client = _client({
        _INTENDED: [{"id": 1, "device": 5, "commit_id": "new"}],
        _BACKUP: [{"id": 1, "device": 5, "deployed_commit_id": "old"}],
    })
    try:
        assert await client.has_recorded_config_drift("5") is True
    finally:
        await client.close()
    # equal -> no drift
    client = _client({
        _INTENDED: [{"id": 1, "device": 5, "commit_id": "same"}],
        _BACKUP: [{"id": 1, "device": 5, "deployed_commit_id": "same"}],
    })
    try:
        assert await client.has_recorded_config_drift("5") is False
    finally:
        await client.close()
