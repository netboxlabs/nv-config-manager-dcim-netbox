# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""DSX Air behavior the NetBox provider supplies to NVCM's simulation wizard."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from nv_config_manager_installer.air_sim.constants import (
    CONFIG_MANAGER_HOSTNAME,
    CONFIG_MANAGER_NAMESPACE,
    NVCM_BOX_USER,
)
from nv_config_manager_installer.air_sim.provider import (
    AirInstallerPackages,
    AirProviderHooks,
    AirProviderStatus,
)
from nv_config_manager_installer.air_sim.sim_config import SimConfig
from nv_config_manager_installer.air_sim.sim_manager import AirSimulationManager
from nv_config_manager_installer.tui.air_sim.screens.population import PopulationPanel
from nv_config_manager_installer.tui.widgets import LabeledSwitch
from textual.app import ComposeResult
from textual.widgets import Label

from nv_config_manager_dcim_netbox.installer import cluster
from nv_config_manager_dcim_netbox.installer.air.callback import (
    PROVIDER_DISTRIBUTION,
    REMOTE_CHART_DIR,
    REMOTE_WHEEL_DIR,
    NetBoxAirCallback,
)
from nv_config_manager_dcim_netbox.installer.air.images import ensure_air_images
from nv_config_manager_dcim_netbox.installer.settings import (
    NetBoxAirSettings,
    NetBoxInstallerSettings,
)
from nv_config_manager_dcim_netbox.installer.tokens import (
    generate_api_token,
    generate_nats_password,
)

__all__ = [
    "NETBOX_LAUNCH_STATUS",
    "NetBoxAirHooks",
    "NetBoxAirSettings",
    "NetBoxPopulationPanel",
    "air_settings",
    "get_provisioning_status",
]

LOG = logging.getLogger(__name__)

NETBOX_HOSTNAME = f"netbox.{CONFIG_MANAGER_HOSTNAME}"

NETBOX_LAUNCH_STATUS = AirProviderStatus(
    display_name="NetBox",
    web_pod_prefix=cluster.NETBOX_DEPLOYMENT,
    access_url=f"https://{NETBOX_HOSTNAME}",
    dependent_pod_prefixes=(
        "netbox-worker",
        "nv-config-manager-render-",
        "nv-config-manager-temporal-",
        "nv-config-manager-ztp",
        "nv-config-manager-dhcp",
    ),
    excluded_web_pod_prefixes=("netbox-worker", "netbox-postgresql", "netbox-valkey"),
)

_PROVISIONING_SHELL = (
    "from netbox_nv_config_manager.models import DeviceConfigStatus;"
    "qs=DeviceConfigStatus.objects.filter(render_enabled=True).select_related('device');"
    "total=qs.count();"
    "pending=[s.device.name for s in qs if s.device.status != 'provisioned'];"
    "print(f'{total-len(pending)}/{total}|{chr(44).join(sorted(pending)[:5])}')"
)


def air_settings(cfg: SimConfig) -> NetBoxAirSettings:
    """Return the validated NetBox block of a simulation config."""
    return NetBoxAirSettings.model_validate(cfg.dcim_settings)


def provisioning_command(
    namespace: str = CONFIG_MANAGER_NAMESPACE, deployment: str = cluster.NETBOX_DEPLOYMENT
) -> str:
    """Return the remote command that prints ``provisioned/total|pending``."""
    return (
        f"KUBECONFIG=/home/{NVCM_BOX_USER}/.kube/config kubectl exec -i -n {namespace} "
        f"deployment/{deployment} -c {cluster.NETBOX_CONTAINER} -- "
        "/opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py shell -c "
        f'"{_PROVISIONING_SHELL}"'
    )


def parse_provisioning(output: str) -> tuple[int, int, list[str]] | None:
    """Parse ``provisioned/total|name,name`` from the shell output."""
    for line in output.strip().splitlines():
        if "/" not in line:
            continue
        counts, _, remaining = line.partition("|")
        provisioned, _, total = counts.partition("/")
        names = [name.strip() for name in remaining.split(",") if name.strip()]
        return int(provisioned.strip()), int(total.strip()), names
    return None


def get_provisioning_status(
    manager: AirSimulationManager, host: str, port: int
) -> tuple[int, int, list[str]]:
    """Return (provisioned, total, remaining[:5]) from NetBox, or (0, 0, []) on error."""
    try:
        result = manager.run_remote(host, port, provisioning_command(), timeout=30)
        if result.returncode == 0 and (parsed := parse_provisioning(result.stdout)):
            return parsed
    except Exception:  # noqa: BLE001 - a probe failure reads as "nothing provisioned yet"
        pass
    return 0, 0, []


class NetBoxAirHooks(AirProviderHooks):
    """Ship, install and probe NetBox around NVCM's shared AIR bringup.

    The orchestrator passes the simulation config only to the early hooks, so
    it is remembered here for the per-host hooks that follow.
    """

    def __init__(self) -> None:
        self._cfg: SimConfig | None = None
        self._log: Callable[[str], None] = LOG.info
        self._bring_up: NetBoxAirCallback | None = None
        self._api_token = ""
        self._nats_password = ""

    def _shared_secrets(self, cfg: SimConfig) -> tuple[str, str]:
        """Return the API token and NATS password used by the bring-up and NVCM."""
        if not self._api_token:
            self._api_token = air_settings(cfg).token or generate_api_token()
            self._nats_password = generate_nats_password()
        return self._api_token, self._nats_password

    def _bind(self, cfg: SimConfig) -> NetBoxAirSettings:
        self._cfg = cfg
        return air_settings(cfg)

    def _deployment(self, manager: AirSimulationManager) -> NetBoxAirCallback:
        if self._cfg is None:
            raise RuntimeError("NetBoxAirHooks: prepare_images or install_settings must run first")
        if self._bring_up is None:
            token, nats_password = self._shared_secrets(self._cfg)
            self._bring_up = NetBoxAirCallback(
                self._log,
                self._cfg,
                air_settings(self._cfg),
                manager=manager,
                api_token=token,
                nats_password=nats_password,
            )
        self._bring_up.set_manager(manager)
        return self._bring_up

    def validate_config(self, cfg: SimConfig) -> str:
        try:
            self._bind(cfg)
        except ValueError as exc:
            return f"dcim_settings is invalid: {exc}"
        return ""

    def install_settings(self, cfg: SimConfig) -> dict[str, Any]:
        """Return ``dcim.settings`` for the installer config uploaded to the box."""
        settings = self._bind(cfg)
        installed = NetBoxInstallerSettings(
            **settings.model_dump(exclude={"token"}),
        ).model_copy(update={"chart": Path(REMOTE_CHART_DIR) / settings.chart.name})
        return installed.model_dump(mode="json", exclude_defaults=True)

    def install_secrets(self, cfg: SimConfig) -> dict[str, dict[str, str]]:
        """Seed NVCM's install config with the values the bring-up installs NetBox with."""
        self._bind(cfg)
        token, nats_password = self._shared_secrets(cfg)
        return {"dcim": {"token": token}, "nats": {"password": nats_password}}

    def prepare_images(self, cfg: SimConfig, log: Callable[[str], None]) -> None:
        settings = self._bind(cfg)
        self._log = log
        ensure_air_images(settings.provider_image, settings.netbox_image, log)

    def gateway_hostnames(self, cfg: SimConfig) -> tuple[str, ...]:
        return (air_settings(cfg).netbox_hostname(CONFIG_MANAGER_HOSTNAME),)

    def pre_deploy(self, manager: AirSimulationManager, host: str, port: int) -> None:
        self._deployment(manager).bring_up_netbox(host, port)

    def installer_packages(self, cfg: SimConfig) -> AirInstallerPackages:
        return AirInstallerPackages(
            requirements=(PROVIDER_DISTRIBUTION,),
            find_links=(REMOTE_WHEEL_DIR,),
        )

    def wait_for_configs(
        self,
        manager: AirSimulationManager,
        host: str,
        port: int,
        expected_total: int,
    ) -> None:
        self._deployment(manager).wait_for_intended_configs(host, port, expected_total)

    def post_deploy(self, manager: AirSimulationManager, host: str, port: int) -> None:
        try:
            self._deployment(manager).verify_route(host, port)
        except Exception as exc:  # noqa: BLE001 - a UI route is not deployment-critical
            self._log(f"Could not verify the NetBox route: {exc}")

    def provisioning_status(
        self,
        manager: AirSimulationManager,
        host: str,
        port: int,
    ) -> tuple[int, int, list[str]]:
        return get_provisioning_status(manager, host, port)


class NetBoxPopulationPanel(PopulationPanel):
    """Configure whether the generated fabric is loaded into NetBox."""

    def compose(self) -> ComposeResult:
        yield Label("NetBox", classes="subsection-label")
        yield LabeledSwitch(
            "Load the generated topology into NetBox",
            value=air_settings(self._config).load_blueprint,
            id="load-blueprint",
        )

    def write_to_config(self, config: SimConfig) -> None:
        settings = air_settings(config).model_copy(
            update={"load_blueprint": self.query_one("#load-blueprint", LabeledSwitch).value}
        )
        config.dcim_settings = settings.model_dump(mode="json", exclude_defaults=True)

    def sync_from_config(self, config: SimConfig) -> None:
        super().sync_from_config(config)
        self.query_one("#load-blueprint", LabeledSwitch).value = air_settings(config).load_blueprint
