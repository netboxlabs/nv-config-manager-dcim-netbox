# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""NVCM lifecycle state (managed / intended / backup).

Backed by first-class plugin models: ``DeviceConfigStatus`` (per-device flags),
``IntendedConfig``, and ``BackupConfig`` — one record per device, read/written
over the plugin REST API.
The config *content* lives in the config store (referenced by ``commit_id``);
these records hold the flags and pointers. A device is "managed" when its
``DeviceConfigStatus.render_enabled`` is set.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from nv_config_manager_dcim import (
    ConfigurationBackupMetadata,
    RenderDeviceStatus,
    RenderTemplateVersion,
)

if TYPE_CHECKING:
    from nv_config_manager_dcim import ConfigurationBackupIntent, IntendedConfigurationUpdate

_STATUS_PATH = "plugins/nv-config-manager/device-config-statuses/"
_INTENDED_PATH = "plugins/nv-config-manager/intended-configs/"
_BACKUP_PATH = "plugins/nv-config-manager/backup-configs/"

# Platform slugs eligible for backup (matches the managed-fleet platforms).
_BACKUP_PLATFORMS = ("arista-eos", "cumulus-linux", "nv-os")


class NetBoxLifecycleStateMixin:
    """Managed-device flags and intended/backup configuration metadata."""

    async def _one_for_device(self, path: str, device_id: str) -> dict | None:
        """Return the single per-device record at ``path``, or None."""
        rows = await self.rest_list(path, [("device_id", str(device_id))])
        return rows[0] if rows else None

    async def _upsert_for_device(self, path: str, device_id: str, body: dict) -> None:
        """Create or update the per-device record at ``path``."""
        existing = await self._one_for_device(path, device_id)
        if existing:
            await self.rest_request("PATCH", f"{path}{existing['id']}/", json=body)
        else:
            await self.rest_request("POST", path, json={"device": int(device_id), **body})

    async def get_render_device_status(self, device_id: str) -> RenderDeviceStatus | None:
        status = await self._one_for_device(_STATUS_PATH, device_id)
        if not status:
            return None
        return RenderDeviceStatus(
            render_enabled=bool(status.get("render_enabled")),
            is_aggregate_managed=bool(status.get("is_aggregate_managed")),
        )

    async def get_render_enabled_device_ids(self, is_aggregate_managed: bool | None) -> list[str]:
        params = [("render_enabled", "true")]
        if is_aggregate_managed is not None:
            params.append(("is_aggregate_managed", str(is_aggregate_managed).lower()))
        statuses = await self.rest_list(_STATUS_PATH, params)
        return [str(s["device"]) for s in statuses if s.get("device") is not None]

    async def get_render_template_versions(self) -> list[RenderTemplateVersion]:
        statuses = await self.rest_list(_STATUS_PATH, [("render_enabled", "true")])
        render_device_ids = [str(s["device"]) for s in statuses if s.get("device") is not None]
        if not render_device_ids:
            return []
        intended = await self.rest_list(
            _INTENDED_PATH, [("device_id", did) for did in render_device_ids]
        )
        versions = {
            str(row["device"]): row.get("template_version")
            for row in intended
            if row.get("device") is not None
        }
        return [
            RenderTemplateVersion(device_id=device_id, template_version=versions.get(device_id))
            for device_id in render_device_ids
        ]

    async def upsert_intended_configuration(self, update: IntendedConfigurationUpdate) -> None:
        await self._upsert_for_device(
            _INTENDED_PATH,
            update.device_id,
            {
                "config_store_instance": update.config_store_instance,
                "path": update.path,
                "commit_id": update.commit_id,
                "updated": update.updated,
                "updated_by": update.updated_by,
                "commit_message": update.commit_message,
                "template_version": update.template_version,
            },
        )

    async def update_render_template_version(self, device_id: str, template_version: str) -> None:
        await self._upsert_for_device(
            _INTENDED_PATH, device_id, {"template_version": template_version}
        )

    async def get_backup_enabled_device_ids(self, is_aggregate_managed: bool) -> set[str]:
        # Intersect backup-enabled statuses with active devices on backup-capable
        # platforms; the two queries are independent, so run them concurrently.
        params = [("status", "active")]
        params.extend(("platform", slug) for slug in _BACKUP_PLATFORMS)
        statuses, devices = await asyncio.gather(
            self.rest_list(
                _STATUS_PATH,
                [
                    ("backup_enabled", "true"),
                    ("is_aggregate_managed", str(is_aggregate_managed).lower()),
                ],
            ),
            self.rest_list("dcim/devices/", params),
        )
        backup_enabled = {str(s["device"]) for s in statuses if s.get("device") is not None}
        eligible = {str(d["id"]) for d in devices}
        return backup_enabled & eligible

    async def get_configuration_backup_metadata(
        self, device_id: str
    ) -> ConfigurationBackupMetadata | None:
        backup = await self._one_for_device(_BACKUP_PATH, device_id)
        if not backup:
            return None
        return ConfigurationBackupMetadata(
            commit_id=backup.get("commit_id"),
            deployed_commit_id=backup.get("deployed_commit_id") or None,
            workflow_id=backup.get("workflow_id"),
        )

    async def record_configuration_backup(self, intent: ConfigurationBackupIntent) -> None:
        await self._upsert_for_device(
            _BACKUP_PATH,
            intent.device_id,
            {
                "config_store_instance": intent.config_store_url,
                "path": intent.filename,
                "commit_id": intent.commit_id,
                "updated": datetime.now(UTC).isoformat(),
                "updated_by": intent.user,
                "commit_message": intent.commit_message,
                "workflow_id": intent.workflow_id,
                "deployed_commit_id": intent.deployed_commit_id or "",
            },
        )

    async def has_recorded_config_drift(self, device_id: str) -> bool:
        intended, backup = await asyncio.gather(
            self._one_for_device(_INTENDED_PATH, device_id),
            self._one_for_device(_BACKUP_PATH, device_id),
        )
        intended_commit = (intended or {}).get("commit_id")
        deployed_commit = (backup or {}).get("deployed_commit_id")
        return intended_commit != deployed_commit
