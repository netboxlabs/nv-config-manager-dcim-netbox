# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Constants shared by the installer plugin, the CLI and the DSX Air hooks."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

# Source checkout layout. Packaged installs have no chart or profile; the
# settings model accepts explicit paths for those.
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CHART = REPO_ROOT / "deploy" / "netbox"
DEFAULT_PROFILE = REPO_ROOT / "deploy" / "configs" / "local-netbox.yaml"

# Image tags track the package version, the same source `make images` uses.
try:
    IMAGE_TAG = version("nv-config-manager-dcim-netbox")
except PackageNotFoundError:  # pragma: no cover - not installed
    IMAGE_TAG = "0.0.0"

PROVIDER_IMAGE = f"nv-config-manager-dcim-netbox:{IMAGE_TAG}"
NETBOX_IMAGE = "nv-config-manager-netbox"

# Entry-point name shared by the DCIM SDK provider, the installer provider and
# the `provider` field the plugin stamps on every change event.
PROVIDER_NAME = "netbox"

# JetStream stream and subject NetBox publishes change events on. The chart's
# nvcm.eventStream / nvcm.eventSubject and NVCM's stream filter must agree.
EVENT_STREAM = "netbox"
EVENT_SUBJECT = "netbox.change"
