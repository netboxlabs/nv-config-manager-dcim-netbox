# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""NVCM lifecycle state (managed / intended / backup).

Backed by first-class plugin models: ``DeviceConfigStatus`` (per-device flags),
``IntendedConfig``, and ``BackupConfig`` -- one record per device, read/written
over the plugin REST API.
The config *content* lives in the config store (referenced by ``commit_id``);
these records hold the flags and pointers. A device is "managed" when its
``DeviceConfigStatus.render_enabled`` is set.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from nv_config_manager_dcim import (
    ConfigurationBackupMetadata,
    RenderDeviceStatus,
    RenderTemplateVersion,
)

from nv_config_manager_dcim_netbox._mappers import choice_str as _choice_str
from nv_config_manager_dcim_netbox._mappers import device_pk as _device_pk
from nv_config_manager_dcim_netbox._mappers import slug_of as _slug_of
from nv_config_manager_dcim_netbox.fabric_common import (
    BACKUP_PATH as _BACKUP_PATH,
)
from nv_config_manager_dcim_netbox.fabric_common import (
    INTENDED_PATH as _INTENDED_PATH,
)
from nv_config_manager_dcim_netbox.fabric_common import (
    STATUS_PATH as _STATUS_PATH,
)
from nv_config_manager_dcim_netbox.fabric_common import (
    chunked as _chunked,
)

if TYPE_CHECKING:
    from nv_config_manager_dcim import ConfigurationBackupIntent, IntendedConfigurationUpdate

logger = logging.getLogger(__name__)

# Platform slugs eligible for backup (matches the managed-fleet platforms).
_BACKUP_PLATFORMS = frozenset({"arista-eos", "cumulus-linux", "nv-os"})
# ZTP writes ``provisioned``, so freshly-provisioned devices need it as well as
# ``active`` or they would never be backed up.
_BACKUP_STATUSES = frozenset({"active", "provisioned"})


class NetBoxLifecycleStateMixin:
    """Managed-device flags and intended/backup configuration metadata."""

    async def _one_for_device(self, path: str, device_id: str) -> dict | None:
        """Return the single per-device record at ``path``, or None."""
        rows = await self.rest_list(path, [("device_id", str(device_id))])
        return rows[0] if rows else None

    async def _upsert_for_device(self, path: str, device_id: str, body: dict) -> None:
        """Create or update the per-device record at ``path``.

        Only for callers supplying a *complete* body: the plugin's IntendedConfig and
        BackupConfig require every config-store pointer field, so the create branch
        rejects a partial one. Partial updates go through ``_patch_for_device``.
        """
        existing = await self._one_for_device(path, device_id)
        if existing:
            await self.rest_request("PATCH", f"{path}{existing['id']}/", json=body)
        else:
            await self.rest_request("POST", path, json={"device": int(device_id), **body})

    async def _patch_for_device(self, path: str, device_id: str, body: dict) -> bool:
        """Patch fields onto an existing per-device record; False if there is none."""
        existing = await self._one_for_device(path, device_id)
        if not existing:
            return False
        await self.rest_request("PATCH", f"{path}{existing['id']}/", json=body)
        return True

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
        """Record a template-version change on the device's existing intended config.

        Patch-only, matching the reference provider: a template_version alone is not
        a valid IntendedConfig (the plugin also requires the config-store pointer
        fields), and a device with no record has never been rendered.
        """
        patched = await self._patch_for_device(
            _INTENDED_PATH, device_id, {"template_version": template_version}
        )
        if not patched:
            logger.warning(
                "No intended config for device %s; not recording template version %s",
                device_id,
                template_version,
            )

    async def get_backup_enabled_device_ids(self, is_aggregate_managed: bool) -> set[str]:
        """Backup-enabled devices that are in service on a backup-capable platform.

        Status and platform are matched client-side, as the reference provider
        does. Both are choice-validated filters in NetBox, so sending a value this
        NetBox does not define (``provisioned`` needs the plugin's FIELD_CHOICES;
        a platform needs the slug to exist) fails the whole query with a 400
        instead of matching nothing.
        """
        statuses = await self.rest_list(
            _STATUS_PATH,
            [
                ("backup_enabled", "true"),
                ("is_aggregate_managed", str(is_aggregate_managed).lower()),
            ],
        )
        backup_enabled = {pk for s in statuses if (pk := _device_pk(s))}
        if not backup_enabled:
            return set()
        devices = await self.devices_by_ids(backup_enabled)
        return {
            str(d["id"])
            for d in devices
            if _choice_str(d.get("status")) in _BACKUP_STATUSES
            and _slug_of(d.get("platform")) in _BACKUP_PLATFORMS
        }

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
