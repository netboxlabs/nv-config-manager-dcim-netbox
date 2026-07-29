# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Behavioral tests for the lifecycle-state methods not covered elsewhere."""

from __future__ import annotations

from tests._fake import fake_client as _client

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
    assert [(r.device_id, r.template_version) for r in result] == [("5", "v1")]


async def test_update_render_template_version_creates_when_absent():
    record = []
    # no existing intended-config row -> POST create
    client = _client(rest_list={_INTENDED: []}, record=record)
    await client.update_render_template_version("5", "v2")
    posts = [(p, body) for method, p, body in record if method == "POST"]
    assert posts and posts[0][0] == _INTENDED
    assert posts[0][1]["device"] == 5 and posts[0][1]["template_version"] == "v2"


async def test_get_backup_enabled_device_ids_intersects_eligible():
    client = _client(
        rest_list={
            _STATUS: [{"device": 5}, {"device": 6}],
            "dcim/devices/": [{"id": 5}],  # only 5 is active on a backup platform
        }
    )
    assert await client.get_backup_enabled_device_ids(True) == {"5"}


async def test_get_configuration_backup_metadata():
    client = _client(
        rest_list={_BACKUP: [{"commit_id": "c1", "deployed_commit_id": "d1", "workflow_id": "w1"}]}
    )
    meta = await client.get_configuration_backup_metadata("5")
    assert meta is not None and meta.commit_id == "c1" and meta.workflow_id == "w1"


async def test_get_configuration_backup_metadata_missing_returns_none():
    assert await _client(rest_list={_BACKUP: []}).get_configuration_backup_metadata("5") is None
