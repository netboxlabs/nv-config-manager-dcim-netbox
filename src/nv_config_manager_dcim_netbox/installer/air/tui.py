# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""NetBox adapter for NVCM's shared DSX Air simulation TUI."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import ClassVar

from nv_config_manager_dcim_netbox.installer import cluster
from nv_config_manager_dcim_netbox.installer.air import AirSupportUnavailable
from nv_config_manager_dcim_netbox.installer.air.callback import NAMESPACE
from nv_config_manager_dcim_netbox.installer.air.config import NetBoxSimConfig
from nv_config_manager_dcim_netbox.installer.air.orchestrator import NetBoxSimOrchestrator

# textual belongs in here with the rest: it reaches us through
# nv-config-manager-installer, so without the `air` extra it is absent too, and
# importing it above would raise a bare ImportError that `air --help` cannot catch.
try:
    from nv_config_manager_installer.air_sim.orchestrator import OrchestratorCallback
    from nv_config_manager_installer.air_sim.sim_config import SimConfig
    from nv_config_manager_installer.air_sim.sim_manager import AirSimulationManager
    from nv_config_manager_installer.tui.air_sim.app import NVCMAirSimApp
    from nv_config_manager_installer.tui.air_sim.screens.launch import (
        AirProviderStatus,
        LaunchScreen,
    )
    from nv_config_manager_installer.tui.air_sim.screens.topology import (
        PopulationPanel,
        TopologyScreen,
    )
    from nv_config_manager_installer.tui.widgets import LabeledSwitch
    from textual.app import ComposeResult
    from textual.widgets import Label
except ImportError as exc:  # pragma: no cover - depends on the `air` extra
    raise AirSupportUnavailable(exc) from exc


NETBOX_PROVIDER_STATUS = AirProviderStatus(
    display_name="NetBox",
    web_pod_prefix="netbox",
    access_url="https://netbox.nvcm.air",
    dependent_pod_prefixes=(
        "netbox-worker",
        "nv-config-manager-render-",
        "nv-config-manager-temporal-",
        "nv-config-manager-ztp",
        "nv-config-manager-dhcp",
    ),
    excluded_web_pod_prefixes=("netbox-worker", "netbox-postgresql", "netbox-valkey"),
)

_NETBOX_PROVISIONING_SHELL = (
    "from netbox_nv_config_manager.models import DeviceConfigStatus;"
    "qs=DeviceConfigStatus.objects.filter(render_enabled=True).select_related('device');"
    "total=qs.count();"
    "pending=[s.device.name for s in qs if s.device.status != 'provisioned'];"
    "print(f'{total-len(pending)}/{total}|{chr(44).join(sorted(pending)[:5])}')"
)


class NetBoxPopulationPanel(PopulationPanel):
    """Configure whether the generated fabric is loaded into NetBox."""

    def compose(self) -> ComposeResult:
        yield Label("NetBox", classes="subsection-label")
        yield LabeledSwitch(
            "Load the generated topology into NetBox",
            value=self._netbox_config.load_blueprint,
            id="load-blueprint",
        )

    @property
    def _netbox_config(self) -> NetBoxSimConfig:
        if not isinstance(self._config, NetBoxSimConfig):
            raise TypeError("NetBoxPopulationPanel requires NetBoxSimConfig")
        return self._config

    def write_to_config(self, config: SimConfig) -> None:
        if not isinstance(config, NetBoxSimConfig):
            raise TypeError("NetBoxPopulationPanel requires NetBoxSimConfig")
        config.load_blueprint = self.query_one("#load-blueprint", LabeledSwitch).value

    def sync_from_config(self, config: SimConfig) -> None:
        super().sync_from_config(config)
        if not isinstance(config, NetBoxSimConfig):
            raise TypeError("NetBoxPopulationPanel requires NetBoxSimConfig")
        self.query_one("#load-blueprint", LabeledSwitch).value = config.load_blueprint


class NetBoxTopologyScreen(TopologyScreen):
    """Shared topology screen with NetBox-owned population controls."""

    POPULATION_PANEL_CLASS = NetBoxPopulationPanel


class NetBoxAirSimulationManager(AirSimulationManager):
    """Report switch provisioning through NetBox."""

    def get_provisioning_status(
        self,
        host: str,
        port: int,
        namespace: str = NAMESPACE,
        deployment: str = cluster.NETBOX_DEPLOYMENT,
    ) -> tuple[int, int, list[str]]:
        ssh_base = self._ssh_cmd(host, port)
        command = (
            f"KUBECONFIG=/home/nvcm/.kube/config kubectl exec -i -n {namespace} "
            f"deployment/{deployment} -c {cluster.NETBOX_CONTAINER} -- "
            "/opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py shell -c "
            f'"{_NETBOX_PROVISIONING_SHELL}"'
        )
        try:
            result = subprocess.run(
                [*ssh_base, command],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                for line in result.stdout.strip().splitlines():
                    if "/" not in line:
                        continue
                    counts, _, remaining = line.partition("|")
                    provisioned, _, total = counts.partition("/")
                    names = [name.strip() for name in remaining.split(",") if name.strip()]
                    return int(provisioned.strip()), int(total.strip()), names
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        return 0, 0, []


class NetBoxLaunchScreen(LaunchScreen):
    """Run the NetBox orchestrator and display NetBox readiness."""

    PROVIDER_STATUS = NETBOX_PROVIDER_STATUS

    def create_orchestrator(self, callback: OrchestratorCallback) -> NetBoxSimOrchestrator:
        if not isinstance(self._config, NetBoxSimConfig):
            raise TypeError("NetBoxLaunchScreen requires NetBoxSimConfig")
        return NetBoxSimOrchestrator(self._config, callback)

    def create_simulation_manager(self) -> NetBoxAirSimulationManager:
        return NetBoxAirSimulationManager(
            ngc_api_key=self._config.ngc_api_key,
            use_internal=self._config.use_internal,
            org_id=self._config.org_id,
            ssh_password=self._config.oob_ssh_password,
        )

    def validate_provider_config(self) -> str:
        return ""


class NetBoxAirSimApp(NVCMAirSimApp):
    """DSX Air simulation wizard backed by NetBox."""

    TITLE = "NVCM NetBox DSX Air Sim Wizard"
    CONFIG_MODEL = NetBoxSimConfig
    SCREEN_CLASSES: ClassVar = {
        **NVCMAirSimApp.SCREEN_CLASSES,
        "topology": NetBoxTopologyScreen,
        "launch": NetBoxLaunchScreen,
    }


def run(config_path: Path) -> None:
    """Load a NetBox simulation config and launch its TUI."""
    config = NetBoxSimConfig.load_or_default(config_path)
    NetBoxAirSimApp(config=config, config_path=config_path).run()
