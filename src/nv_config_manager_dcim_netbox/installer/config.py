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
from pathlib import Path
from typing import Any

import yaml

# Where the profile lives when running from a source checkout. Packaged copies
# are found through importlib.resources instead.
_REPO_PROFILE = Path(__file__).resolve().parents[3] / "deploy" / "configs" / "local-netbox.yaml"

TOKEN_PLACEHOLDER = "REPLACED_BY_INSTALLER"


@dataclass(frozen=True)
class ConfigOptions:
    """Values `up`/`config` compute or take from flags; everything else is profile."""

    api_token: str
    namespace: str = "nv-config-manager"
    size: str = "small"
    provider_image: str = "nv-config-manager-dcim-netbox:0.1.0"
    netbox_server: str = "http://netbox"
    netbox_public_url: str = "http://127.0.0.1:8080"


def load_profile(path: Path | None = None) -> dict[str, Any]:
    """Load the base installer profile."""
    if path is not None:
        return yaml.safe_load(path.read_text())
    if _REPO_PROFILE.exists():
        return yaml.safe_load(_REPO_PROFILE.read_text())
    packaged = resources.files("nv_config_manager_dcim_netbox.installer") / "local-netbox.yaml"
    return yaml.safe_load(packaged.read_text())


def apply_options(profile: dict[str, Any], options: ConfigOptions) -> dict[str, Any]:
    """Return *profile* with the computed deployment values applied.

    Mutates a copy, so the shipped profile on disk is never rewritten.
    """
    config = yaml.safe_load(yaml.safe_dump(profile))

    cluster = config.setdefault("cluster", {})
    cluster["namespace"] = options.namespace
    cluster["size"] = options.size

    dcim = config.setdefault("dcim", {})
    dcim["server"] = options.netbox_server
    dcim["public_url"] = options.netbox_public_url

    # The provider wheelhouse image. Without a provider_packages entry the
    # `netbox` entry point is never installed into NVCM's pods and provider
    # discovery fails — this is the block the NVCM TUI has no field for.
    packages = dcim.get("provider_packages") or [{"name": "netbox"}]
    packages[0]["image"] = options.provider_image
    dcim["provider_packages"] = packages

    # Shared with NetBox: the plugin's nvcm_init installs this same value, so
    # neither side has to read it back from the other.
    config.setdefault("secrets", {}).setdefault("k8s", {}).setdefault("dcim", {}).setdefault(
        "values", {}
    )["token"] = options.api_token

    return config


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
