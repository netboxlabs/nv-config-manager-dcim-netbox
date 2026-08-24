# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Deploy NetBox into a DSX Air sim, driven by NVCM's orchestrator callbacks.

`on_deploy_started` fires after the install config is uploaded and before the
deploy runs; `on_complete` after everything.
"""

from __future__ import annotations

import shlex
import tempfile
import time
from pathlib import Path

import yaml

from nv_config_manager_dcim_netbox.installer import cluster
from nv_config_manager_dcim_netbox.installer import config as base_config
from nv_config_manager_dcim_netbox.installer.air import AirSupportUnavailable
from nv_config_manager_dcim_netbox.installer.air.config import NetBoxSimConfig
from nv_config_manager_dcim_netbox.installer.remote import ScriptRunner, SshRunner
from nv_config_manager_dcim_netbox.installer.shell import CommandError, run
from nv_config_manager_dcim_netbox.installer.tokens import (
    generate_api_token,
    generate_nats_password,
)

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

# Echoed on success by the *wrapper* around the bring-up script. NVCM streams a
# remote command and watches for a marker like this to tell completion from a
# dropped connection.
BRING_UP_MARKER = "NVCM_NETBOX_BRING_UP_COMPLETE"

# Echoed by the script body itself, and watched for by nothing. `_ssh_run_and_tail`
# terminates the SSH process the moment it sees the marker it was given, so any
# command sequenced after the marker-producing one never runs. The two are kept
# distinct so the wrapper's cleanup happens before the marker the tail is
# watching for is emitted.
SCRIPT_DONE_MARKER = "NVCM_NETBOX_SCRIPT_DONE"
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
        # Supplied to NVCM in the install config, so NetBox is installed with it
        # rather than upgraded again once NVCM's deployer has minted one.
        self.nats_password = generate_nats_password()
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
        # NetBox already holds the NATS password (see __init__), so the publisher
        # needs nothing here. Its HTTPRoute does: that could not render during
        # bring-up because NVCM's deploy had not yet registered the Gateway API
        # CRDs, and this is the first point at which it has.
        box, box_port = host or self.host, port or self.port
        if success and self._netbox_is_deployed(box, box_port):
            for label, action in (
                ("publish the NetBox route", self.publish_route),
                (f"add {self.cfg.netbox_hostname()} to /etc/hosts", self.publish_hostname),
            ):
                try:
                    action(box, box_port)
                except Exception as exc:  # noqa: BLE001 - cosmetic, never fail the deploy
                    self.on_log(f"Could not {label}: {exc}")
        elif success:
            self.on_log(
                "NetBox is not on the sim's cluster, so its route and /etc/hosts entry "
                "were skipped. Nothing was deployed — see the failed step above."
            )
        self._inner.on_complete(success, host, port)

    def _netbox_is_deployed(self, host: str, port: int) -> bool:
        """Whether NetBox actually reached the sim's cluster.

        ``success`` does not answer this: NVCM's orchestrator reports the run as
        successful even when `wait-setup` failed and it therefore skipped
        `upload-files` and `run-deploy` (``orchestrator.run`` sets ``success =
        True`` whenever ``_run_impl`` returns).
        """
        try:
            return cluster.netbox_service_exists(NAMESPACE, runner=self.runner(host, port))
        except Exception:  # noqa: BLE001 - an unreachable box is "not deployed" too
            return False

    # -- the work ------------------------------------------------------------

    def runner(self, host: str, port: int) -> SshRunner:
        return SshRunner(self._manager, host, port)

    def bring_up_netbox(
        self,
        host: str,
        port: int,
        *,
        patch_install_config: bool = True,
    ) -> None:
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

        if patch_install_config:
            self.on_log("Pointing NVCM's install config at NetBox...")
            self.patch_install_config(host, port)

    def wait_for_intended_configs(
        self,
        host: str,
        port: int,
        expected_total: int,
        *,
        timeout: int = 180,
        interval: int = 10,
    ) -> bool:
        """Wait until NetBox has intended-config records for render-enabled devices."""
        if expected_total <= 0:
            return True

        python_code = (
            "from netbox_nv_config_manager.models import DeviceConfigStatus, IntendedConfig;"
            "ready=IntendedConfig.objects.filter("
            "device__nvcm_config_status__render_enabled=True).count();"
            "total=DeviceConfigStatus.objects.filter(render_enabled=True).count();"
            "print(f'{ready}/{total}')"
        )
        command = [
            "kubectl",
            "exec",
            "-n",
            NAMESPACE,
            "deployment/netbox",
            "-c",
            "netbox",
            "--",
            "/opt/netbox/venv/bin/python",
            "/opt/netbox/netbox/manage.py",
            "shell",
            "-c",
            python_code,
        ]
        deadline = time.monotonic() + timeout
        last_counts = "0/0"
        while time.monotonic() < deadline:
            try:
                output = self.runner(host, port).run(command).strip()
                ready_text, separator, total_text = output.rpartition("\n")[-1].partition("/")
                if separator:
                    ready = int(ready_text)
                    total = int(total_text)
                    last_counts = f"{ready}/{total}"
                    if ready >= expected_total:
                        self.on_log(f"NetBox intended configs ready: {last_counts}")
                        return True
            except (CommandError, TypeError, ValueError):
                pass
            time.sleep(interval)

        self.on_log(f"Timed out waiting for NetBox intended configs; last count: {last_counts}")
        return False

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
            nats_password=self.nats_password,
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
        return script.script(marker=SCRIPT_DONE_MARKER, redact=[self.api_token])

    def run_script(self, host: str, port: int, body: str) -> None:
        """Upload the script and run it in a single SSH session.

        Uses NVCM's own `_ssh_run_and_tail`, which streams the output back and
        watches for a completion marker; that is how their deploy reports
        progress, and it means one authentication for the whole bring-up.

        The script holds the API token and the NATS password, so it is written 0600
        and deleted before the run reports completion -- the wrapper echoes
        BRING_UP_MARKER only after the `rm`, because the tail kills the process as
        soon as it sees that marker (see SCRIPT_DONE_MARKER).
        """
        remote = f"/home/{NVCM_BOX_USER}/nvcm-netbox-bring-up.sh"
        with tempfile.TemporaryDirectory() as tmpdir:
            local = Path(tmpdir) / "nvcm-netbox-bring-up.sh"
            local.write_text(body)
            local.chmod(0o600)
            self.runner(host, port).upload(local, remote)

        quoted = shlex.quote(remote)

        # The uploads just above prove the box was reachable, but the bring-up
        # opens a *fresh* connection and a still-settling sshd refuses the odd one
        # with exit 255. `SshRunner` retries precisely that; NVCM's
        # `_ssh_run_and_tail` does not, and it cannot tell a refused connection
        # from a script that failed -- both surface as "did not finish".
        #
        # So the session is established through the retrying runner first. The
        # script itself is expensive and not worth re-running blindly, so it is
        # still handed to the tail exactly once.
        try:
            self.runner(host, port).run(["true"])
        except Exception as exc:  # noqa: BLE001 - re-raised with what it means
            raise RuntimeError(
                "Could not open a session on the sim box to run the NetBox bring-up, "
                f"though the uploads just before it succeeded:\n  {exc}\n"
                "If that says 'Permission denied', the box's nvcm password no longer "
                "matches oob_ssh_password in the sim config."
            ) from exc

        ok = self._manager._ssh_run_and_tail(
            host,
            port,
            f"chmod 600 {quoted} && bash {quoted}; rc=$?; rm -f {quoted}; "
            f'if [ "$rc" -eq 0 ]; then echo {BRING_UP_MARKER}; fi; exit "$rc"',
            marker=BRING_UP_MARKER,
            label="netbox",
            timeout=self.cfg.deploy_timeout or 3600,
        )
        if not ok:
            raise RuntimeError(
                "The NetBox bring-up did not finish. Its output is above; the failing "
                "command is the last one echoed."
            )

    def publish_route(self, host: str, port: int) -> None:
        """Create NetBox's HTTPRoute, now that the Gateway API CRDs are registered.

        The bring-up installs the chart before NVCM deploys, so the Gateway API does
        not exist on the box yet and templates/httproute.yaml is skipped --
        rendering an HTTPRoute without its CRDs fails the whole release. Nothing
        else upgrades the release afterwards, so without this pass the sim's
        `https://netbox.<hostname>` never routes.

        The chart is still unpacked where the bring-up put it, and --reuse-values
        replays the install's own values.
        """
        runner = self.runner(host, port)
        remote_chart = f"/home/{NVCM_BOX_USER}/charts/{self.chart.name}"
        cluster.publish_netbox_route(
            remote_chart,
            release=self.cfg.netbox_release,
            namespace=NAMESPACE,
            runner=runner,
        )
        if cluster.netbox_route_exists(NAMESPACE, runner=runner):
            self.on_log(f"NetBox route published for https://{self.cfg.netbox_hostname()}")
        else:
            self.on_log(
                "NetBox still has no HTTPRoute after the upgrade, so its hostname will not "
                "resolve. Reach it with kubectl port-forward on the sim box instead."
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
