# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Produce an nv-config-manager-install.yaml for an external-NetBox deployment.

The shipped profile (``deploy/configs/local-netbox.yaml``) is a complete, valid
installer config, not a template: it is loaded as YAML and only the computed
fields are overridden. That keeps the profile editable and independently
validatable (``nv-config-manager-installer validate``), and means we never model
NVCM's schema ourselves.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import yaml

# Where the profile lives when running from a source checkout. Packaged copies
# are found through importlib.resources instead.
_REPO_PROFILE = Path(__file__).resolve().parents[3] / "deploy" / "configs" / "local-netbox.yaml"

# Image tags track the package version, the same source `make images` builds them
# from. Hardcoded here, a version bump would leave the installer asking for a tag
# that was never built.
try:
    IMAGE_TAG = version("nv-config-manager-dcim-netbox")
except PackageNotFoundError:  # pragma: no cover - not installed
    IMAGE_TAG = "0.0.0"

PROVIDER_IMAGE = f"nv-config-manager-dcim-netbox:{IMAGE_TAG}"
NETBOX_IMAGE = "nv-config-manager-netbox"

TOKEN_PLACEHOLDER = "REPLACED_BY_INSTALLER"

# The provider's entry-point name, which must equal DCIMProviderMetadata.name and
# the `provider` field the plugin stamps on every change event.
PROVIDER_NAME = "netbox"

# The JetStream stream and subject NetBox publishes change events on. Three
# things must agree -- this, the chart's nvcm.eventSubject, and the plugin's
# nats_subject -- or the broker drops every event silently. NVCM's own default
# filters on `nautobot`, which matches nothing NetBox publishes.
EVENT_STREAM = "netbox"
EVENT_SUBJECT = "netbox.change"

# The NVCM branch to check out and to clone onto a sim box. The pluggable-DCIM
# framework this provider plugs into merged to main, so this is NVCM's default
# branch; it stays a named constant because every checkout, sim clone and CI job
# resolves it from here.
NVCM_BRANCH = "main"


@dataclass(frozen=True)
class ConfigOptions:
    """Values `up`/`config` compute or take from flags; everything else is profile."""

    api_token: str
    namespace: str = "nv-config-manager"
    size: str = "small"
    provider_image: str = PROVIDER_IMAGE
    netbox_server: str = "http://netbox"
    netbox_public_url: str = "https://netbox.config-manager.local"
    # Supplied rather than left to NVCM's deployer to generate, so NetBox can be
    # installed once with it instead of rolled again afterwards.
    nats_password: str = ""


def load_profile(path: Path | None = None) -> dict[str, Any]:
    """Load the base installer profile."""
    if path is not None:
        return yaml.safe_load(path.read_text())
    if _REPO_PROFILE.exists():
        return yaml.safe_load(_REPO_PROFILE.read_text())
    packaged = resources.files("nv_config_manager_dcim_netbox.installer") / "local-netbox.yaml"
    return yaml.safe_load(packaged.read_text())


def apply_dcim(config: dict[str, Any], options: ConfigOptions) -> dict[str, Any]:
    """Point an install config at NetBox, in place.

    Shared by both paths. The kind path applies it to our own profile, where most
    of this is already true and the writes are idempotent; the Air path applies it
    to the config NVCM's own generator produced, where none of it is.
    """
    dcim = config.setdefault("dcim", {})
    dcim["provider"] = PROVIDER_NAME
    dcim["server"] = options.netbox_server
    dcim["public_url"] = options.netbox_public_url

    # The provider wheelhouse image. Without a provider_packages entry the
    # `netbox` entry point is never installed into NVCM's pods and provider
    # discovery fails. This is the block the NVCM TUI has no field for.
    packages = dcim.get("provider_packages") or [{"name": PROVIDER_NAME}]
    packages[0]["image"] = options.provider_image
    dcim["provider_packages"] = packages

    # Shared with NetBox: the plugin's nvcm_init installs this same value, so
    # neither side has to read it back from the other.
    config.setdefault("secrets", {}).setdefault("k8s", {}).setdefault("dcim", {}).setdefault(
        "values", {}
    )["token"] = options.api_token

    # NVCM generates a NATS password unless the config supplies one. Supplying
    # it lets NetBox be configured with the password before NVCM is deployed, so
    # the release does not have to be upgraded just to switch its publisher on.
    # NATS is core infrastructure and has its own secret group independently of
    # the selected DCIM provider.
    if options.nats_password:
        nats = config.setdefault("secrets", {}).setdefault("k8s", {}).setdefault("nats", {})
        nats["enabled"] = True
        nats.setdefault("values", {})["password"] = options.nats_password

    # Must match the chart's nvcm.eventSubject, which the plugin publishes to.
    dcim["event_stream"] = EVENT_STREAM
    dcim["event_subject"] = EVENT_SUBJECT

    # NetBox is the DCIM provider, so NVCM's bundled one is not deployed.
    config.setdefault("services", {})["nautobot"] = False

    return config


def apply_options(profile: dict[str, Any], options: ConfigOptions) -> dict[str, Any]:
    """Return *profile* with the computed deployment values applied.

    Mutates a copy, so the shipped profile on disk is never rewritten.
    """
    config = yaml.safe_load(yaml.safe_dump(profile))

    cluster = config.setdefault("cluster", {})
    cluster["namespace"] = options.namespace
    cluster["size"] = options.size

    return apply_dcim(config, options)


def write_config(config: dict[str, Any], path: Path) -> Path:
    """Write *config* to *path* with owner-only permissions (it holds a token)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(config, sort_keys=False))
    path.chmod(0o600)
    return path


def event_subject(config: dict[str, Any]) -> str:
    """Return the DCIM change subject the NVCM side will consume on.

    The NetBox plugin must publish to exactly this, or changes are published and
    silently never delivered.
    """
    return (config.get("dcim") or {}).get("event_subject") or "netbox.change"
