# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""NetBox implementation of the NVCM DCIM provider API."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from nv_config_manager_dcim import (
    DCIMChangeEvent,
    DCIMClient,
    DCIMProviderMetadata,
    DCIMRenderEventRegistry,
    ProviderSettings,
)

from nv_config_manager_dcim_netbox.cable_status import NetBoxCableStatusMixin
from nv_config_manager_dcim_netbox.client import (
    BaseNetBoxClient,
    netbox_connection_settings,
)
from nv_config_manager_dcim_netbox.dhcp import NetBoxDHCPMixin
from nv_config_manager_dcim_netbox.events import register_render_event_handlers
from nv_config_manager_dcim_netbox.fabric_common import FabricCommonMixin
from nv_config_manager_dcim_netbox.firmware import NetBoxFirmwareMixin
from nv_config_manager_dcim_netbox.hosts import NetBoxHostsMixin
from nv_config_manager_dcim_netbox.infiniband import NetBoxInfiniBandMixin
from nv_config_manager_dcim_netbox.inventory import NetBoxInventoryMixin
from nv_config_manager_dcim_netbox.ipam import NetBoxIPAMMixin
from nv_config_manager_dcim_netbox.lifecycle_state import NetBoxLifecycleStateMixin
from nv_config_manager_dcim_netbox.reference import NetBoxReferenceMixin
from nv_config_manager_dcim_netbox.render import NetBoxRenderMixin
from nv_config_manager_dcim_netbox.secrets import NetBoxSecretsMixin
from nv_config_manager_dcim_netbox.spectrumx import NetBoxSpectrumXMixin
from nv_config_manager_dcim_netbox.ztp import NetBoxZTPMixin

logger = logging.getLogger(__name__)


def _provider_version() -> str:
    """Read the installed package version so it is declared in one place only.

    Falls back to "0.0.0" when the package metadata is unavailable (running from a
    source tree that was never installed).
    """
    try:
        return version("nv-config-manager-dcim-netbox")
    except PackageNotFoundError:  # pragma: no cover - not installed
        logger.warning("nv-config-manager-dcim-netbox is not installed; reporting version 0.0.0")
        return "0.0.0"


class NetBoxDCIMClient(
    NetBoxCableStatusMixin,
    NetBoxInventoryMixin,
    NetBoxReferenceMixin,
    NetBoxIPAMMixin,
    NetBoxHostsMixin,
    NetBoxDHCPMixin,
    NetBoxZTPMixin,
    NetBoxFirmwareMixin,
    NetBoxLifecycleStateMixin,
    NetBoxSecretsMixin,
    NetBoxRenderMixin,
    NetBoxSpectrumXMixin,
    NetBoxInfiniBandMixin,
    FabricCommonMixin,
    BaseNetBoxClient,
):
    """One broad NetBox implementation of the SDK's ``DCIMClient`` contract.

    Each functional area is a mixin in its own module; the whole protocol is
    implemented against native NetBox REST and GraphQL, with no unsupported
    stubs. Device and location identifiers are native NetBox PKs in both
    directions. ``close``, the async-context and identifier-validation hooks, and
    ``get_device_ui_url`` are provided by ``BaseNetBoxClient``.
    """


# The SDK API versions this provider is known to implement, as literals.
#
# Deliberately not `(DCIM_PROVIDER_API_VERSION,)`: the SDK's registry admits a
# provider by comparing the running SDK's major version against this tuple, so
# echoing the installed SDK's own constant back at it makes the gate a tautology
# that passes on any version, including the incompatible one it exists to catch.
# Spelled out, an SDK major bump is refused at discovery with a clear message
# instead of failing somewhere inside a render.
#
# Bump this only after re-checking the coupling surface against the new SDK: the
# `DCIMClient` protocol (tests/test_coverage.py walks every member), the render
# models, and RENDER_DATA_CACHE_SCHEMA_VERSION.
SUPPORTED_API_VERSIONS = ("1.0",)


class NetBoxProvider:
    """NetBox provider factory and lifecycle contract."""

    metadata = DCIMProviderMetadata(
        name="netbox",
        display_name="NetBox",
        provider_version=_provider_version(),
        supported_api_versions=SUPPORTED_API_VERSIONS,
    )

    def validate_settings(self, settings: ProviderSettings) -> None:
        """Validate explicit NetBox provider settings."""
        netbox_connection_settings(settings)

    def create_client(self, settings: ProviderSettings) -> DCIMClient:
        """Create the configured NetBox client."""
        return NetBoxDCIMClient.from_settings(settings)

    def register_render_event_handlers(self, registry: DCIMRenderEventRegistry) -> None:
        """Register NetBox change-event interpretation with the render dispatcher.

        Maps each supported ``object_type`` (device, interface, overlay
        assignment, BGP session) to a handler that fans a change out to the
        device renders it should trigger. See ``events._HANDLERS``.
        """
        register_render_event_handlers(registry)

    def normalize_event(self, payload: Mapping[str, Any]) -> DCIMChangeEvent:
        """Validate a NetBox event envelope into the SDK ``DCIMChangeEvent``.

        The companion plugin's ``EVENTS_PIPELINE`` bridge already emits the
        provider-neutral envelope, so this validates and parses it.
        """
        return DCIMChangeEvent.from_dict(payload)
