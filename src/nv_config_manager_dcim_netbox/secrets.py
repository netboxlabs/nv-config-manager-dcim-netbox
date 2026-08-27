# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Secret / password mapping operations (config-context driven).

These surface secret *references* (which secret name / which version), not secret
values -- the reference NVCM implementation reads them from the device's config
context, and so does this. No secret store (e.g. netbox-secrets / Vault) is
involved: the actual secret material is resolved elsewhere in NVCM.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

_DEVICE_CONFIG_CONTEXT_QUERY = """
query ($id: ID!) { device(id: $id) { config_context } }
"""


class NetBoxSecretsMixin:
    """Per-device password mapping and secret reference reads."""

    async def _device_config_context(self, device_id: str) -> dict:
        data = await self.graphql_query(_DEVICE_CONFIG_CONTEXT_QUERY, {"id": device_id})
        return (data.get("device") or {}).get("config_context") or {}

    async def _device_password_mappings(self, device_id: str) -> dict:
        """Return the device's ``password_mappings``, or ``{}`` if unusable.

        One shape guard, so the two password reads cannot disagree on what counts as
        a valid mapping.
        """
        config_context = await self._device_config_context(device_id)
        password_mappings = config_context.get("password_mappings")
        return password_mappings if isinstance(password_mappings, dict) else {}

    async def get_device_password_mapping_users(self, device_id: str) -> set[str]:
        mappings = await self._device_password_mappings(device_id)
        return {str(username) for username in mappings}

    async def get_device_secret_versions(self, device_id: str) -> Mapping[str, str]:
        """Map the device config context to normalized secret-version intent."""
        config_context = await self._device_config_context(device_id)
        secret_versions = config_context.get("secrets_versions")
        if not isinstance(secret_versions, dict):
            return {}
        return {
            str(secret_type): str(version)
            for secret_type, version in secret_versions.items()
            if isinstance(secret_type, str) and version is not None
        }

    async def get_device_password_secret_names(self, device_id: str) -> Mapping[str, str]:
        """Map device config-context password mappings to username→secret-name intent."""
        password_mappings = await self._device_password_mappings(device_id)
        result: dict[str, str] = {}
        for username, user_config in password_mappings.items():
            if not isinstance(username, str) or not isinstance(user_config, dict):
                continue
            password_type = user_config.get("password", "")
            rotation = user_config.get("rotation", "")
            result[username] = f"{password_type}_{rotation}"
        return result
