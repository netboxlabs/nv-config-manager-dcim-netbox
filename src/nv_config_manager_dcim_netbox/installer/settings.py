# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""``dcim.settings`` model for the NetBox installer provider."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from nv_config_manager_dcim_netbox.installer.config import (
    DEFAULT_CHART,
    EVENT_STREAM,
    EVENT_SUBJECT,
    IMAGE_TAG,
    NETBOX_IMAGE,
    PROVIDER_IMAGE,
)

# Django META name of the username header. NVCM's OIDC proxy sets
# X-Auth-Request-Email on browser traffic and its JWT claim mapping sets it on
# svc-* traffic; X-Auth-Request-User is proxy-only and equals the email claim.
DEFAULT_USERNAME_HEADER = "HTTP_X_AUTH_REQUEST_EMAIL"
GROUP_HEADER = "HTTP_X_AUTH_REQUEST_GROUPS"
# oauth2-proxy joins the groups claim with commas.
GROUP_SEPARATOR = ","

# Database the chart installs NetBox against. `cnpg` needs the operator
# (NVCM's deploy installs it before NetBox); `bundled` is the upstream chart's
# PostgreSQL subchart, for clusters that will never get the operator.
DatabaseBackend = Literal["cnpg", "bundled"]


class NetBoxSSOSettings(BaseModel):
    """Remote-user mapping applied when NVCM's `sso.enabled` is true.

    Group lists default to NVCM's `rbac.admin_roles` minus the `all` sentinel.
    """

    model_config = ConfigDict(extra="forbid")

    username_header: str = DEFAULT_USERNAME_HEADER
    superuser_groups: list[str] | None = None
    staff_groups: list[str] | None = None
    # Groups every remote user joins, and NetBox permissions granted to them:
    # `app.action_model` -> None, or a list of constraint dicts.
    default_groups: list[str] = Field(default_factory=list)
    default_permissions: dict[str, list[dict[str, Any]] | None] = Field(default_factory=dict)


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
    database: DatabaseBackend = "cnpg"
    sso: NetBoxSSOSettings = Field(default_factory=NetBoxSSOSettings)
    # Extra `--set key=value` overrides for deploy/netbox, applied last.
    helm_set: list[str] = Field(default_factory=list)
    # DSX Air only: load the simulated fabric into NetBox during bring-up.
    load_blueprint: bool = True

    def database_overrides(self) -> list[str]:
        """Return the chart `--set` pairs selecting the bundled PostgreSQL."""
        if self.database == "cnpg":
            return []
        return ["nvcm.database.cnpg.enabled=false", "netbox.postgresql.enabled=true"]

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
    # The bring-up runs before NVCM's deploy puts the CNPG operator on the box.
    database: DatabaseBackend = "bundled"
    # Installed into NetBox and handed to NVCM; generated when empty.
    token: str = ""
