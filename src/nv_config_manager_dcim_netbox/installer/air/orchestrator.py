# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""NetBox-owned extensions to NVCM's shared DSX Air orchestration."""

from __future__ import annotations

from pathlib import Path

import yaml

from nv_config_manager_dcim_netbox.installer import config as base_config
from nv_config_manager_dcim_netbox.installer.air import AirSupportUnavailable
from nv_config_manager_dcim_netbox.installer.air.callback import NetBoxAirCallback
from nv_config_manager_dcim_netbox.installer.air.config import NetBoxSimConfig
from nv_config_manager_dcim_netbox.installer.air.images import ensure_air_images

try:
    from nv_config_manager_installer.air_sim.orchestrator import (
        OrchestratorCallback,
        SimOrchestrator,
    )
    from nv_config_manager_installer.air_sim.sim_manager import AirSimulationManager
except ImportError as exc:  # pragma: no cover - depends on the `air` extra
    raise AirSupportUnavailable(exc) from exc


class NetBoxSimOrchestrator(SimOrchestrator):
    """Install NetBox through provider lifecycle hooks around the shared Air flow."""

    def __init__(
        self,
        config: NetBoxSimConfig,
        callback: OrchestratorCallback,
        *,
        chart: Path | None = None,
        provider_deployment: NetBoxAirCallback | None = None,
    ) -> None:
        super().__init__(config, callback)
        self._provider_deployment = provider_deployment or NetBoxAirCallback(
            callback,
            config,
            chart=chart,
        )

    @property
    def api_token(self) -> str:
        """Return the token shared by NetBox and NVIDIA Config Manager."""
        return self._provider_deployment.api_token

    def _run_impl(self) -> tuple[str, int]:
        """Prepare provider-owned images before allocating an AIR simulation."""
        ensure_air_images(
            self._cfg.dcim_provider_package_image,
            f"{self._cfg.netbox_image}:{self._cfg.netbox_image_tag}",
            self._log,
        )
        return super()._run_impl()

    def _generate_install_yaml(
        self,
        cfg: NetBoxSimConfig,
        site_name: str,
        lb_allowed_prefixes: list[str],
    ) -> str:
        """Select NetBox before the common installer config is uploaded."""
        generated = yaml.safe_load(
            super()._generate_install_yaml(cfg, site_name, lb_allowed_prefixes)
        )
        configured = base_config.apply_dcim(
            generated,
            base_config.ConfigOptions(
                api_token=self._provider_deployment.api_token,
                provider_image=cfg.dcim_provider_package_image,
                netbox_server=cfg.dcim_server,
                netbox_public_url=cfg.dcim_ui_url(),
                nats_password=self._provider_deployment.nats_password,
            ),
        )
        return yaml.safe_dump(configured, default_flow_style=False, sort_keys=False)

    def _run_provider_pre_deploy(
        self,
        manager: AirSimulationManager,
        host: str,
        port: int,
    ) -> None:
        """Ship and install the NetBox chart and images before NVCM deploys."""
        self._provider_deployment._manager = manager
        self._provider_deployment.bring_up_netbox(host, port, patch_install_config=False)

    def _provider_gateway_hostnames(self, cfg: NetBoxSimConfig) -> tuple[str, ...]:
        """Publish the NetBox UI through the shared Air gateway host mapping."""
        return (cfg.netbox_hostname(),)

    def _wait_for_provider_configs(
        self,
        manager: AirSimulationManager,
        host: str,
        port: int,
        expected_total: int,
    ) -> None:
        """Wait through NetBox rather than the bundled DCIM's shell."""
        self._provider_deployment._manager = manager
        self._provider_deployment.wait_for_intended_configs(host, port, expected_total)

    def _run_provider_post_deploy(
        self,
        manager: AirSimulationManager,
        host: str,
        port: int,
    ) -> None:
        """Publish the NetBox route after NVCM installs Gateway API resources."""
        self._provider_deployment._manager = manager
        try:
            self._provider_deployment.publish_route(host, port)
        except Exception as exc:  # noqa: BLE001 - a UI route is not deployment-critical
            self._log(f"Could not publish the NetBox route: {exc}")
