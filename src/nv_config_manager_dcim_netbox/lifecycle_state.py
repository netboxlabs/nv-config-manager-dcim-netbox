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

from nv_config_manager_dcim_netbox._mappers import fk_id
from nv_config_manager_dcim_netbox.fabric_common import (
    BACKUP_PATH as _BACKUP_PATH,
)
from nv_config_manager_dcim_netbox.fabric_common import (
    INTENDED_PATH as _INTENDED_PATH,
)
from nv_config_manager_dcim_netbox.fabric_common import (
    STATUS_PATH as _STATUS_PATH,
)

if TYPE_CHECKING:
    from nv_config_manager_dcim import ConfigurationBackupIntent, IntendedConfigurationUpdate

# Platform slugs eligible for backup (matches the managed-fleet platforms).
_BACKUP_PLATFORMS = ("arista-eos", "cumulus-linux", "nv-os")

# Device ids per ``device_id`` list query. Repeating the param for a whole fleet
# builds a query string long enough for a proxy to reject with 414, so the reads
# that filter by many ids walk the fleet in chunks.
_ID_CHUNK = 100


def _device_pk(row: dict) -> str | None:
    """Return the NetBox device PK from a plugin row's ``device`` field.

    The plugin's serializers may render the FK as a nested brief object or a bare
    PK depending on the endpoint, so read either shape.
    """
    pk = fk_id(row.get("device"))
    return str(pk) if pk is not None else None


def _chunked(values: list[str], size: int = _ID_CHUNK):
    """Yield ``values`` in fixed-size chunks."""
    for start in range(0, len(values), size):
        yield values[start : start + size]


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
        return [pk for s in statuses if (pk := _device_pk(s))]

    async def get_render_template_versions(self) -> list[RenderTemplateVersion]:
        statuses = await self.rest_list(_STATUS_PATH, [("render_enabled", "true")])
        render_device_ids = [pk for s in statuses if (pk := _device_pk(s))]
        if not render_device_ids:
            return []
        # Chunked: one query per _ID_CHUNK devices rather than every id in a
        # single query string (see _ID_CHUNK).
        pages = await asyncio.gather(
            *(
                self.rest_list(_INTENDED_PATH, [("device_id", did) for did in chunk])
                for chunk in _chunked(render_device_ids)
            )
        )
        versions = {
            pk: row.get("template_version")
            for page in pages
            for row in page
            if (pk := _device_pk(row))
        }
        return [
            RenderTemplateVersion(device_id=pk, template_version=versions.get(pk))
            for pk in render_device_ids
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
        # Backup-enabled statuses intersected with in-service devices on
        # backup-capable platforms, in two independent queries. ``provisioned`` is
        # included as well as ``active``: ZTP writes it, so freshly-provisioned
        # devices would otherwise never be backed up.
        params = [("status", "active"), ("status", "provisioned")]
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
        backup_enabled = {pk for s in statuses if (pk := _device_pk(s))}
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
