# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tests for the adapter consumed by NVCM's shared cluster integration suite."""

from __future__ import annotations

from typing import Any

from nv_config_manager_dcim_netbox import integration_adapter


class _Response:
    def __init__(self, payload: dict[str, Any]):
        self.payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return self.payload


class _Session:
    def __init__(self):
        self.headers: dict[str, str] = {}
        self.verify = True
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        timeout: int = 0,
    ) -> _Response:
        del timeout
        query = dict(params or {})
        self.calls.append((url, query))
        if "device-config-statuses" in url:
            return _Response(
                {
                    "results": [
                        {
                            "device": 2,
                            "render_enabled": True,
                            "ztp_enabled": True,
                            "backup_enabled": True,
                        }
                    ],
                    "next": None,
                }
            )
        if "intended-configs" in url:
            return _Response(
                {
                    "results": [{"device": 2, "path": "2/startup.yaml", "commit_id": "abc"}],
                    "next": None,
                }
            )
        if "backup-configs" in url:
            return _Response({"count": 1, "results": []})
        if "dcim/devices/2" in url:
            return _Response(
                {
                    "id": 2,
                    "name": "leaf-01",
                    "serial": "serial-1",
                    "status": {"label": "Provisioning"},
                    "platform": {"name": "Cumulus Linux"},
                    "role": {"name": "Leaf"},
                    "primary_ip4": {"address": "192.0.2.2/24"},
                }
            )
        if "dcim/interfaces" in url:
            return _Response(
                {
                    "results": [
                        {
                            "id": 20,
                            "name": "eth0",
                            "role": None,
                            "custom_fields": {"nvcm_interface_role": "Uplink"},
                            "primary_mac_address": {"mac_address": "00:11:22:33:44:55"},
                            "mgmt_only": True,
                        }
                    ],
                    "next": None,
                }
            )
        if "ipam/ip-addresses" in url and query.get("tag") == "dhcp-reserve":
            return _Response(
                {
                    "results": [{"assigned_object": {"device": {"id": 2}}}],
                    "next": None,
                }
            )
        if "ipam/ip-addresses" in url:
            return _Response({"results": [{"address": "192.0.2.2/24"}], "next": None})
        raise AssertionError(f"Unexpected request: {url} {query}")


def _adapter(monkeypatch) -> tuple[integration_adapter.NetBoxIntegrationAdapter, _Session]:
    session = _Session()
    monkeypatch.setattr(integration_adapter.requests, "Session", lambda: session)
    adapter = integration_adapter.NetBoxIntegrationAdapter("https://netbox.example", "token")
    return adapter, session


def test_common_shape_avoids_expensive_interface_reads_by_default(monkeypatch):
    adapter, session = _adapter(monkeypatch)

    devices = adapter.list_devices(render_enabled=True, platform="Cumulus Linux")

    assert devices[0] == {
        "id": "2",
        "name": "leaf-01",
        "serial": "serial-1",
        "status": "Provisioning",
        "platform": "Cumulus Linux",
        "role": "Leaf",
        "primary_ip4": "192.0.2.2/24",
        "interfaces": [],
        "dhcp_reserved": False,
        "render_enabled": True,
        "ztp_enabled": True,
        "backup_enabled": True,
        "intended_config": {"device": 2, "path": "2/startup.yaml", "commit_id": "abc"},
    }
    assert not any("interfaces" in url or "ip-addresses" in url for url, _ in session.calls)


def test_dhcp_shape_includes_interfaces_and_reservation(monkeypatch):
    adapter, _ = _adapter(monkeypatch)

    device = adapter.list_devices(ztp_enabled=True, include_interfaces=True)[0]

    assert device["dhcp_reserved"] is True
    assert device["interfaces"][0]["role"] == "Uplink"
    assert device["interfaces"][0]["mac_address"] == "00:11:22:33:44:55"
    assert device["interfaces"][0]["ip_addresses"] == [{"address": "192.0.2.2/24"}]


def test_status_and_backup_inspection_use_provider_api(monkeypatch):
    adapter, _ = _adapter(monkeypatch)

    assert adapter.get_device_status("2") == "Provisioning"
    assert adapter.backup_config_count() == 1
