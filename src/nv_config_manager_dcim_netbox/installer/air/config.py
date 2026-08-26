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

    # The sim clones this ref onto the box (cloud_init.py:
    # `git clone -b <config_manager_ref>`). Bound to NVCM_BRANCH rather than left
    # to SimConfig so both resolve from one place if it ever moves off main again.
    config_manager_ref: str = base_config.NVCM_BRANCH

    # Build the simulated fabric from the shared blueprint, but do not register
    # the bundled Nautobot population job. NetBox loads the blueprint through
    # its own management command during provider deployment.
    generate_fabric_from_mock_context: bool | None = True
    run_mock_topology_job: bool = False

    dcim_provider: str = base_config.PROVIDER_NAME

    # Neither image exists in a registry, so the AIR orchestrator builds both for
    # the simulation architecture and ships them into its kind cluster over SSH.
    # Tags track the package version.
    dcim_provider_package_image: str = base_config.PROVIDER_IMAGE
    netbox_image: str = base_config.NETBOX_IMAGE
    netbox_image_tag: str = base_config.IMAGE_TAG

    # Generated when empty, then installed into NetBox and handed to NVCM, so
    # neither side has to read it back from the other.
    dcim_token: str = ""

    # In-cluster Service published by deploy/netbox (fullnameOverride: netbox).
    dcim_server: str = "http://netbox"
    dcim_public_url: str = ""

    netbox_release: str = "netbox"
    netbox_chart: Path | None = None

    # AIR simulations are disposable demo environments, so use the same
    # predictable UI login as the bundled demo provider. Normal installer
    # deployments continue to let the chart generate their credentials.
    netbox_superuser_name: str = "demo"
    netbox_superuser_password: str = "demo"

    # Load the sim's topology into NetBox with the plugin's nvcm_load_dcim. Off
    # means an empty NetBox, which renders nothing; only turn it off when
    # pointing at a NetBox that already holds the fabric.
    load_blueprint: bool = True

    # Extra `--set` arguments for the NetBox chart, for one-off overrides.
    netbox_helm_set: list[str] = field(default_factory=list)

    def gateway_overrides(self) -> list[str]:
        """Chart `--set` arguments that publish NetBox on the sim's Gateway.

        A sim serves NVCM at `nvcm.air`, not the `config-manager.local` a kind
        cluster uses, so the chart's default hostname would put NetBox on a name
        nothing resolves. Taken from NVCM's own constant so the two cannot drift.

        Skipped when the caller has already set either key in netbox_helm_set,
        which is appended after these and would otherwise silently conflict.
        """
        from nv_config_manager_installer.air_sim.constants import CONFIG_MANAGER_HOSTNAME

        if any(
            "gateway.baseHostname" in s or "gateway.hostname" in s for s in self.netbox_helm_set
        ):
            return []
        return [f"nvcm.gateway.baseHostname={CONFIG_MANAGER_HOSTNAME}"]

    def netbox_hostname(self) -> str:
        """The name NetBox is published under on the sim."""
        from nv_config_manager_installer.air_sim.constants import CONFIG_MANAGER_HOSTNAME

        return f"netbox.{CONFIG_MANAGER_HOSTNAME}"

    def dcim_ui_url(self) -> str:
        """Return the browser-facing NetBox URL persisted for UI links."""
        return self.dcim_public_url or f"https://{self.netbox_hostname()}"

    def blueprint_dir(self, remote_repo: str) -> str:
        """Path on the sim box to this sim's topology blueprint.

        cloud-init clones the NVCM repo onto the box, so it is already there.
        """
        return f"{remote_repo}/development/mock_topology/context/{self.mock_blueprint}"
