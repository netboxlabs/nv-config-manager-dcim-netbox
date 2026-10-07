# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""``dcim.settings`` model for the NetBox installer provider."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from nv_config_manager_dcim_netbox.installer.config import (
    DEFAULT_CHART,
    EVENT_STREAM,
    EVENT_SUBJECT,
    IMAGE_TAG,
    NETBOX_IMAGE,
    PROVIDER_IMAGE,
)


class NetBoxInstallerSettings(BaseModel):
    """Chart, image and event-bus settings for a local NetBox deployment."""

    model_config = ConfigDict(extra="forbid")

    chart: Path = DEFAULT_CHART
    release: str = "netbox"
    image_repository: str = NETBOX_IMAGE
    image_tag: str = IMAGE_TAG
    # Wheelhouse installed into NVCM's pods when dcim.provider_packages is empty.
    provider_image: str = PROVIDER_IMAGE
    # Gateway hostname; empty publishes NetBox at netbox.<cluster.hostname>.
    hostname: str = ""
    superuser_name: str = "admin"
    # Empty lets the chart generate one.
    superuser_password: str = ""
    event_stream: str = EVENT_STREAM
    event_subject: str = EVENT_SUBJECT
    consumer_name: str = "nv-config-manager-dcim"
    # Init containers poll this path before dependent services start.
    readiness_path: str = "/login/"
    # Extra `--set key=value` overrides for deploy/netbox, applied last.
    helm_set: list[str] = Field(default_factory=list)
    # DSX Air only: load the simulated fabric into NetBox during bring-up.
    load_blueprint: bool = True

    def netbox_hostname(self, cluster_hostname: str) -> str:
        """Return the hostname NetBox is published under."""
        return self.hostname or f"netbox.{cluster_hostname}"

    @property
    def netbox_image(self) -> str:
        """Return the full NetBox image reference."""
        return f"{self.image_repository}:{self.image_tag}"


class NetBoxAirSettings(NetBoxInstallerSettings):
    """``dcim_settings`` for a NetBox DSX Air simulation.

    AIR simulations are disposable demo environments, so the UI login defaults
    to the same predictable value the bundled demo provider uses.
    """

    superuser_name: str = "demo"
    superuser_password: str = "demo"
    # Installed into NetBox and handed to NVCM; generated when empty.
    token: str = ""
