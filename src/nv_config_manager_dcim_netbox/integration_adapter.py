# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""NetBox inspection adapter for NVCM's shared live-cluster integration tests."""

from __future__ import annotations

from typing import Any

import requests


class NetBoxIntegrationAdapter:
    """Translate NetBox plugin lifecycle records into the common test shape."""

    provider_name = "netbox"
    service_name = "netbox"
    service_port = 80
    hostname_prefix = "netbox"
    # NVCM 1.x retains this legacy secret name for chart compatibility.
    secret_name = "nautobot-token"
    secret_key = "token"
    url_environment_variable = "NETBOX_URL"
    token_environment_variable = "NETBOX_TOKEN"

    def __init__(self, server: str, token: str):
        self.server = server.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"Token {token}", "Content-Type": "application/json"}
        )
        self.session.verify = False

    def _get(self, path: str, params: Any = None) -> dict[str, Any]:
        response = self.session.get(
            f"{self.server}/api/{path.lstrip('/')}", params=params, timeout=30
        )
        response.raise_for_status()
        return response.json()

    def _list(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        query = dict(params or {})
        query.setdefault("limit", 250)
        results: list[dict[str, Any]] = []
        url: str | None = f"{self.server}/api/{path.lstrip('/')}"
        while url:
            response = self.session.get(url, params=query, timeout=30)
            response.raise_for_status()
            payload = response.json()
            results.extend(payload.get("results", []))
            url = payload.get("next")
            query = {}
        return results

    @staticmethod
    def _reference_id(reference: Any) -> str | None:
        if isinstance(reference, dict):
            value = reference.get("id")
        else:
            value = reference
        return str(value) if value is not None else None

    def _interfaces(self, device_id: str) -> list[dict[str, Any]]:
        interfaces = self._list("dcim/interfaces/", {"device_id": device_id})
        for interface in interfaces:
            addresses = self._list("ipam/ip-addresses/", {"interface_id": interface["id"]})
            role = interface.get("role")
            if isinstance(role, dict):
                role = role.get("name") or role.get("label") or role.get("value")
            if not role:
                role = (interface.get("custom_fields") or {}).get("nvcm_interface_role")
            primary_mac = interface.get("primary_mac_address") or {}
            interface["mac_address"] = interface.get("mac_address") or primary_mac.get(
                "mac_address"
            )
            interface["role"] = role
            interface["ip_addresses"] = [
                {"address": address.get("address")}
                for address in addresses
                if address.get("address")
            ]
        return interfaces

    def _reserved_device_ids(self) -> set[str]:
        reserved = self._list("ipam/ip-addresses/", {"tag": "dhcp-reserve"})
        return {
            str(device_id)
            for address in reserved
            if (device_id := ((address.get("assigned_object") or {}).get("device") or {}).get("id"))
            is not None
        }

    def list_devices(
        self,
        *,
        render_enabled: bool | None = None,
        ztp_enabled: bool | None = None,
        backup_enabled: bool | None = None,
        platform: str | None = None,
        include_interfaces: bool = False,
    ) -> list[dict[str, Any]]:
        params = {
            name: str(value).lower()
            for name, value in (
                ("render_enabled", render_enabled),
                ("ztp_enabled", ztp_enabled),
                ("backup_enabled", backup_enabled),
            )
            if value is not None
        }
        statuses = self._list("plugins/nv-config-manager/device-config-statuses/", params)
        intended = self._list("plugins/nv-config-manager/intended-configs/")
        intended_by_device = {
            device_id: row
            for row in intended
            if (device_id := self._reference_id(row.get("device"))) is not None
        }
        reserved_device_ids = self._reserved_device_ids() if include_interfaces else set()

        devices = []
        for status in statuses:
            device_id = self._reference_id(status.get("device"))
            if device_id is None:
                continue
            device = self._get(f"dcim/devices/{device_id}/")
            platform_name = (device.get("platform") or {}).get("name")
            if platform is not None and platform_name != platform:
                continue
            devices.append(
                {
                    "id": device_id,
                    "name": device.get("name"),
                    "serial": device.get("serial"),
                    "status": (device.get("status") or {}).get("label"),
                    "platform": platform_name,
                    "role": (device.get("role") or {}).get("name"),
                    "primary_ip4": (device.get("primary_ip4") or {}).get("address"),
                    "interfaces": self._interfaces(device_id) if include_interfaces else [],
                    "dhcp_reserved": device_id in reserved_device_ids,
                    "render_enabled": bool(status.get("render_enabled")),
                    "ztp_enabled": bool(status.get("ztp_enabled")),
                    "backup_enabled": bool(status.get("backup_enabled")),
                    "intended_config": intended_by_device.get(device_id),
                }
            )
        return devices

    def get_device_status(self, device_id: str) -> str:
        device = self._get(f"dcim/devices/{device_id}/")
        return str((device.get("status") or {}).get("label") or "")

    def backup_config_count(self) -> int:
        payload = self._get("plugins/nv-config-manager/backup-configs/", {"limit": 1})
        return int(payload.get("count", len(payload.get("results", []))))
