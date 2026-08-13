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
from nv_config_manager_dcim_netbox.installer.remote import ScriptRunner, SshRunner
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

# Echoed by the bring-up script on success. NVCM streams a remote command and
# watches for a marker like this to tell completion from a dropped connection.
BRING_UP_MARKER = "NVCM_NETBOX_BRING_UP_COMPLETE"
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
            try:
                self.publish_hostname(host or self.host, port or self.port)
            except Exception as exc:  # noqa: BLE001 - cosmetic, never fail the deploy
                self.on_log(f"Could not add {self.cfg.netbox_hostname()} to /etc/hosts: {exc}")
        self._inner.on_complete(success, host, port)

    # -- the work ------------------------------------------------------------

    def runner(self, host: str, port: int) -> SshRunner:
        return SshRunner(self._manager, host, port)

    def bring_up_netbox(self, host: str, port: int) -> None:
        """Ship the images and chart, then run the whole bring-up in one session.

        Modelled on NVCM's own deploy, which uploads what it needs and then runs a
        single command on the box, streaming the output back. Driving the box a
        command at a time costs an authenticated SSH connection each, and any one
        of them can catch a freshly booted sshd mid-blip and lose the run.
        """
        self.check_image_arch(host, port)

        self.on_log("Shipping NetBox and provider images into the sim...")
        images_tar = self.upload_images(host, port)

        self.on_log("Uploading the NetBox chart...")
        chart_tar = self.upload_chart(host, port)

        self.on_log("Deploying NetBox...")
        self.run_script(host, port, self.bring_up_script(images_tar, chart_tar))

        self.on_log("Pointing NVCM's install config at NetBox...")
        self.patch_install_config(host, port)

    def bring_up_script(self, images_tar: str, chart_tar: str) -> str:
        """Render every on-box step as one script.

        Built by running the same `cluster` functions the kind path uses against a
        recording runner, so the two cannot drift.
        """
        chart_dir = f"/home/{NVCM_BOX_USER}/charts"
        remote_chart = f"{chart_dir}/{self.chart.name}"

        script = ScriptRunner()
        script.run(["kind", "load", "image-archive", images_tar, "--name", KIND_CLUSTER])
        script.run(["rm", "-f", images_tar])
        script.run(
            [
                "sh",
                "-c",
                f"rm -rf {shlex.quote(chart_dir)} && mkdir -p {shlex.quote(chart_dir)} "
                f"&& tar -xzf {shlex.quote(chart_tar)} -C {shlex.quote(chart_dir)} "
                f"&& rm -f {shlex.quote(chart_tar)}",
            ]
        )
        cluster.install_netbox(
            remote_chart,
            release=self.cfg.netbox_release,
            namespace=NAMESPACE,
            api_token=self.api_token,
            image_repository=self.cfg.netbox_image,
            image_tag=self.cfg.netbox_image_tag,
            extra_set=[*self.cfg.gateway_overrides(), *self.cfg.netbox_helm_set],
            runner=script,
        )
        cluster.wait_for_netbox(NAMESPACE, runner=script)
        cluster.run_nvcm_init(NAMESPACE, self.api_token, runner=script)
        if self.cfg.load_blueprint:
            cluster.load_topology(
                self.cfg.blueprint_dir(CONFIG_MANAGER_REMOTE_DIR),
                NAMESPACE,
                deployment_name=self.cfg.deployment_name or self.cfg.mock_blueprint,
                runner=script,
            )
        return script.script(marker=BRING_UP_MARKER, redact=[self.api_token])

    def run_script(self, host: str, port: int, body: str) -> None:
        """Upload the script and run it in a single SSH session.

        Uses NVCM's own `_ssh_run_and_tail`, which streams the output back and
        watches for a completion marker; that is how their deploy reports
        progress, and it means one authentication for the whole bring-up.

        The script holds the API token, so it is written 0600 and removed after.
        """
        remote = f"/home/{NVCM_BOX_USER}/nvcm-netbox-bring-up.sh"
        with tempfile.TemporaryDirectory() as tmpdir:
            local = Path(tmpdir) / "nvcm-netbox-bring-up.sh"
            local.write_text(body)
            local.chmod(0o600)
            self.runner(host, port).upload(local, remote)

        ok = self._manager._ssh_run_and_tail(
            host,
            port,
            f"chmod 600 {shlex.quote(remote)} && bash {shlex.quote(remote)}; "
            f"rc=$?; rm -f {shlex.quote(remote)}; exit $rc",
            marker=BRING_UP_MARKER,
            label="netbox",
            timeout=self.cfg.deploy_timeout or 3600,
        )
        if not ok:
            raise RuntimeError(
                "The NetBox bring-up did not finish. Its output is above; the failing "
                "command is the last one echoed."
            )

    def publish_hostname(self, host: str, port: int) -> None:
        """Resolve NetBox's hostname on the sim box, next to NVCM's own.

        NVCM's post-deploy writes one /etc/hosts line pointing its hostnames at
        the Gateway's address, but that list is fixed and has no NetBox entry, so
        `https://netbox.<hostname>` would not resolve through the SOCKS proxy the
        documented browser flow uses.

        The address is read back from the line NVCM just wrote rather than
        rediscovered, so the two can never disagree. Appended as its own line,
        which their `grep -q` guard leaves alone.
        """
        hostname = self.cfg.netbox_hostname()
        base = hostname.split(".", 1)[1]
        script = (
            f"ip=$(awk '/[[:space:]]{base.replace('.', chr(92) + '.')}([[:space:]]|$)/"
            " {print $1; exit}' /etc/hosts); "
            'if [ -z "$ip" ]; then echo "no NVCM hosts entry yet" >&2; exit 1; fi; '
            f"grep -q {shlex.quote(hostname)} /etc/hosts "
            f'|| echo "$ip {hostname}" | sudo tee -a /etc/hosts > /dev/null'
        )
        self.runner(host, port).run(["sh", "-c", script])
        self.on_log(f"NetBox is at https://{hostname} on the sim.")

    def check_image_arch(self, host: str, port: int) -> None:
        """Refuse to ship images the sim box cannot execute.

        `make images` builds for the host, and a sim box is x86_64, so building on
        Apple Silicon produces arm64 images. Loaded onto the box they start and
        immediately die with "exec format error", which surfaces ten minutes later
        as a helm timeout with nothing pointing at the cause.
        """
        want = self.runner(host, port).run(["uname", "-m"]).strip()
        want = {"x86_64": "amd64", "aarch64": "arm64"}.get(want, want)

        images = [
            self.cfg.dcim_provider_package_image,
            f"{self.cfg.netbox_image}:{self.cfg.netbox_image_tag}",
        ]
        wrong = []
        for image in images:
            try:
                got = run(["docker", "image", "inspect", image, "--format", "{{.Architecture}}"])
            except Exception:  # noqa: BLE001 - reported as a missing image below
                wrong.append(f"  {image}: not built")
                continue
            if got.strip() != want:
                wrong.append(f"  {image}: {got.strip()}")
        if wrong:
            raise RuntimeError(
                f"The sim box is {want}, but these images are not:\n"
                + "\n".join(wrong)
                + f"\n\nRebuild them for it:\n  make images PLATFORM=linux/{want}"
            )

    def upload_images(self, host: str, port: int) -> str:
        """`docker save` both images and upload the archive. Returns its path.

        Neither image is in a registry and both are pulled IfNotPresent, so they
        must already be on the node. `kind load` runs later, in the script.
        """
        images = [
            self.cfg.dcim_provider_package_image,
            f"{self.cfg.netbox_image}:{self.cfg.netbox_image_tag}",
        ]
        remote = f"/home/{NVCM_BOX_USER}/nvcm-netbox-images.tar"
        with tempfile.NamedTemporaryFile(suffix=".tar", prefix="nvcm-netbox-") as tmp:
            archive = Path(tmp.name)
            run(["docker", "save", *images, "-o", str(archive)])
            self.runner(host, port).upload(archive, remote)
        return remote

    def upload_chart(self, host: str, port: int) -> str:
        """Upload the chart with its dependencies vendored. Returns its path.

        Vendored so the sim never has to reach the chart registry. Unpacking
        happens later, in the script.
        """
        cluster.helm_dependency_build(self.chart)
        remote_tar = f"/home/{NVCM_BOX_USER}/netbox-chart.tgz"
        with tempfile.TemporaryDirectory() as tmpdir:
            archive = Path(tmpdir) / "netbox-chart.tgz"
            run(["tar", "-czf", str(archive), "-C", str(self.chart.parent), self.chart.name])
            self.runner(host, port).upload(archive, remote_tar)
        return remote_tar

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
            extra_set=[*self.cfg.gateway_overrides(), *self.cfg.netbox_helm_set],
            runner=runner,
        )
        self.on_log("NetBox is publishing change events to NVCM.")
