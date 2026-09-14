# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Behavioral tests for the lifecycle-state methods not covered elsewhere."""

from __future__ import annotations

import pytest

from tests._fake import fake_client as _client

# Area marker for `make <area>` / `pytest -m <area>`.
pytestmark = pytest.mark.state
_STATUS = "plugins/nv-config-manager/device-config-statuses/"
_INTENDED = "plugins/nv-config-manager/intended-configs/"
_BACKUP = "plugins/nv-config-manager/backup-configs/"


async def test_get_render_template_versions():
    client = _client(
        rest_list={
            _STATUS: [{"device": 5}],
            _INTENDED: [{"device": 5, "template_version": "v1"}],
        }
    )
    result = await client.get_render_template_versions()
    # device_id is the native NetBox pk, like every other id handed back to NVCM.
    assert [(r.device_id, r.template_version) for r in result] == [("5", "v1")]


async def test_get_render_template_versions_reads_nested_device_fk():
    # The plugin serializer may render `device` as a nested brief object rather
    # than a bare PK; both shapes must resolve to the same device.
    client = _client(
        rest_list={
            _STATUS: [{"device": {"id": 5, "name": "leaf1"}}],
            _INTENDED: [{"device": {"id": 5}, "template_version": "v1"}],
        }
    )
    result = await client.get_render_template_versions()
    assert [(r.device_id, r.template_version) for r in result] == [("5", "v1")]


async def test_update_render_template_version_patches_the_existing_record():
    record = []
    client = _client(rest_list={_INTENDED: [{"id": 12, "device": 5}]}, record=record)
    await client.update_render_template_version("5", "v2")
    assert record == [("PATCH", f"{_INTENDED}12/", {"template_version": "v2"})]


async def test_update_render_template_version_does_not_create_when_absent():
    # A template_version alone is not a valid IntendedConfig (the plugin requires the
    # config-store pointer fields too), so a create here always 400s.
    record = []
    client = _client(rest_list={_INTENDED: []}, record=record)
    await client.update_render_template_version("5", "v2")
    assert not [p for method, p, _ in record if method == "POST"]


async def test_get_backup_enabled_device_ids_intersects_eligible():
    # Status and platform are matched client-side, so the device rows carry them
    # rather than the query filtering on them.
    client = _client(
        rest_list={
            _STATUS: [{"device": 5}, {"device": 6}, {"device": 7}],
            "dcim/devices/": [
                {"id": 5, "status": {"value": "active"}, "platform": {"slug": "cumulus-linux"}},
                {"id": 6, "status": {"value": "offline"}, "platform": {"slug": "cumulus-linux"}},
                {"id": 7, "status": {"value": "active"}, "platform": {"slug": "generic-linux"}},
            ],
        }
    )
    result = await client.get_backup_enabled_device_ids(True)
    assert {i for i in result} == {"5"}


async def test_get_backup_enabled_device_ids_includes_juniper_junos():
    # Parity: the reference's backup platform set includes "Juniper Junos".
    client = _client(
        rest_list={
            _STATUS: [{"device": 5}],
            "dcim/devices/": [
                {"id": 5, "status": {"value": "active"}, "platform": {"slug": "juniper-junos"}},
            ],
        }
    )
    assert set(await client.get_backup_enabled_device_ids(True)) == {"5"}


async def test_get_backup_enabled_device_ids_accepts_provisioned():
    # ZTP writes `provisioned`; a NetBox without the plugin's FIELD_CHOICES cannot
    # be asked to filter on it, so it must be matched here.
    client = _client(
        rest_list={
            _STATUS: [{"device": 5}],
            "dcim/devices/": [
                {"id": 5, "status": {"value": "provisioned"}, "platform": {"slug": "nv-os"}},
            ],
        }
    )
    assert set(await client.get_backup_enabled_device_ids(True)) == {"5"}


async def test_get_backup_enabled_device_ids_does_not_filter_server_side(monkeypatch):
    # A platform/status the NetBox does not define is a 400, not an empty match,
    # so neither may appear as a query param.
    seen: list[list[tuple[str, str]]] = []
    client = _client(rest_list={_STATUS: [{"device": 5}], "dcim/devices/": []})
    original = client.rest_list

    async def spy(path, params=None, **kwargs):
        if path == "dcim/devices/":
            seen.append(list(params or []))
        return await original(path, params, **kwargs)

    monkeypatch.setattr(client, "rest_list", spy)
    await client.get_backup_enabled_device_ids(True)
    keys = {key for params in seen for key, _ in params}
    assert "platform" not in keys and "status" not in keys


async def test_get_configuration_backup_metadata():
    client = _client(
        rest_list={_BACKUP: [{"commit_id": "c1", "deployed_commit_id": "d1", "workflow_id": "w1"}]}
    )
    meta = await client.get_configuration_backup_metadata("5")
    assert meta is not None and meta.commit_id == "c1" and meta.workflow_id == "w1"


async def test_get_configuration_backup_metadata_missing_returns_none():
    assert await _client(rest_list={_BACKUP: []}).get_configuration_backup_metadata("5") is None
