# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Deploy NetBox into a DSX Air sim, driven by NVCM's orchestrator callbacks.

`on_deploy_started` fires after the install config is uploaded and before the
deploy runs; `on_complete` after everything.
"""

from __future__ import annotations

import shlex
import tempfile
from pathlib import Path

import yaml

from nv_config_manager_dcim_netbox.installer import cluster
from nv_config_manager_dcim_netbox.installer import config as base_config
from nv_config_manager_dcim_netbox.installer.air import AirSupportUnavailable
from nv_config_manager_dcim_netbox.installer.air.config import NetBoxSimConfig
from nv_config_manager_dcim_netbox.installer.remote import SshRunner
from nv_config_manager_dcim_netbox.installer.shell import run
from nv_config_manager_dcim_netbox.installer.tokens import generate_api_token

try:
    from nv_config_manager_installer.air_sim.constants import (
        CONFIG_MANAGER_INSTALL_CONFIG,
        CONFIG_MANAGER_REMOTE_DIR,
        NVCM_BOX_USER,
    )
    from nv_config_manager_installer.air_sim.sim_manager import AirSimulationManager
except ImportError as exc:  # pragma: no cover - depends on the `air` extra
    raise AirSupportUnavailable(exc) from exc

# cloud-init creates the cluster with this name.
KIND_CLUSTER = "nvcm"
NAMESPACE = "nv-config-manager"

_REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_CHART = _REPO_ROOT / "deploy" / "netbox"


class NetBoxAirCallback:
    """Wraps the CLI's callback and adds the NetBox bring-up.

    Every method delegates to the wrapped callback, so the caller's logging and
    step reporting still work.
    """

    def __init__(
        self,
        inner,
        cfg: NetBoxSimConfig,
        chart: Path | None = None,
        manager=None,
    ) -> None:
        self._inner = inner
        self.cfg = cfg
        self.chart = chart or cfg.netbox_chart or DEFAULT_CHART
        self.api_token = cfg.dcim_token or generate_api_token()
        self.host = ""
        self.port = 0
        # Injectable so the bring-up can be tested without a simulation.
        self._manager = manager or AirSimulationManager(
            ngc_api_key=cfg.ngc_api_key or None,
            org_id=cfg.org_id or None,
            use_internal=cfg.use_internal,
            ssh_password=cfg.oob_ssh_password,
        )

    # -- OrchestratorCallback ------------------------------------------------

    def on_step(self, step_id, status, message: str = "") -> None:
        self._inner.on_step(step_id, status, message)

    def on_log(self, line: str) -> None:
        self._inner.on_log(line)

    def on_ssh_ready(self, host: str, port: int) -> None:
        self.host, self.port = host, port
        self._inner.on_ssh_ready(host, port)

    def on_deploy_started(self, host: str, port: int) -> None:
        """Everything NetBox, in the window before NVCM deploys."""
        self.host, self.port = host, port
        try:
            self.bring_up_netbox(host, port)
        finally:
            self._inner.on_deploy_started(host, port)

    def on_complete(self, success: bool, host: str = "", port: int = 0) -> None:
        # NVCM's deployer creates the NATS password, so the publisher can only be
        # switched on now. Until this runs NetBox logs envelopes instead.
        if success:
            try:
                self.connect_event_bus(host or self.host, port or self.port)
            except Exception as exc:  # noqa: BLE001 - never fail a good deploy here
                self.on_log(f"Could not connect NetBox to the event bus: {exc}")
                self.on_log("Re-run `nvcm-netbox air connect` once the cluster settles.")
        self._inner.on_complete(success, host, port)

    # -- the work ------------------------------------------------------------

    def runner(self, host: str, port: int) -> SshRunner:
        return SshRunner(self._manager, host, port)

    def bring_up_netbox(self, host: str, port: int) -> None:
        """Ship the images, deploy NetBox, initialise it, load the topology."""
        runner = self.runner(host, port)
        self.on_log("Shipping NetBox and provider images into the sim...")
        self.load_images(host, port)

        self.on_log("Deploying NetBox...")
        remote_chart = self.upload_chart(host, port)
        cluster.install_netbox(
            remote_chart,
            release=self.cfg.netbox_release,
            namespace=NAMESPACE,
            api_token=self.api_token,
            image_repository=self.cfg.netbox_image,
            image_tag=self.cfg.netbox_image_tag,
            extra_set=self.cfg.netbox_helm_set,
            runner=runner,
        )
        cluster.wait_for_netbox(NAMESPACE, runner=runner)

        self.on_log("Initialising NetBox...")
        cluster.run_nvcm_init(NAMESPACE, self.api_token, runner=runner)

        if self.cfg.load_blueprint:
            self.on_log(f"Loading the {self.cfg.mock_blueprint} topology into NetBox...")
            self.load_topology(host, port)

        self.on_log("Pointing NVCM's install config at NetBox...")
        self.patch_install_config(host, port)

    def load_images(self, host: str, port: int) -> None:
        """`docker save` both images, upload once, `kind load` on the box.

        Neither image is in a registry and both are pulled IfNotPresent, so they
        must already be on the node.
        """
        images = [
            self.cfg.dcim_provider_package_image,
            f"{self.cfg.netbox_image}:{self.cfg.netbox_image_tag}",
        ]
        with tempfile.NamedTemporaryFile(suffix=".tar", prefix="nvcm-netbox-") as tmp:
            archive = Path(tmp.name)
            run(["docker", "save", *images, "-o", str(archive)])
            remote = f"/home/{NVCM_BOX_USER}/nvcm-netbox-images.tar"
            self.runner(host, port).upload(archive, remote)
        self.runner(host, port).run(
            ["kind", "load", "image-archive", remote, "--name", KIND_CLUSTER]
        )
        self.runner(host, port).run(["rm", "-f", remote])

    def upload_chart(self, host: str, port: int) -> str:
        """Upload the chart with its dependencies vendored, so the sim does not
        have to reach the chart registry."""
        cluster.helm_dependency_build(self.chart)
        with tempfile.TemporaryDirectory() as tmpdir:
            archive = Path(tmpdir) / "netbox-chart.tgz"
            run(
                [
                    "tar",
                    "-czf",
                    str(archive),
                    "-C",
                    str(self.chart.parent),
                    self.chart.name,
                ]
            )
            remote_tar = f"/home/{NVCM_BOX_USER}/netbox-chart.tgz"
            self.runner(host, port).upload(archive, remote_tar)
        remote_dir = f"/home/{NVCM_BOX_USER}/charts"
        self.runner(host, port).run(
            [
                "sh",
                "-c",
                f"rm -rf {shlex.quote(remote_dir)} && mkdir -p {shlex.quote(remote_dir)} "
                f"&& tar -xzf {shlex.quote(remote_tar)} -C {shlex.quote(remote_dir)} "
                f"&& rm -f {shlex.quote(remote_tar)}",
            ]
        )
        return f"{remote_dir}/{self.chart.name}"

    def load_topology(self, host: str, port: int) -> None:
        """Load the sim's blueprint into NetBox with the plugin's loader.

        The blueprint is already on the box in the NVCM clone; it just has to
        reach the pod.
        """
        cluster.load_topology(
            self.cfg.blueprint_dir(CONFIG_MANAGER_REMOTE_DIR),
            NAMESPACE,
            deployment_name=self.cfg.deployment_name or self.cfg.mock_blueprint,
            runner=self.runner(host, port),
        )

    def patch_install_config(self, host: str, port: int) -> None:
        """Rewrite the uploaded install config to select NetBox.

        Only the DCIM fields change; the rest of the file stays as NVCM's
        generator wrote it.
        """
        runner = self.runner(host, port)
        remote = f"/home/{NVCM_BOX_USER}/{CONFIG_MANAGER_INSTALL_CONFIG}"
        current = yaml.safe_load(runner.run(["cat", remote])) or {}
        patched = base_config.apply_dcim(
            current,
            base_config.ConfigOptions(
                api_token=self.api_token,
                provider_image=self.cfg.dcim_provider_package_image,
                netbox_server=self.cfg.dcim_server,
                netbox_public_url=self.cfg.dcim_public_url or self.cfg.dcim_server,
            ),
        )
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as tmp:
            yaml.safe_dump(patched, tmp, sort_keys=False)
            local = Path(tmp.name)
        try:
            runner.upload(local, remote)
        finally:
            local.unlink(missing_ok=True)

    def connect_event_bus(self, host: str, port: int) -> None:
        """Re-run the NetBox release with the NATS password NVCM created."""
        runner = self.runner(host, port)
        password = cluster.read_nats_password(NAMESPACE, runner=runner)
        if not password:
            self.on_log("No NATS secret yet; NetBox stays on the log publisher.")
            return
        cluster.install_netbox(
            f"/home/{NVCM_BOX_USER}/charts/{self.chart.name}",
            release=self.cfg.netbox_release,
            namespace=NAMESPACE,
            api_token=self.api_token,
            image_repository=self.cfg.netbox_image,
            image_tag=self.cfg.netbox_image_tag,
            nats_password=password,
            extra_set=self.cfg.netbox_helm_set,
            runner=runner,
        )
        self.on_log("NetBox is publishing change events to NVCM.")
