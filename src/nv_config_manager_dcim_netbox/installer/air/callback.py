# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Deploy NetBox into a DSX Air sim, in the window before NVCM deploys.

Driven by `installer.air_hooks.NetBoxAirHooks`: `pre_deploy` ships the images
and chart and runs the bring-up, `wait_for_configs` and `post_deploy` reuse
the same SSH plumbing afterwards.
"""

from __future__ import annotations

import os
import shlex
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from nv_config_manager_installer.air_sim.constants import (
    AIR_CONTENT_REMOTE_DIR,
    CONFIG_MANAGER_HOSTNAME,
    CONFIG_MANAGER_REMOTE_DIR,
    NVCM_BOX_USER,
)
from nv_config_manager_installer.air_sim.sim_config import SimConfig
from nv_config_manager_installer.air_sim.sim_manager import AirSimulationManager

from nv_config_manager_dcim_netbox.installer import cluster
from nv_config_manager_dcim_netbox.installer.air.images import TARGET_PLATFORM
from nv_config_manager_dcim_netbox.installer.config import DEFAULT_CHART
from nv_config_manager_dcim_netbox.installer.remote import ScriptRunner, SshRunner
from nv_config_manager_dcim_netbox.installer.settings import NetBoxAirSettings
from nv_config_manager_dcim_netbox.installer.shell import CommandError, run
from nv_config_manager_dcim_netbox.installer.tokens import (
    generate_api_token,
    generate_nats_password,
)

# cloud-init creates the cluster with this name.
KIND_CLUSTER = "nvcm"

# Echoed on success by the *wrapper* around the bring-up script. NVCM streams a
# remote command and watches for a marker like this to tell completion from a
# dropped connection.
BRING_UP_MARKER = "NVCM_NETBOX_BRING_UP_COMPLETE"
# Printed every 30s while the script runs. Keeps NVCM's tail reading, and its
# deadline evaluating, through steps that produce no output of their own.
HEARTBEAT_PREFIX = "NVCM_NETBOX_STILL_RUNNING"

# Longer than cluster.install_netbox's 10m default, which is sized for kind on a
# workstation. A sim box's first NetBox boot runs the whole 4.7 migration set
# against a cold database: measured 7m58s to Ready on a `small` box, against a
# 10m budget. Overrunning leaves the release in `pending-install`, which then
# has to be `helm uninstall`ed by hand before a retry.
NETBOX_INSTALL_TIMEOUT = "20m"

# Echoed by the script body itself, and watched for by nothing. `_ssh_run_and_tail`
# terminates the SSH process the moment it sees the marker it was given, so any
# command sequenced after the marker-producing one never runs. The two are kept
# distinct so the wrapper's cleanup happens before the marker the tail is
# watching for is emitted.
SCRIPT_DONE_MARKER = "NVCM_NETBOX_SCRIPT_DONE"
NAMESPACE = "nv-config-manager"
REMOTE_CHART_DIR = f"/home/{NVCM_BOX_USER}/charts"
# The provider image's /plugin-wheels, unpacked for NVCM's installer run on the box.
REMOTE_WHEEL_DIR = f"/home/{NVCM_BOX_USER}/nvcm-netbox-wheels"
PROVIDER_DISTRIBUTION = "nv-config-manager-dcim-netbox"


def blueprint_dir(cfg: SimConfig, remote_repo: str = CONFIG_MANAGER_REMOTE_DIR) -> str:
    """Return the sim's topology blueprint on the box.

    Uses the uploaded mock-topology tree when NVCM staged one, else the NVCM clone.
    """
    mock_root = f"{remote_repo}/development/mock_topology"
    if cfg._air_content_staged and cfg.mock_topology_path.startswith(AIR_CONTENT_REMOTE_DIR):
        mock_root = cfg.mock_topology_path
    return f"{mock_root}/context/{cfg.mock_blueprint}"


class NetBoxAirCallback:
    """The NetBox bring-up on a sim box, as one uploaded script.

    Named for the orchestrator callback it used to wrap; it now runs from the
    provider's AIR hooks.
    """

    def __init__(
        self,
        log: Callable[[str], None],
        cfg: SimConfig,
        settings: NetBoxAirSettings | None = None,
        chart: Path | None = None,
        manager: AirSimulationManager | None = None,
    ) -> None:
        self._log = log
        self.cfg = cfg
        self.settings = settings or NetBoxAirSettings.model_validate(cfg.dcim_settings)
        self.chart = chart or self.settings.chart or DEFAULT_CHART
        self.api_token = self.settings.token or generate_api_token()
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

    def set_manager(self, manager: AirSimulationManager) -> None:
        """Use the orchestrator's own AIR client for the remaining SSH work."""
        self._manager = manager

    def on_log(self, line: str) -> None:
        self._log(line)

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
        self.host, self.port = host, port
        self.check_image_arch(host, port)

        self.on_log("Shipping NetBox and provider images into the sim...")
        images_tar = self.upload_images(host, port)

        self.on_log("Uploading the NetBox chart...")
        chart_tar = self.upload_chart(host, port)

        self.on_log("Uploading the provider wheels for NVCM's installer...")
        wheels_tar = self.upload_wheels(host, port)

        self.on_log("Deploying NetBox...")
        self.run_script(host, port, self.bring_up_script(images_tar, chart_tar, wheels_tar))

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

    def bring_up_script(self, images_tar: str, chart_tar: str, wheels_tar: str) -> str:
        """Render every on-box step as one script.

        Built by running the same `cluster` functions the kind path uses against a
        recording runner, so the two cannot drift.
        """
        settings = self.settings
        remote_chart = f"{REMOTE_CHART_DIR}/{self.chart.name}"

        chart_dir = shlex.quote(REMOTE_CHART_DIR)
        script = ScriptRunner()
        script.run(["kind", "load", "image-archive", images_tar, "--name", KIND_CLUSTER])
        script.run(["rm", "-f", images_tar])
        script.run(
            [
                "sh",
                "-c",
                f"rm -rf {chart_dir} && mkdir -p {chart_dir} "
                f"&& tar -xzf {shlex.quote(chart_tar)} -C {chart_dir} "
                f"&& rm -f {shlex.quote(chart_tar)}",
            ]
        )
        wheel_dir = shlex.quote(REMOTE_WHEEL_DIR)
        script.run(
            [
                "sh",
                "-c",
                f"rm -rf {wheel_dir} && mkdir -p {wheel_dir} "
                f"&& tar -xzf {shlex.quote(wheels_tar)} -C {wheel_dir} "
                f"&& rm -f {shlex.quote(wheels_tar)}",
            ]
        )
        esc = cluster.helm_set_value
        cluster.install_netbox(
            remote_chart,
            release=settings.release,
            namespace=NAMESPACE,
            api_token=self.api_token,
            image_repository=settings.image_repository,
            image_tag=settings.image_tag,
            nats_password=self.nats_password,
            workflow_url=f"https://{CONFIG_MANAGER_HOSTNAME}",
            extra_set=[
                # The CNPG operator reaches the box with NVCM's deploy, after this.
                *settings.database_overrides(),
                f"nvcm.eventStream={esc(settings.event_stream)}",
                f"nvcm.eventSubject={esc(settings.event_subject)}",
                f"netbox.superuser.name={esc(settings.superuser_name)}",
                f"netbox.superuser.password={esc(settings.superuser_password)}",
                *settings.helm_set,
            ],
            timeout=NETBOX_INSTALL_TIMEOUT,
            runner=script,
        )
        cluster.wait_for_netbox(NAMESPACE, runner=script)
        cluster.run_nvcm_init(NAMESPACE, self.api_token, runner=script)
        if settings.load_blueprint:
            cluster.load_topology(
                blueprint_dir(self.cfg),
                NAMESPACE,
                deployment_name=self.cfg.deployment_name or self.cfg.mock_blueprint,
                runner=script,
            )
        # Every secret the script carries, not just the token: the NATS and
        # superuser passwords currently share the helm line with it, but the
        # redaction must not depend on that staying true. Both the raw superuser
        # password and its helm-escaped form, since the script holds the latter.
        return script.script(
            marker=SCRIPT_DONE_MARKER,
            redact=[
                self.api_token,
                self.nats_password,
                self.settings.superuser_password,
                cluster.helm_set_value(self.settings.superuser_password),
            ],
        )

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
            # Created 0600 rather than written and then chmod'ed, so the file
            # never exists at the umask default while it holds the secrets.
            fd = os.open(local, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(body)
            self.runner(host, port).upload(local, remote)

        quoted = shlex.quote(remote)

        # The uploads just above prove the box was reachable, but the bring-up
        # opens a *fresh* connection and a still-settling sshd refuses the odd one
        # with exit 255. `SshRunner` retries precisely that; NVCM's
        # `_ssh_run_and_tail` does not, and it cannot tell a refused connection
        # from a script that failed -- both surface as "did not finish".
        try:
            self.runner(host, port).run(["true"])
        except Exception as exc:  # noqa: BLE001 - re-raised with what it means
            raise RuntimeError(
                "Could not open a session on the sim box to run the NetBox bring-up, "
                f"though the uploads just before it succeeded:\n  {exc}\n"
                "If that says 'Permission denied', the box's nvcm password no longer "
                "matches oob_ssh_password in the sim config."
            ) from exc

        tail_timeout = self.cfg.deploy_timeout or 3600
        # Bounded on the box, and slightly tighter than the tail, so an
        # overrunning step ends as `timeout`'s exit 124 with the failing command
        # still on screen rather than as the tail's generic give-up.
        script_timeout = max(60, tail_timeout - 60)

        ok = self._manager._ssh_run_and_tail(
            host,
            port,
            self._bring_up_command(quoted, script_timeout),
            marker=BRING_UP_MARKER,
            label="netbox",
            timeout=tail_timeout,
        )
        if not ok:
            raise RuntimeError(
                "The NetBox bring-up did not finish. Its output is above; the failing "
                "command is the last one echoed.\n"
                "A run that ends with no output at all is usually the SSH session "
                "dying rather than a slow step: NVCM's tail blocks on a read and its "
                "own timeout is only checked when a line arrives, and its ssh has no "
                "ServerAliveInterval. Re-run, and if it recurs check the box directly "
                "with `kubectl -n nv-config-manager get pods`."
            )

    @staticmethod
    def _bring_up_command(quoted_script: str, script_timeout: int) -> str:
        """Wrap the uploaded script for the tail.

        `timeout` bounds the script on the box, so a hung step becomes a reported
        failure, and a heartbeat keeps output flowing so the tail's deadline is
        evaluated: it only checks inside `for line in proc.stdout`.
        """
        # Pod READY/STATUS on every beat, so a first boot grinding through
        # migrations is distinguishable from a stall without opening a session.
        # Best effort: before the pods exist it says so rather than failing.
        status_fn = (
            "nbstat() { "
            f"kubectl -n {NAMESPACE} get pods -l app.kubernetes.io/name=netbox "
            "--no-headers 2>/dev/null "
            "| awk '{printf \"%s %s %s; \", $1,$2,$3}'; }; "
        )
        return (
            # `;` not `&&` before the heartbeat: `a && { b; } &` backgrounds the
            # whole list, chmod included.
            f"chmod 600 {quoted_script} || exit $?; " + status_fn + "{ while true; do sleep 30; "
            f'echo "{HEARTBEAT_PREFIX} $(date -u +%H:%M:%S) $(nbstat)"; done; }} & '
            "hb=$!; "
            f"timeout {script_timeout} bash {quoted_script}; rc=$?; "
            'kill "$hb" 2>/dev/null || true; '
            f"rm -f {quoted_script}; "
            'if [ "$rc" -eq 124 ]; then '
            f'echo "The NetBox bring-up exceeded {script_timeout}s and was stopped; '
            'the last command echoed above is where it stalled."; fi; '
            f'if [ "$rc" -eq 0 ]; then echo {BRING_UP_MARKER}; fi; exit "$rc"'
        )

    def verify_route(self, host: str, port: int) -> None:
        """Report whether NVCM's chart published NetBox's HTTPRoute on the sim's Gateway."""
        runner = self.runner(host, port)
        hostname = self.settings.netbox_hostname(CONFIG_MANAGER_HOSTNAME)
        if cluster.netbox_route_exists(NAMESPACE, runner=runner):
            self.on_log(f"NetBox route published for https://{hostname}")
        else:
            self.on_log(
                "NetBox has no HTTPRoute, so its hostname will not resolve; NVCM's release "
                "renders it from dcim.local. Reach it with kubectl port-forward on the sim "
                "box instead."
            )

    def check_image_arch(self, host: str, port: int) -> None:
        """Refuse to ship images the sim box cannot execute.

        The hooks build for the sim architecture before allocating one. This check
        sits at the upload boundary so an image that did not come from that build
        cannot reach the cluster with an incompatible architecture.
        """
        want = self.runner(host, port).run(["uname", "-m"]).strip()
        want = {"x86_64": "amd64", "aarch64": "arm64"}.get(want, want)

        images = [self.settings.provider_image, self.settings.netbox_image]
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
                + f"\n\nThe hooks build these for {TARGET_PLATFORM} before allocating "
                + "the sim, so each one above was either overridden in the sim config, "
                + "replaced locally, or never built. Check the build output above."
            )

    def upload_images(self, host: str, port: int) -> str:
        """`docker save` both images and upload the archive. Returns its path.

        Neither image is in a registry and both are pulled IfNotPresent, so they
        must already be on the node. `kind load` runs later, in the script.
        """
        images = [self.settings.provider_image, self.settings.netbox_image]
        remote = f"/home/{NVCM_BOX_USER}/nvcm-netbox-images.tar"
        with tempfile.NamedTemporaryFile(suffix=".tar", prefix="nvcm-netbox-") as tmp:
            archive = Path(tmp.name)
            run(["docker", "save", *images, "-o", str(archive)])
            self.runner(host, port).upload(archive, remote)
        return remote

    def upload_wheels(self, host: str, port: int) -> str:
        """Upload the provider image's wheelhouse. Returns the archive's path.

        The same wheels NVCM's pods install from, so the installer on the box loads
        the provider code they run. Unpacking happens later, in the script.
        """
        remote_tar = f"/home/{NVCM_BOX_USER}/nvcm-netbox-wheels.tgz"
        container = run(["docker", "create", self.settings.provider_image]).strip()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                wheels = Path(tmpdir) / "wheels"
                run(["docker", "cp", f"{container}:/plugin-wheels/.", str(wheels)])
                archive = Path(tmpdir) / "wheels.tgz"
                run(["tar", "-czf", str(archive), "-C", str(wheels), "."])
                self.runner(host, port).upload(archive, remote_tar)
        finally:
            run(["docker", "rm", container])
        return remote_tar

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
