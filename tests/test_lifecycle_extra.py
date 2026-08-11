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
    client = _client(
        rest_list={
            _STATUS: [{"device": 5}, {"device": 6}],
            "dcim/devices/": [{"id": 5}],  # only 5 is active on a backup platform
        }
    )
    result = await client.get_backup_enabled_device_ids(True)
    assert {i for i in result} == {"5"}


async def test_get_configuration_backup_metadata():
    client = _client(
        rest_list={_BACKUP: [{"commit_id": "c1", "deployed_commit_id": "d1", "workflow_id": "w1"}]}
    )
    meta = await client.get_configuration_backup_metadata("5")
    assert meta is not None and meta.commit_id == "c1" and meta.workflow_id == "w1"


async def test_get_configuration_backup_metadata_missing_returns_none():
    assert await _client(rest_list={_BACKUP: []}).get_configuration_backup_metadata("5") is None
