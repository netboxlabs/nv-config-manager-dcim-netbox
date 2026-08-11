# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Sim config with the NetBox fields NVCM's own SimConfig does not carry."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from nv_config_manager_dcim_netbox.installer import config as base_config
from nv_config_manager_dcim_netbox.installer.air import AirSupportUnavailable

try:
    from nv_config_manager_installer.air_sim.sim_config import SimConfig
except ImportError as exc:  # pragma: no cover - depends on the `air` extra
    raise AirSupportUnavailable(exc) from exc


@dataclass
class NetBoxSimConfig(SimConfig):
    """A DSX Air sim that uses NetBox as its DCIM provider.

    `SimConfig.from_yaml` filters on `dataclasses.fields(cls)`, so these extra
    keys load from the same YAML file.
    """

    # NVCM's pluggable-DCIM framework is not on main, and the sim clones this ref
    # onto the box (cloud_init.py: `git clone -b <config_manager_ref>`). Left at
    # SimConfig's `main` default the box gets a tree with no DCIM support at all.
    config_manager_ref: str = base_config.NVCM_BRANCH

    dcim_provider: str = base_config.PROVIDER_NAME

    # Built by `make images`; neither image exists in a registry, so both are
    # shipped into the sim's kind cluster over SSH.
    dcim_provider_package_image: str = "nv-config-manager-dcim-netbox:0.1.0"
    netbox_image: str = "nv-config-manager-netbox"
    netbox_image_tag: str = "0.1.0"

    # Generated when empty, then installed into NetBox and handed to NVCM, so
    # neither side has to read it back from the other.
    dcim_token: str = ""

    # In-cluster Service published by deploy/netbox (fullnameOverride: netbox).
    dcim_server: str = "http://netbox"
    dcim_public_url: str = ""

    netbox_release: str = "netbox"
    netbox_chart: Path | None = None

    # Load the sim's topology into NetBox with the plugin's nvcm_load_dcim. Off
    # means an empty NetBox, which renders nothing; only turn it off when
    # pointing at a NetBox that already holds the fabric.
    load_blueprint: bool = True

    # Extra `--set` arguments for the NetBox chart, for one-off overrides.
    netbox_helm_set: list[str] = field(default_factory=list)

    def blueprint_dir(self, remote_repo: str) -> str:
        """Path on the sim box to this sim's topology blueprint.

        cloud-init clones the NVCM repo onto the box, so it is already there.
        """
        return f"{remote_repo}/development/mock_topology/context/{self.mock_blueprint}"
