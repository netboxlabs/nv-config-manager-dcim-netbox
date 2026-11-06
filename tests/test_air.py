# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""DSX Air path: the provider hooks, the sim settings and the bring-up sequence.

No simulation and no cluster. The parts that need one are covered by the
end-to-end steps in docs/air-sim.md.
"""

from __future__ import annotations

from importlib import metadata
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml
from click.testing import CliRunner

from nv_config_manager_dcim_netbox.installer import cluster
from nv_config_manager_dcim_netbox.installer.tokens import generate_api_token, is_valid_api_token

pytestmark = pytest.mark.air

# Everything here needs nv-config-manager-installer, which comes from an NVCM
# checkout rather than an index (see `make setup`).
pytest.importorskip("nv_config_manager_installer", reason="the `installer` extra is not installed")

from nv_config_manager_installer.air_sim.installer_config import (  # noqa: E402
    build_deploy_command,
    build_template_plugins,
    generate_air_sim_install_config,
)
from nv_config_manager_installer.air_sim.orchestrator import SimOrchestrator  # noqa: E402
from nv_config_manager_installer.air_sim.sim_config import SimConfig  # noqa: E402

from nv_config_manager_dcim_netbox.installer import air_hooks  # noqa: E402
from nv_config_manager_dcim_netbox.installer.air import callback as air_callback  # noqa: E402
from nv_config_manager_dcim_netbox.installer.air import cli as air_cli  # noqa: E402
from nv_config_manager_dcim_netbox.installer.air import images as air_images  # noqa: E402
from nv_config_manager_dcim_netbox.installer.settings import (  # noqa: E402
    NetBoxAirSettings,
    NetBoxInstallerSettings,
)

NetBoxAirCallback = air_callback.NetBoxAirCallback
BRING_UP_MARKER = air_callback.BRING_UP_MARKER


def _sim(**kwargs) -> SimConfig:
    kwargs.setdefault("dcim_provider", "netbox")
    return SimConfig(**kwargs)


# -- independent fabric generation and DCIM population ------------------------


def test_blueprint_still_builds_the_fabric_without_the_nautobot_job():
    """NetBox loads the blueprint itself; the generated install config carries
    no Nautobot population job."""
    cfg = _sim(mock_blueprint="air_trial", deployment_name="demo")

    # 1. The sim still gets a fabric to boot.
    topology_path = SimOrchestrator(cfg, Mock())._resolve_topology_path(cfg)
    design = yaml.safe_load(Path(topology_path).read_text())
    assert design["devices"], "the generated site design has no devices"

    # 2. The job that would load it into a DCIM we do not deploy is not written.
    generated = generate_air_sim_install_config(
        cfg, "air-trial", ["192.0.2.0/24"], air_hooks.NetBoxAirHooks()
    )
    assert generated["dcim"]["provider"] == "netbox"
    assert generated["dcim"]["local"] is True
    assert "jobs" not in generated["dcim"].get("settings", {})
    assert "nautobot" not in generated["services"]

    # 3. The templates that render the demo fabric survive.
    assert any("superpod-template-plugin" in e["path"] for e in build_template_plugins(cfg))


# -- sim settings -------------------------------------------------------------


def test_air_settings_default_to_the_demo_login_and_the_package_images():
    settings = NetBoxAirSettings()
    assert settings.superuser_name == "demo"
    assert settings.superuser_password == "demo"
    assert settings.token == ""
    assert settings.load_blueprint is True
    assert settings.netbox_image == NetBoxInstallerSettings().netbox_image
    assert settings.netbox_hostname("nvcm.air") == "netbox.nvcm.air"


def test_air_settings_load_from_dcim_settings(tmp_path):
    path = tmp_path / "sim.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "mock_blueprint": "air_trial",
                "dcim_provider": "netbox",
                "dcim_settings": {"image_tag": "9.9.9", "load_blueprint": False},
            }
        )
    )
    cfg = SimConfig.from_yaml(path)
    settings = air_hooks.air_settings(cfg)
    assert settings.image_tag == "9.9.9"
    assert settings.load_blueprint is False
    assert air_hooks.NetBoxAirHooks().validate_config(cfg) == ""


def test_unknown_dcim_settings_are_a_launch_error():
    cfg = _sim(dcim_settings={"netbox_image": "x"})
    assert "dcim_settings is invalid" in air_hooks.NetBoxAirHooks().validate_config(cfg)


def test_install_settings_point_the_box_at_the_uploaded_chart():
    """The install config NVCM uploads runs this provider's pre-Helm hook on the
    box, where the chart is wherever the bring-up unpacked it."""
    cfg = _sim(dcim_settings={"image_tag": "9.9.9", "token": "nbt_secret.x"})
    settings = air_hooks.NetBoxAirHooks().install_settings(cfg)

    assert settings["chart"] == f"{air_callback.REMOTE_CHART_DIR}/netbox"
    assert settings["image_tag"] == "9.9.9"
    # Demo login survives even though it is the AIR default.
    assert settings["superuser_name"] == "demo"
    assert settings["superuser_password"] == "demo"
    # The token is a secret, not a setting.
    assert "token" not in settings
    NetBoxInstallerSettings.model_validate(settings)


def test_blueprint_dir_points_into_the_clone_on_the_box():
    assert air_callback.blueprint_dir(_sim(mock_blueprint="air_trial")).endswith(
        "/development/mock_topology/context/air_trial"
    )


def test_blueprint_dir_uses_staged_local_content():
    cfg = _sim(mock_blueprint="air_trial")
    cfg.mock_topology_path = "/home/nvcm/air-content/mock-topology/mock_topology"
    cfg._air_content_staged = True

    assert air_callback.blueprint_dir(cfg) == (
        "/home/nvcm/air-content/mock-topology/mock_topology/context/air_trial"
    )


def test_blueprint_dir_ignores_a_local_mock_topology_that_was_not_staged():
    cfg = _sim(mock_blueprint="air_trial", mock_topology_path="/Users/me/mock_topology")
    cfg._air_content_staged = True

    assert air_callback.blueprint_dir(cfg, "/home/nvcm/nv-config-manager") == (
        "/home/nvcm/nv-config-manager/development/mock_topology/context/air_trial"
    )


# -- hooks ---------------------------------------------------------------------


class _FakeDeployment:
    instances: list = []

    def __init__(self, log, cfg, settings, manager=None):
        self.cfg = cfg
        self.settings = settings
        self.manager = manager
        self.calls: list[tuple] = []
        _FakeDeployment.instances.append(self)

    def set_manager(self, manager):
        self.manager = manager

    def bring_up_netbox(self, host, port):
        self.calls.append(("pre", host, port))

    def wait_for_intended_configs(self, host, port, expected_total):
        self.calls.append(("wait", host, port, expected_total))

    def verify_route(self, host, port):
        self.calls.append(("post", host, port))


@pytest.fixture
def fake_deployment(monkeypatch):
    _FakeDeployment.instances = []
    monkeypatch.setattr(air_hooks, "NetBoxAirCallback", _FakeDeployment)
    yield _FakeDeployment


def test_hooks_own_the_provider_lifecycle(fake_deployment, monkeypatch):
    hooks = air_hooks.NetBoxAirHooks()
    cfg = _sim(mock_blueprint="air_trial")
    built = []
    monkeypatch.setattr(air_hooks, "ensure_air_images", lambda p, n, log: built.append((p, n)))
    manager = object()

    hooks.prepare_images(cfg, lambda line: None)
    hooks.pre_deploy(manager, "worker.example", 17117)
    hooks.wait_for_configs(manager, "worker.example", 17117, 7)
    hooks.post_deploy(manager, "worker.example", 17117)

    settings = NetBoxAirSettings()
    assert built == [(settings.provider_image, settings.netbox_image)]
    (deployment,) = fake_deployment.instances
    assert deployment.cfg is cfg
    assert deployment.manager is manager
    assert deployment.calls == [
        ("pre", "worker.example", 17117),
        ("wait", "worker.example", 17117, 7),
        ("post", "worker.example", 17117),
    ]
    assert hooks.gateway_hostnames(cfg) == ("netbox.nvcm.air",)


def test_hooks_layer_the_uploaded_wheelhouse_onto_the_box_installer():
    packages = air_hooks.NetBoxAirHooks().installer_packages(_sim())

    assert packages.find_links == (air_callback.REMOTE_WHEEL_DIR,)
    assert packages.requirements == (air_callback.PROVIDER_DISTRIBUTION,)
    command = build_deploy_command(_sim(), packages)
    assert f"--with {air_callback.PROVIDER_DISTRIBUTION}" in command


def test_the_box_requirement_names_the_distribution_holding_the_entry_point():
    (entry_point,) = metadata.entry_points(group="nv_config_manager.installer.dcim", name="netbox")
    assert entry_point.dist is not None
    assert entry_point.dist.name == air_callback.PROVIDER_DISTRIBUTION


def test_hooks_need_the_sim_config_before_the_per_host_stages(fake_deployment):
    with pytest.raises(RuntimeError, match="prepare_images"):
        air_hooks.NetBoxAirHooks().pre_deploy(object(), "box", 22)


def test_a_failed_route_check_does_not_fail_the_deploy(fake_deployment):
    hooks = air_hooks.NetBoxAirHooks()
    logs = []
    hooks._log = logs.append
    hooks.install_settings(_sim())

    def boom(host, port):
        raise RuntimeError("no gateway")

    fake_deployment.verify_route = boom
    hooks.post_deploy(object(), "box", 22)
    assert any("Could not verify the NetBox route" in line for line in logs)


def test_netbox_provisioning_status_uses_netbox_shell():
    manager = Mock()
    manager.run_remote.return_value = Mock(returncode=0, stdout="2/3|leaf-03\n")

    status = air_hooks.NetBoxAirHooks().provisioning_status(manager, "worker.example", 17117)

    assert status == (2, 3, ["leaf-03"])
    command = manager.run_remote.call_args.args[2]
    assert manager.run_remote.call_args.kwargs == {"timeout": 30}
    assert "-n nv-config-manager" in command
    assert "deployment/netbox" in command
    assert "-c netbox" in command
    assert "/opt/netbox/venv/bin/python" in command
    assert "DeviceConfigStatus" in command
    assert "nautobot" not in command.lower()


def test_a_failed_provisioning_probe_reads_as_nothing_provisioned():
    manager = Mock()
    manager.run_remote.side_effect = OSError("ssh down")
    assert air_hooks.NetBoxAirHooks().provisioning_status(manager, "box", 22) == (0, 0, [])


def test_launch_status_watches_the_netbox_web_pod():
    status = air_hooks.NETBOX_LAUNCH_STATUS
    assert status.display_name == "NetBox"
    assert status.access_url == "https://netbox.nvcm.air"
    assert status.is_web_pod({"name": "netbox-7d9f"})
    assert not status.is_web_pod({"name": "netbox-worker-7d9f"})
    assert not status.is_web_pod({"name": "netbox-postgresql-0"})


@pytest.mark.asyncio
async def test_population_panel_writes_load_blueprint_into_dcim_settings():
    from nv_config_manager_installer.tui.widgets import LabeledSwitch
    from textual.app import App

    cfg = _sim(dcim_settings={"load_blueprint": False})

    class _Host(App[None]):
        def compose(self):
            yield air_hooks.NetBoxPopulationPanel(cfg, id="population-panel")

    app = _Host()
    async with app.run_test(size=(120, 40)):
        panel = app.query_one("#population-panel", air_hooks.NetBoxPopulationPanel)
        assert panel.query_one("#load-blueprint", LabeledSwitch).value is False
        panel.query_one("#load-blueprint", LabeledSwitch).value = True
        panel.write_to_config(cfg)
        assert cfg.dcim_settings == {}
        panel.query_one("#load-blueprint", LabeledSwitch).value = False
        panel.write_to_config(cfg)
        assert cfg.dcim_settings == {"load_blueprint": False}


# -- the bring-up sequence -----------------------------------------------------


class _FakeManager:
    """Stands in for AirSimulationManager: records commands, uploads nothing."""

    def __init__(self, files: dict[str, str] | None = None) -> None:
        self.commands: list[str] = []
        self.uploads: list[tuple[str, str]] = []
        self.files = files or {}

    def _ssh_cmd(self, host, port, command):
        self.commands.append(command)
        # The patch step reads the uploaded config back with `cat`.
        if command.startswith("cat "):
            return ["printf", "%s", self.files.get(command.split()[-1], "")]
        if "jsonpath" in command:
            return ["printf", "%s", "netbox-abc123"]
        return ["true"]

    def upload_to_server(self, host, port, local, remote, **kwargs):
        self.uploads.append((str(local), remote))
        self.files[remote] = Path(local).read_text()
        return True


class _Recorder:
    def __init__(self) -> None:
        self.events: list[str] = []

    def on_step(self, step_id, status, message="") -> None:
        self.events.append(f"step:{step_id}")

    def on_log(self, line) -> None:
        self.events.append(f"log:{line}")

    def on_ssh_ready(self, host, port) -> None:
        self.events.append("ssh_ready")

    def on_deploy_started(self, host, port) -> None:
        self.events.append("deploy_started")

    def on_complete(self, success, host="", port=0) -> None:
        self.events.append(f"complete:{success}")


def test_a_generated_token_is_valid_and_reused():
    callback = NetBoxAirCallback(lambda line: None, _sim(), manager=_FakeManager())
    assert is_valid_api_token(callback.api_token)
    # Supplied tokens win, so both sides can be given a known value.
    supplied = generate_api_token()
    other = NetBoxAirCallback(
        lambda line: None, _sim(dcim_settings={"token": supplied}), manager=_FakeManager()
    )
    assert other.api_token == supplied
    assert other.settings.token == supplied


def test_netbox_is_installed_once_with_the_nats_password():
    """NVCM accepts a supplied natsPassword (installer/secrets.py), so NetBox can
    be configured with it up front instead of being upgraded a second time once
    NVCM's deployer has generated one."""
    cb = _callback_for_script()
    script = cb.bring_up_script("/i.tar", "/c.tgz", "/w.tgz")
    assert script.count("helm upgrade --install") == 1
    assert f"nvcm.natsPassword={cb.nats_password}" in script


def test_air_netbox_uses_overridable_demo_login():
    script = _callback_for_script().bring_up_script("/i.tar", "/c.tgz", "/w.tgz")
    assert "netbox.superuser.name=demo" in script
    assert "netbox.superuser.password=demo" in script

    custom = _callback_for_script(
        superuser_name="operator",
        superuser_password="lab-password",
    ).bring_up_script("/i.tar", "/c.tgz", "/w.tgz")
    assert "netbox.superuser.name=operator" in custom
    assert "netbox.superuser.password=lab-password" in custom


def test_air_netbox_login_escapes_helm_separators():
    """helm --set reads `,` as the next assignment and `\\` as an escape.

    Unescaped, a password holding either silently becomes a shorter password
    plus a junk key, and the login the TUI prints is not the one installed.
    The generated NATS password cannot hit this -- it is alphanumeric by
    construction -- but these two are free-form config values.
    """
    script = _callback_for_script(
        superuser_password=r"lab,pass\word",
    ).bring_up_script("/i.tar", "/c.tgz", "/w.tgz")
    assert r"netbox.superuser.password=lab\,pass\\word" in script


def test_helm_set_value_escapes_only_what_helm_reads():
    assert cluster.helm_set_value("plain") == "plain"
    assert cluster.helm_set_value("a.b") == "a.b"
    assert cluster.helm_set_value("a,b") == r"a\,b"
    assert cluster.helm_set_value("a\\b") == r"a\\b"


# -- SSH retry -----------------------------------------------------------------


def _ssh_runner(monkeypatch, results):
    """An SshRunner whose underlying `run` yields *results* in order."""
    from nv_config_manager_dcim_netbox.installer import remote
    from nv_config_manager_dcim_netbox.installer.shell import CommandError

    calls = []

    def fake_run(argv, stdin=None):
        calls.append(list(argv))
        outcome = results[len(calls) - 1]
        if isinstance(outcome, int):
            raise CommandError(argv, outcome, "boom")
        return outcome

    monkeypatch.setattr(remote, "run", fake_run)
    monkeypatch.setattr(remote.time, "sleep", lambda _s: None)

    class _Manager:
        def _ssh_cmd(self, host, port, command):
            return ["sshpass", "-p", "pw", "ssh", f"{host}:{port}", command]

    return remote.SshRunner(_Manager(), "box", 22), calls


def test_ssh_transport_failures_are_retried(monkeypatch):
    # A freshly booted box refuses the occasional connection while sshd settles,
    # and one blip should not lose a deploy that is already well underway.
    runner, calls = _ssh_runner(monkeypatch, [255, 255, "ok"])
    assert runner.run(["helm", "upgrade"]) == "ok"
    assert len(calls) == 3


def test_remote_command_failures_are_not_retried(monkeypatch):
    # Only 255 is ssh's own. Anything else came from the remote command, and
    # re-running it could repeat a side effect.
    from nv_config_manager_dcim_netbox.installer.shell import CommandError

    runner, calls = _ssh_runner(monkeypatch, [1, "ok"])
    with pytest.raises(CommandError):
        runner.run(["helm", "upgrade"])
    assert len(calls) == 1


def test_ssh_retries_are_bounded(monkeypatch):
    from nv_config_manager_dcim_netbox.installer.shell import CommandError

    runner, calls = _ssh_runner(monkeypatch, [255] * 10)
    with pytest.raises(CommandError):
        runner.run(["helm", "upgrade"])
    assert len(calls) == 4


# -- one SSH session -----------------------------------------------------------


def _callback_for_script(**overrides):
    cb = NetBoxAirCallback.__new__(NetBoxAirCallback)
    cb.cfg = _sim(mock_blueprint="air_trial", deployment_name="demo")
    cb.settings = NetBoxAirSettings(**overrides)
    cb._log = lambda line: None
    cb.chart = Path("deploy/netbox")

    cb.api_token = "nbt_SECRETKEY123." + "B" * 40
    cb.nats_password = "NatsProbePassword123"
    return cb


def test_bring_up_is_one_script_covering_every_on_box_step():
    """NVCM runs a deploy as one command on the box and streams it back.

    A command at a time costs an authenticated SSH connection each, and any one
    of them can catch a freshly booted sshd mid-blip and lose the run.
    """
    script = _callback_for_script().bring_up_script(
        "/home/nvcm/i.tar", "/home/nvcm/c.tgz", "/home/nvcm/w.tgz"
    )

    for expected in (
        "kind load image-archive /home/nvcm/i.tar",
        "helm upgrade --install netbox",
        "rollout status deploy/netbox",
        "nvcm_init --bootstrap",
        "nvcm_load_dcim",
    ):
        assert expected in script, expected
    # Stops at the first failure, and only says it finished if it did. The marker
    # the SSH tail actually watches for is emitted by run_script's wrapper, after it
    # has deleted the script -- see SCRIPT_DONE_MARKER.
    assert script.startswith("#!/usr/bin/env bash\nset -euo pipefail")
    assert script.rstrip().endswith("echo NVCM_NETBOX_SCRIPT_DONE")


def test_the_bring_up_script_is_deleted_before_the_run_reports_done(tmp_path):
    """The script carries the API token and the NATS password.

    `_ssh_run_and_tail` terminates the SSH process the instant it sees the marker
    it was given, so a `rm -f` sequenced after the command that emits it never
    runs and the script survives on the box. The wrapper must therefore delete it
    *before* emitting that marker.
    """
    cb = _callback_for_script()
    seen = {}

    class _Manager:
        def _ssh_run_and_tail(self, host, port, command, *, marker, label, timeout):
            seen["command"] = command
            seen["marker"] = marker
            return True

    cb._manager = _Manager()
    cb.runner = lambda host, port: _Uploads()

    cb.run_script("box", 22, "#!/usr/bin/env bash\necho hi\n")

    command, marker = seen["command"], seen["marker"]
    assert marker == "NVCM_NETBOX_BRING_UP_COMPLETE"
    assert "rm -f" in command
    assert command.index("rm -f") < command.index(f"echo {marker}"), command
    # And only on success, so a failure is not reported as a completed deploy.
    assert 'if [ "$rc" -eq 0 ]' in command


class _Uploads:
    """An SshRunner stand-in that accepts the upload and the session probe."""

    def upload(self, local, remote):
        assert local.exists()

    def run(self, argv, *, stdin=None):
        return ""


def _assert_untraced(script: str, secret: str, label: str) -> None:
    lines = script.splitlines()
    seen = False
    for i, line in enumerate(lines):
        if secret in line:
            seen = True
            preceding = "\n".join(lines[max(0, i - 4) : i])
            assert "set +x" in preceding, f"{label} traced at line {i}: {line[:60]}"
    assert seen, f"{label} not in script; the assertion checked nothing"


def test_the_api_token_is_never_traced():
    # The script runs under `set -x` and its output is streamed back to the
    # operator, so any line carrying the token must disable tracing first.
    cb = _callback_for_script()
    _assert_untraced(cb.bring_up_script("/i.tar", "/c.tgz", "/w.tgz"), cb.api_token, "token")


def test_no_password_in_the_script_is_traced():
    """The NATS and superuser passwords are redacted in their own right.

    They happen to share the helm line with the API token today, so a redact
    list of just the token covers them by accident. Each must be named so a
    refactor that moves one onto its own line does not start echoing it.
    """
    cb = _callback_for_script(superuser_password=r"lab,pass\word")
    script = cb.bring_up_script("/i.tar", "/c.tgz", "/w.tgz")
    _assert_untraced(script, cb.nats_password, "NATS password")
    # The script holds the helm-escaped form, not the raw value.
    _assert_untraced(script, r"lab\,pass\\word", "superuser password")

    from nv_config_manager_dcim_netbox.installer.remote import ScriptRunner

    captured = {}
    original = ScriptRunner.script

    def spy(self, *, marker, redact=None):
        captured["redact"] = list(redact or [])
        return original(self, marker=marker, redact=redact)

    ScriptRunner.script = spy
    try:
        cb.bring_up_script("/i.tar", "/c.tgz", "/w.tgz")
    finally:
        ScriptRunner.script = original
    assert cb.nats_password in captured["redact"]
    assert r"lab,pass\word" in captured["redact"]
    assert r"lab\,pass\\word" in captured["redact"]


def test_the_local_script_is_created_private(tmp_path):
    """The script holds the token and passwords, so it must never exist at the
    umask default, not even between a write and a chmod."""
    cb = _callback_for_script()
    seen = {}

    class _Manager:
        def _ssh_run_and_tail(self, host, port, command, *, marker, label, timeout):
            return True

    class _ModeCapture:
        def upload(self, local, remote):
            seen["mode"] = local.stat().st_mode & 0o777

        def run(self, argv, *, stdin=None):
            return ""

    cb._manager = _Manager()
    cb.runner = lambda host, port: _ModeCapture()

    cb.run_script("box", 22, "#!/usr/bin/env bash\necho hi\n")

    assert seen["mode"] == 0o600


def test_the_blueprint_load_is_skipped_when_asked():
    script = _callback_for_script(load_blueprint=False).bring_up_script(
        "/i.tar", "/c.tgz", "/w.tgz"
    )
    assert "nvcm_load_dcim" not in script


def test_the_sim_installs_netbox_on_the_bundled_postgres():
    # The bring-up runs before NVCM's deploy installs the CNPG operator on the
    # box, so the chart's CNPG default would leave NetBox without a database.
    script = _callback_for_script().bring_up_script("/i.tar", "/c.tgz", "/w.tgz")
    assert "nvcm.database.cnpg.enabled=false" in script
    assert "netbox.postgresql.enabled=true" in script
    # Routes come from NVCM's release; the chart takes no gateway values.
    assert "nvcm.gateway" not in script


def test_install_settings_keep_the_box_on_the_bundled_postgres():
    settings = air_hooks.NetBoxAirHooks().install_settings(_sim())
    assert settings["database"] == "bundled"


def test_script_runner_renders_stdin_as_a_heredoc():
    from nv_config_manager_dcim_netbox.installer.remote import ScriptRunner

    r = ScriptRunner()
    r.run(["sh", "-s"], stdin="echo hello\n")
    body = r.script(marker="DONE")
    assert "sh -s <<'NVCM_STDIN'\necho hello\nNVCM_STDIN" in body


# -- image architecture --------------------------------------------------------


def test_air_image_preparation_builds_without_make(monkeypatch, tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    commands = []
    monkeypatch.setattr(air_images, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(air_images, "require_tools", lambda *names: None)
    monkeypatch.setattr(air_images, "_resolve_nvcm_repo", lambda: Path("/nvcm"))
    monkeypatch.setattr(air_images, "_resolve_plugin_repo", lambda log, ref: Path("/plugin"))
    monkeypatch.setattr(
        air_images,
        "_load_versions",
        lambda: {
            "NETBOX_BASE_IMAGE": "netbox-base:4.7",
            "NETBOX_BGP_VERSION": "0.20.2",
            "NETBOX_PLUGIN_REF": "main",
        },
    )
    monkeypatch.setattr(
        air_images,
        "_stream",
        lambda argv, **kwargs: commands.append(argv),
    )

    air_images.ensure_air_images("provider:tag", "netbox:tag", lambda line: None)

    # Two docker builds and nothing else: the NetBox base is netbox-docker's
    # published image, so there is no base-image build step to run.
    assert len(commands) == 2
    assert commands[0][:4] == ["docker", "build", "--platform", "linux/amd64"]
    assert commands[1][:4] == ["docker", "build", "--platform", "linux/amd64"]
    assert "--build-arg" in commands[1]
    assert "NETBOX_BASE_IMAGE=netbox-base:4.7" in commands[1]
    assert all("make" not in command for command in commands)


def test_a_rate_limited_build_retries_through_a_mirror(monkeypatch, tmp_path):
    """Hub 429s the base-image manifest even when the image is cached locally, so
    the build dies before the layer cache is reached."""
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    calls = []

    def fake_stream(argv, **kwargs):
        calls.append(argv)
        if len(calls) == 1:
            raise air_images._RateLimited(argv, 1)

    monkeypatch.setattr(air_images, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(air_images, "require_tools", lambda *names: None)
    monkeypatch.setattr(air_images, "_resolve_nvcm_repo", lambda: Path("/nvcm"))
    monkeypatch.setattr(air_images, "_resolve_plugin_repo", lambda log, ref: Path("/plugin"))
    monkeypatch.setattr(
        air_images,
        "_load_versions",
        lambda: {"NETBOX_BASE_IMAGE": "netbox-base:4.7", "NETBOX_BGP_VERSION": "0.20.2"},
    )
    monkeypatch.setattr(air_images, "_stream", fake_stream)

    air_images.ensure_air_images("provider:tag", "netbox:tag", lambda line: None)

    retry = calls[1]
    assert retry[-1] == ".", "the build context must stay last"
    for image in air_images._HUB_BASE_IMAGES:
        assert f"{image}=docker-image://{air_images.DEFAULT_MIRROR}/{image}" in retry


def test_skipping_the_build_refuses_a_wrong_arch_image(monkeypatch, tmp_path):
    monkeypatch.setenv(air_images.SKIP_BUILD_ENV, "1")
    monkeypatch.setattr(air_images, "_image_is_amd64", lambda image: image == "provider:tag")
    with pytest.raises(RuntimeError, match="netbox:tag"):
        air_images.ensure_air_images("provider:tag", "netbox:tag", lambda line: None)


def _arch_callback(monkeypatch, box_arch, image_arch):
    from nv_config_manager_dcim_netbox.installer.air import callback as cb_mod

    cb = NetBoxAirCallback.__new__(NetBoxAirCallback)
    cb.cfg = _sim(mock_blueprint="air_trial")
    cb.settings = NetBoxAirSettings()
    cb.on_log = lambda _line: None

    class _Runner:
        def run(self, argv, stdin=None):
            return box_arch + "\n"

    cb.runner = lambda h, p: _Runner()
    monkeypatch.setattr(cb_mod, "run", lambda argv, stdin=None: image_arch + "\n")
    return cb


def test_images_built_for_the_wrong_arch_are_refused(monkeypatch):
    """The upload boundary still rejects a custom image for the wrong host.

    Built on Apple Silicon and loaded onto the box, the containers die instantly
    with "exec format error", which surfaces ten minutes later as a helm timeout
    with nothing pointing at the cause.
    """
    cb = _arch_callback(monkeypatch, "x86_64", "arm64")
    with pytest.raises(RuntimeError) as excinfo:
        cb.check_image_arch("host", 22)
    message = str(excinfo.value)
    assert "amd64" in message
    assert air_images.TARGET_PLATFORM in message
    assert "overridden in the sim config" in message


def test_matching_images_pass(monkeypatch):
    cb = _arch_callback(monkeypatch, "x86_64", "amd64")
    cb.check_image_arch("host", 22)


def test_an_arm_box_wants_arm_images(monkeypatch):
    cb = _arch_callback(monkeypatch, "aarch64", "arm64")
    cb.check_image_arch("host", 22)


# -- the SOCKS tunnel ----------------------------------------------------------
#
# `tunnel` is importable without the `air` extra on purpose (it takes the AIR
# client as an argument), but it lives here with the rest of the Air path.
from nv_config_manager_dcim_netbox.installer.air import tunnel as tunnel_mod  # noqa: E402


class _Iface:
    def __init__(self, iface_id):
        self.id = iface_id


class _Node:
    def __init__(self, name):
        self.name = name
        self.id = f"node-{name}"


class _Service:
    def __init__(self, interface, node_port, fqdn, port):
        self.interface = interface
        self.node_port = node_port
        self.worker_fqdn = fqdn
        self.worker_port = port


class _Sim:
    def __init__(self, name, sim_id="sim-1"):
        self.name = name
        self.id = sim_id


class _Client:
    """The two-call shape find_ssh_endpoint needs from the AIR client."""

    def __init__(self, sims, nodes, services, ifaces):
        self._sims, self._nodes, self._services, self._ifaces = sims, nodes, services, ifaces
        self.created = []

    class _Sims:
        def __init__(self, outer):
            self._outer = outer

        def list(self, **kwargs):
            return iter(self._outer._sims)

    @property
    def simulations(self):
        return self._Sims(self)

    @property
    def nodes(self):
        return type("N", (), {"list": lambda _s, **k: iter(self._nodes)})()

    @property
    def interfaces(self):
        return type("I", (), {"list": lambda _s, **k: iter(self._ifaces)})()

    @property
    def services(self):
        outer = self

        class _Svcs:
            def list(self, **kwargs):
                return iter(outer._services)

            def create(self, **kwargs):
                outer.created.append(kwargs)
                raise AssertionError("tunnel must not create an SSH service")

        return _Svcs()


class _Manager:
    def __init__(self, client):
        self.client = client


def _manager(sim_name="air-trial", server="oob-mgmt-server", node_port=22):
    iface = _Iface("iface-1")
    return _Manager(
        _Client(
            sims=[_Sim(sim_name)],
            nodes=[_Node(server)],
            services=[_Service(iface, node_port, "worker-7.air.nvidia.com", 31234)],
            ifaces=[iface],
        )
    )


def test_the_ssh_endpoint_is_read_from_the_running_sim():
    # AIR allocates the box's host and port per simulation, so they cannot be
    # written down; the sim config's name is what makes them findable again.
    endpoint = tunnel_mod.find_ssh_endpoint(_manager(), "air-trial", "oob-mgmt-server")
    assert (endpoint.host, endpoint.port) == ("worker-7.air.nvidia.com", 31234)


def test_a_missing_sim_is_reported_not_guessed():
    with pytest.raises(tunnel_mod.SimNotFound):
        tunnel_mod.find_ssh_endpoint(
            _manager(sim_name="something-else"), "air-trial", "oob-mgmt-server"
        )


def test_two_sims_of_one_name_are_ambiguous_rather_than_arbitrary():
    """Names are not unique in an AIR org.

    Connecting to whichever came back first looks like a deployment that
    silently did nothing.
    """
    manager = _manager()
    manager.client._sims = [_Sim("air-trial", "sim-1"), _Sim("air-trial", "sim-2")]
    with pytest.raises(tunnel_mod.AmbiguousSim) as excinfo:
        tunnel_mod.find_ssh_endpoint(manager, "air-trial", "oob-mgmt-server")
    assert "sim-1" in str(excinfo.value) and "sim-2" in str(excinfo.value)


def test_a_sim_still_booting_has_no_ssh_service_yet():
    # And the tunnel must not create one: `air up` owns that, and mutating a sim
    # you were only asked to connect to is a surprise.
    manager = _manager(node_port=80)
    with pytest.raises(tunnel_mod.SimNotFound):
        tunnel_mod.find_ssh_endpoint(manager, "air-trial", "oob-mgmt-server")
    assert manager.client.created == []


def test_the_password_never_reaches_the_command_line():
    """sshpass's own help calls `-p` "security unwise" -- any same-user `ps` reads
    argv.

    NVCM's ProxyInfo.start_tunnel uses it; this uses `-d <fd>` and a pipe, so the
    password exists only in the two processes' memory.
    """
    endpoint = tunnel_mod.SshEndpoint("worker-7.air.nvidia.com", 31234)
    argv = tunnel_mod.tunnel_argv(endpoint, 8080, "nvcm", password_fd=9)

    assert "-p" not in argv[:3], argv  # sshpass's -p, not ssh's port flag
    assert argv[:3] == ["sshpass", "-d", "9"], argv
    # The endpoint still has to arrive, or the tunnel goes nowhere.
    assert "nvcm@worker-7.air.nvidia.com" in argv
    assert "31234" in argv and "-D" in argv and "8080" in argv
    assert "-N" in argv, "a forwarding-only tunnel must not run a remote command"


def test_the_printable_form_carries_no_secret_at_all():
    # Meant for pasting into a ticket, so it must be the prompting form.
    endpoint = tunnel_mod.SshEndpoint("worker-7.air.nvidia.com", 31234)
    printed = tunnel_mod.printable_command(endpoint, 1080, "nvcm")

    assert "sshpass" not in printed
    assert printed.startswith("ssh ")
    assert "-D 1080" in printed and "-p 31234" in printed
    assert "nvcm@worker-7.air.nvidia.com" in printed


def test_the_tunnel_prompts_when_sshpass_is_missing(monkeypatch):
    # macOS has no sshpass by default. Falling back to a prompting ssh keeps the
    # command usable instead of failing on a missing dependency.
    monkeypatch.setattr(tunnel_mod.shutil, "which", lambda _name: None)
    seen = {}

    def _popen(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(tunnel_mod.subprocess, "Popen", _popen)
    tunnel_mod.open_tunnel(
        tunnel_mod.SshEndpoint("box", 22), "s3cret", socks_port=8080, user="nvcm"
    )

    assert seen["argv"][0] == "ssh"
    assert "s3cret" not in seen["argv"]
    assert "pass_fds" not in seen["kwargs"]


def test_the_password_goes_down_a_pipe_not_a_file(monkeypatch):
    monkeypatch.setattr(tunnel_mod.shutil, "which", lambda _name: "/usr/bin/sshpass")
    seen = {}

    def _popen(argv, **kwargs):
        seen["argv"] = argv
        # Read it back the way sshpass would, proving the pipe carries it.
        import os as _os

        fd = kwargs["pass_fds"][0]
        seen["piped"] = _os.read(fd, 128)
        return object()

    monkeypatch.setattr(tunnel_mod.subprocess, "Popen", _popen)
    tunnel_mod.open_tunnel(
        tunnel_mod.SshEndpoint("box", 2222), "s3cret", socks_port=8080, user="nvcm"
    )

    assert seen["piped"] == b"s3cret\n"
    assert not any("s3cret" in str(a) for a in seen["argv"]), seen["argv"]


def test_a_failed_remote_command_does_not_print_the_box_password(monkeypatch):
    """NVCM's `_ssh_cmd` puts the password in argv (`sshpass -p <pw>`), and
    CommandError renders argv into its message.

    Callers log that message, so every failed remote command wrote the sim box's
    password to the terminal and into any captured CI output. `air up`'s
    on_complete did exactly that when publishing the route failed.
    """
    from nv_config_manager_dcim_netbox.installer import remote
    from nv_config_manager_dcim_netbox.installer.shell import CommandError

    secret = "Aq05YrTZtoj9OWd2okqtdjX6"

    class _Manager:
        ssh_password = secret

        def _ssh_cmd(self, host, port, command):
            return ["sshpass", "-p", secret, "ssh", f"{host}:{port}", command]

    def _fail(argv, stdin=None):
        # Echoed in the output too, the way ssh reports the command it ran.
        raise CommandError(argv, 1, f"tried: {' '.join(argv)}")

    monkeypatch.setattr(remote, "run", _fail)
    runner = remote.SshRunner(_Manager(), "box", 22)

    with pytest.raises(CommandError) as excinfo:
        runner.run(["helm", "upgrade", "netbox"])

    rendered = str(excinfo.value)
    assert secret not in rendered, rendered
    assert secret not in " ".join(excinfo.value.argv)
    assert secret not in excinfo.value.output
    assert "<redacted>" in rendered
    # The useful part still survives, or the error is not diagnosable.
    assert "helm" in rendered and "exited 1" in rendered
    # And the unredacted original must not resurface as exception context.
    assert excinfo.value.__suppress_context__


def test_the_bring_up_waits_out_a_settling_sshd(monkeypatch):
    """The uploads prove the box is reachable; the bring-up then opens a fresh
    connection, and a settling sshd refuses the odd one with 255.

    SshRunner retries that. Handing the command straight to NVCM's
    `_ssh_run_and_tail` would not: it does not retry, and reports a refused
    connection exactly like a failed script.
    """
    cb = _callback_for_script()
    probes = []

    class _Probing:
        def upload(self, local, remote):
            assert local.exists()

        def run(self, argv, *, stdin=None):
            probes.append(argv)
            return ""

    cb.runner = lambda h, p: _Probing()

    class _Manager:
        def _ssh_run_and_tail(self, host, port, command, *, marker, label, timeout):
            return True

    cb._manager = _Manager()
    cb.run_script("box", 22, "#!/usr/bin/env bash\necho hi\n")

    assert probes == [["true"]], "the bring-up did not establish a session first"


def test_a_refused_session_says_what_it_means(monkeypatch):
    # "The NetBox bring-up did not finish" sent us looking at the script, when
    # the script had never run.
    cb = _callback_for_script()

    class _Refusing:
        def upload(self, local, remote):
            assert local.exists()

        def run(self, argv, *, stdin=None):
            raise RuntimeError("`sshpass -p <redacted> ssh ...` exited 255\nPermission denied")

    cb.runner = lambda h, p: _Refusing()
    cb._manager = None  # must never be reached

    with pytest.raises(RuntimeError) as excinfo:
        cb.run_script("box", 22, "#!/usr/bin/env bash\necho hi\n")

    message = str(excinfo.value)
    assert "Could not open a session" in message
    assert "oob_ssh_password" in message, "does not point at the likely cause"
    # And it still must not leak the password it was given.
    assert "<redacted>" in message


def test_the_bring_up_is_bounded_on_the_box_and_never_runs_silent():
    """A hung step must end as a reported failure, not an indefinite wait.

    NVCM's `_ssh_run_and_tail` checks its deadline only inside
    `for line in proc.stdout`, so a step that produces no output never reaches
    the check, and its ssh sets no ServerAliveInterval. A NetBox bring-up once
    sat for an hour that way with nothing after `helm ... Installing it now.`

    Two guards, both on the box: `timeout` so the script always exits (the tail
    returns cleanly once remote stdout closes), and a heartbeat so their
    deadline has lines to tick on.
    """
    command = NetBoxAirCallback._bring_up_command("/home/nvcm/bring-up.sh", 3540)

    assert "timeout 3540 bash /home/nvcm/bring-up.sh" in command
    assert "NVCM_NETBOX_STILL_RUNNING" in command
    # `a && { b; } &` backgrounds the whole list, chmod included.
    assert "chmod 600 /home/nvcm/bring-up.sh || exit $?;" in command
    # Exit 124 is `timeout`'s, and says something more useful than "did not finish".
    assert 'if [ "$rc" -eq 124 ]' in command
    # Still only claims success on a zero exit.
    assert f'if [ "$rc" -eq 0 ]; then echo {BRING_UP_MARKER}' in command


def test_the_box_timeout_is_tighter_than_the_tails():
    """So an overrun surfaces as the script's own error, not the tail giving up."""
    cb = _callback_for_script()
    cb.cfg.deploy_timeout = 1800
    seen = {}

    class _Manager:
        def _ssh_run_and_tail(self, host, port, command, *, marker, label, timeout):
            seen["command"] = command
            seen["timeout"] = timeout
            return True

    cb._manager = _Manager()
    cb.runner = lambda host, port: _Uploads()
    cb.run_script("box", 22, "#!/usr/bin/env bash\necho hi\n")

    assert seen["timeout"] == 1800
    assert "timeout 1740 bash" in seen["command"]


def test_the_air_netbox_install_gets_longer_than_the_kind_default():
    """A sim box's first NetBox boot does not fit in cluster.install_netbox's 10m.

    Measured 7m58s to Ready on a `small` box with images already loaded --
    the whole 4.7 migration set against a cold database. Overrunning leaves the
    release `pending-install`, which needs a manual `helm uninstall` before a
    retry, so the margin matters more than the wait.
    """
    script = _callback_for_script().bring_up_script(
        "/home/nvcm/i.tar", "/home/nvcm/c.tgz", "/home/nvcm/w.tgz"
    )
    assert "--timeout 20m" in script, script
    # The kind path is untouched: that default still lives on install_netbox.
    assert air_callback.NETBOX_INSTALL_TIMEOUT == "20m"


def test_the_heartbeat_reports_pod_status():
    """A bare tick cannot distinguish a slow bootstrap from a stall.

    `0/1 Running` while migrations run is the difference between waiting and
    opening an SSH session to find out.
    """
    command = NetBoxAirCallback._bring_up_command("/home/nvcm/bring-up.sh", 600)

    assert "nbstat()" in command
    assert "app.kubernetes.io/name=netbox" in command
    # Interpolated per beat, not once when the wrapper is built.
    assert "$(nbstat)" in command
    # Best effort: a heartbeat must never be what fails the run.
    assert "2>/dev/null" in command


def test_tunnel_accepts_the_ngc_key_from_the_environment(tmp_path, monkeypatch):
    """`air up` worked with only $NGC_API_KEY set; `air tunnel` refused.

    AirSimulationManager resolves `ngc_api_key or os.environ["NGC_API_KEY"]`, and
    NVCM's SimConfig never reads the environment -- so gating on the config value
    alone rejected exactly what the error message told the user to do.
    """
    config = tmp_path / "air-trial.yaml"
    config.write_text("simulation_name: sim-under-test\nmock_blueprint: air_trial\n")
    monkeypatch.setenv("NGC_API_KEY", "nvapi-from-the-environment")

    called = {}

    def _endpoint(manager, simulation_name, server_name):
        called["simulation_name"] = simulation_name
        raise RuntimeError("stop here: the key check already passed")

    monkeypatch.setattr(air_cli.tunnel_mod, "find_ssh_endpoint", _endpoint)

    result = CliRunner().invoke(air_cli.air, ["tunnel", "-c", str(config), "--print"])

    assert "needs an NGC API key" not in result.output, result.output
    assert called.get("simulation_name") == "sim-under-test"


def test_tunnel_still_reports_a_genuinely_missing_key(tmp_path, monkeypatch):
    config = tmp_path / "air-trial.yaml"
    config.write_text("simulation_name: sim-under-test\nmock_blueprint: air_trial\n")
    monkeypatch.delenv("NGC_API_KEY", raising=False)

    result = CliRunner().invoke(air_cli.air, ["tunnel", "-c", str(config), "--print"])

    assert result.exit_code != 0
    assert "needs an NGC API key" in result.output


# -- provider wheels for NVCM's installer on the box ----------------------------


def test_bring_up_unpacks_the_wheelhouse_where_the_installer_looks():
    script = _callback_for_script().bring_up_script("/i.tar", "/c.tgz", "/home/nvcm/w.tgz")

    assert f"mkdir -p {air_callback.REMOTE_WHEEL_DIR}" in script
    assert f"tar -xzf /home/nvcm/w.tgz -C {air_callback.REMOTE_WHEEL_DIR}" in script


def test_upload_wheels_ships_the_provider_images_wheelhouse(monkeypatch):
    cb = _callback_for_script(provider_image="provider:test")
    commands = []
    uploads = []

    def fake_run(argv, *, stdin=None):
        commands.append(argv)
        if argv[:2] == ["docker", "create"]:
            return "cid\n"
        if argv[:2] == ["docker", "cp"]:
            Path(argv[3]).mkdir()
            (Path(argv[3]) / "nv_config_manager_dcim_netbox-0.1.0-py3-none-any.whl").write_text("")
        if argv[0] == "tar":
            Path(argv[2]).write_text("archive")
        return ""

    class _Runner(_Uploads):
        def upload(self, local, remote):
            uploads.append((local.read_text(), remote))

    monkeypatch.setattr(air_callback, "run", fake_run)
    cb.runner = lambda host, port: _Runner()

    remote = cb.upload_wheels("box", 22)

    assert commands[0] == ["docker", "create", "provider:test"]
    assert commands[1][:3] == ["docker", "cp", "cid:/plugin-wheels/."]
    assert commands[-1] == ["docker", "rm", "cid"]
    assert uploads == [("archive", remote)]


def test_upload_wheels_removes_the_container_when_the_copy_fails(monkeypatch):
    cb = _callback_for_script()
    commands = []

    def fake_run(argv, *, stdin=None):
        commands.append(argv)
        if argv[:2] == ["docker", "create"]:
            return "cid"
        if argv[:2] == ["docker", "cp"]:
            raise air_callback.CommandError(argv, 1, "no such path")
        return ""

    monkeypatch.setattr(air_callback, "run", fake_run)

    with pytest.raises(air_callback.CommandError):
        cb.upload_wheels("box", 22)
    assert commands[-1] == ["docker", "rm", "cid"]
