# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Discovery and lifecycle tests for the NetBox DCIM provider."""

from __future__ import annotations

import pytest
from nv_config_manager_dcim import (
    DCIM_PROVIDER_API_VERSION,
    DCIMProviderConfigurationError,
    create_dcim_client,
    discover_dcim_providers,
)

# Area marker for `make <area>` / `pytest -m <area>`.
pytestmark = pytest.mark.lifecycle


def test_provider_is_discovered_via_entry_point():
    providers = discover_dcim_providers()
    assert "netbox" in providers
    metadata = providers["netbox"].metadata
    assert metadata.name == "netbox"
    assert DCIM_PROVIDER_API_VERSION in metadata.supported_api_versions


def test_create_client_requires_server_and_token():
    with pytest.raises(DCIMProviderConfigurationError):
        create_dcim_client("netbox", {})
    with pytest.raises(DCIMProviderConfigurationError):
        create_dcim_client("netbox", {"server": "https://netbox.example"})


async def test_create_client_returns_closable_client():
    client = create_dcim_client("netbox", {"server": "https://netbox.example", "token": "abc"})
    try:
        assert client.get_device_ui_url("1").endswith("/dcim/devices/1/")
    finally:
        await client.close()
