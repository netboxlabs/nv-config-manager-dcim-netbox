# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""DSX Air path: config, the install-config patch, and the bring-up sequence.

No simulation and no cluster. The parts that need one are covered by the
end-to-end steps in docs/air-sim.md.
"""

from __future__ import annotations

import builtins
import importlib
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml
from click.testing import CliRunner

from nv_config_manager_dcim_netbox.installer import cluster
from nv_config_manager_dcim_netbox.installer import config as base_config
from nv_config_manager_dcim_netbox.installer.air import AirSupportUnavailable
from nv_config_manager_dcim_netbox.installer.tokens import generate_api_token, is_valid_api_token

pytestmark = pytest.mark.air

# Everything below the config patch needs nv-config-manager-installer, which
# comes from an NVCM checkout rather than an index (see `make setup-air`).
# exc_type is required: the modules exist, so importing one without the extra raises
# AirSupportUnavailable, not the ModuleNotFoundError importorskip defaults to.
air_config = pytest.importorskip(
    "nv_config_manager_dcim_netbox.installer.air.config",
    reason="the `air` extra is not installed",
    exc_type=ImportError,
)
air_callback = pytest.importorskip(
    "nv_config_manager_dcim_netbox.installer.air.callback",
    reason="the `air` extra is not installed",
    exc_type=ImportError,
)
air_images = pytest.importorskip(
    "nv_config_manager_dcim_netbox.installer.air.images",
    reason="the `air` extra is not installed",
    exc_type=ImportError,
)
air_cli = pytest.importorskip(
    "nv_config_manager_dcim_netbox.installer.air.cli",
    reason="the `air` extra is not installed",
    exc_type=ImportError,
)
air_installer_config = pytest.importorskip(
    "nv_config_manager_installer.air_sim.installer_config",
    reason="the `air` extra is not installed",
    exc_type=ImportError,
)
air_orchestrator = pytest.importorskip(
    "nv_config_manager_installer.air_sim.orchestrator",
    reason="the `air` extra is not installed",
    exc_type=ImportError,
)
air_tui = pytest.importorskip(
    "nv_config_manager_dcim_netbox.installer.air.tui",
    reason="the `air` extra is not installed",
    exc_type=ImportError,
)
netbox_air_orchestrator = pytest.importorskip(
    "nv_config_manager_dcim_netbox.installer.air.orchestrator",
    reason="the `air` extra is not installed",
    exc_type=ImportError,
)
NetBoxSimConfig = air_config.NetBoxSimConfig
NetBoxAirCallback = air_callback.NetBoxAirCallback
BRING_UP_MARKER = air_callback.BRING_UP_MARKER
build_content_jobs = air_installer_config.build_content_jobs
build_template_plugins = air_installer_config.build_template_plugins
SimOrchestrator = air_orchestrator.SimOrchestrator
NetBoxSimOrchestrator = netbox_air_orchestrator.NetBoxSimOrchestrator
NetBoxAirSimApp = air_tui.NetBoxAirSimApp
NetBoxAirSimulationManager = air_tui.NetBoxAirSimulationManager
NetBoxLaunchScreen = air_tui.NetBoxLaunchScreen
NetBoxPopulationPanel = air_tui.NetBoxPopulationPanel
NetBoxTopologyScreen = air_tui.NetBoxTopologyScreen


# -- independent fabric generation and DCIM population ------------------------


def test_blueprint_still_builds_the_fabric_and_templates_without_the_dcim_job():
    """NetBox uses the blueprint without registering Nautobot's population job."""
    cfg = NetBoxSimConfig(mock_blueprint="air_trial", deployment_name="demo")

    # 1. The sim still gets a fabric to boot.
    topology_path = SimOrchestrator(cfg, _Recorder())._resolve_topology_path(cfg)
    design = yaml.safe_load(Path(topology_path).read_text())
    assert design["devices"], "the generated site design has no devices"

    # 2. The job that would load it into a DCIM we do not deploy is gone.
    jobs, run_after_deploy = build_content_jobs(cfg)
    assert not jobs and not run_after_deploy

    # 3. The templates that render the demo fabric survive.
    assert any("superpod-template-plugin" in e["path"] for e in build_template_plugins(cfg))


def test_an_explicit_topology_is_not_overwritten():
    cfg = NetBoxSimConfig(
        mock_blueprint="air_trial", deployment_name="demo", topology_path="/my/topology.yaml"
    )
    assert SimOrchestrator(cfg, _Recorder())._resolve_topology_path(cfg) == "/my/topology.yaml"


# -- config -------------------------------------------------------------------


def test_sim_clones_the_branch_this_provider_is_built_against():
    # Remote repositories clone this ref onto the sim box. It tracks NVCM_BRANCH
    # so a move off the default branch reaches the sim as well as local checkouts.
    assert NetBoxSimConfig().config_manager_ref == base_config.NVCM_BRANCH


def test_extra_keys_survive_from_yaml(tmp_path):
    # SimConfig.from_yaml filters on dataclasses.fields(cls), so the subclass's
    # fields load only because it resolves through cls.
    path = tmp_path / "sim.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "mock_blueprint": "air_trial",
                "dcim_server": "http://netbox",
                "netbox_image_tag": "9.9.9",
                "load_blueprint": False,
            }
        )
    )
    cfg = NetBoxSimConfig.from_yaml(path)
    assert cfg.mock_blueprint == "air_trial"
    assert cfg.dcim_server == "http://netbox"
    assert cfg.netbox_image_tag == "9.9.9"
    assert cfg.load_blueprint is False


def test_blueprint_dir_falls_back_to_the_clone_on_older_orchestrators():
    cfg = NetBoxSimConfig(mock_blueprint="air_trial")
    assert cfg.blueprint_dir("/home/nvcm/nv-config-manager").endswith(
        "/development/mock_topology/context/air_trial"
    )


def test_blueprint_dir_uses_staged_local_content():
    cfg = NetBoxSimConfig(mock_blueprint="air_trial")
    cfg._air_remote_mock_topology_path = "/home/nvcm/air-content/mock-topology/mock_topology"

    assert cfg.blueprint_dir("/home/nvcm/nv-config-manager") == (
        "/home/nvcm/air-content/mock-topology/mock_topology/context/air_trial"
    )


def test_air_dcim_ui_url_uses_the_tunneled_netbox_route():
    assert NetBoxSimConfig().dcim_ui_url() == "https://netbox.nvcm.air"
    assert (
        NetBoxSimConfig(dcim_public_url="https://netbox.example.com").dcim_ui_url()
        == "https://netbox.example.com"
    )


# -- the install-config patch --------------------------------------------------


def _generated_config() -> dict:
    """The shape NVCM's own generator emits: no DCIM block, bundled DCIM on."""
    return {
        "version": "1",
        "cluster": {"namespace": "nv-config-manager", "size": "medium"},
        "services": {"render": True, "nautobot": True},
        "sites": [{"name": "air-trial"}],
    }


def test_apply_dcim_selects_netbox():
    token = generate_api_token()
    patched = base_config.apply_dcim(
        _generated_config(),
        base_config.ConfigOptions(
            api_token=token,
            provider_image="reg.example/provider:1.2",
            netbox_server="http://netbox",
            netbox_public_url="http://netbox",
        ),
    )
    assert patched["dcim"]["provider"] == base_config.PROVIDER_NAME
    assert patched["dcim"]["server"] == "http://netbox"
    # Without this block the provider is never installed into NVCM's pods.
    assert patched["dcim"]["provider_packages"][0]["image"] == "reg.example/provider:1.2"
    assert patched["secrets"]["k8s"]["dcim"]["values"]["token"] == token
    assert patched["services"]["nautobot"] is False


def test_apply_dcim_leaves_the_generators_own_fields_alone():
    # cluster.* is theirs on the Air path; only the kind path sets it.
    patched = base_config.apply_dcim(
        _generated_config(), base_config.ConfigOptions(api_token=generate_api_token())
    )
    assert patched["cluster"] == {"namespace": "nv-config-manager", "size": "medium"}
    assert patched["sites"] == [{"name": "air-trial"}]
    assert patched["services"]["render"] is True


class _FakeProviderDeployment:
    api_token = "nbt_SECRETKEY123." + "B" * 40
    nats_password = "NatsProbePassword123"

    def __init__(self) -> None:
        self._manager = None
        self.calls: list[tuple] = []

    def bring_up_netbox(self, host, port, *, patch_install_config=True):
        self.calls.append(("pre", host, port, patch_install_config))

    def wait_for_intended_configs(self, host, port, expected_total):
        self.calls.append(("wait", host, port, expected_total))

    def publish_route(self, host, port):
        self.calls.append(("post", host, port))


def test_netbox_orchestrator_configures_provider_before_upload():
    provider = _FakeProviderDeployment()
    cfg = NetBoxSimConfig(mock_blueprint="air_trial")
    orchestrator = NetBoxSimOrchestrator(
        cfg,
        _Recorder(),
        provider_deployment=provider,
    )

    generated = yaml.safe_load(
        orchestrator._generate_install_yaml(cfg, "air-trial", ["192.0.2.0/24"])
    )

    assert generated["dcim"]["provider"] == "netbox"
    assert generated["dcim"]["public_url"] == "https://netbox.nvcm.air"
    assert generated["dcim"]["provider_packages"][0]["image"] == (cfg.dcim_provider_package_image)
    assert generated["secrets"]["k8s"]["nats"]["values"]["password"] == (provider.nats_password)
    assert generated["services"]["nautobot"] is False


def test_netbox_orchestrator_owns_provider_lifecycle():
    provider = _FakeProviderDeployment()
    cfg = NetBoxSimConfig()
    orchestrator = NetBoxSimOrchestrator(cfg, _Recorder(), provider_deployment=provider)
    manager = object()

    orchestrator._run_provider_pre_deploy(manager, "worker.example", 17117)
    orchestrator._wait_for_provider_configs(manager, "worker.example", 17117, 7)
    orchestrator._run_provider_post_deploy(manager, "worker.example", 17117)

    assert provider.calls == [
        ("pre", "worker.example", 17117, False),
        ("wait", "worker.example", 17117, 7),
        ("post", "worker.example", 17117),
    ]
    assert orchestrator._provider_gateway_hostnames(cfg) == (cfg.netbox_hostname(),)


def test_netbox_orchestrator_prepares_images_before_common_air_flow(monkeypatch):
    cfg = NetBoxSimConfig()
    recorder = _Recorder()
    orchestrator = NetBoxSimOrchestrator(
        cfg,
        recorder,
        provider_deployment=_FakeProviderDeployment(),
    )
    calls = []
    monkeypatch.setattr(
        netbox_air_orchestrator,
        "ensure_air_images",
        lambda provider, netbox, log: calls.append((provider, netbox)),
    )
    monkeypatch.setattr(SimOrchestrator, "_run_impl", lambda self: ("box", 22))

    assert orchestrator._run_impl() == ("box", 22)
    assert calls == [
        (
            cfg.dcim_provider_package_image,
            f"{cfg.netbox_image}:{cfg.netbox_image_tag}",
        )
    ]


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


def test_bring_up_runs_before_the_deploy_is_reported(monkeypatch):
    # on_deploy_started is the only window between "install config uploaded" and
    # "deploy runs", so the NetBox work has to finish before it is passed on.
    order: list[str] = []
    cfg = NetBoxSimConfig(mock_blueprint="air_trial")
    callback = NetBoxAirCallback(_Recorder(), cfg, manager=_FakeManager())
    monkeypatch.setattr(callback, "bring_up_netbox", lambda h, p: order.append("bring_up"))
    monkeypatch.setattr(callback._inner, "on_deploy_started", lambda h, p: order.append("inner"))

    callback.on_deploy_started("host", 22)
    assert order == ["bring_up", "inner"]


def test_inner_callback_still_runs_when_bring_up_fails(monkeypatch):
    # A failed bring-up must not swallow the orchestrator's own reporting.
    recorder = _Recorder()
    callback = NetBoxAirCallback(recorder, NetBoxSimConfig(), manager=_FakeManager())

    def _boom(host, port):
        raise RuntimeError("no cluster")

    monkeypatch.setattr(callback, "bring_up_netbox", _boom)
    with pytest.raises(RuntimeError):
        callback.on_deploy_started("host", 22)
    assert "deploy_started" in recorder.events


def test_a_generated_token_is_valid_and_reused():
    cfg = NetBoxSimConfig()
    callback = NetBoxAirCallback(_Recorder(), cfg, manager=_FakeManager())
    assert is_valid_api_token(callback.api_token)
    # Supplied tokens win, so both sides can be given a known value.
    supplied = generate_api_token()
    other = NetBoxAirCallback(
        _Recorder(), NetBoxSimConfig(dcim_token=supplied), manager=_FakeManager()
    )
    assert other.api_token == supplied


def test_a_failed_post_deploy_step_does_not_fail_a_good_deploy(monkeypatch):
    # The sim is up by this point; a cosmetic step failing must not turn a
    # working deploy into a reported failure.
    recorder = _Recorder()
    callback = NetBoxAirCallback(recorder, NetBoxSimConfig(), manager=_FakeManager())
    monkeypatch.setattr(
        callback, "publish_hostname", lambda h, p: (_ for _ in ()).throw(RuntimeError("nope"))
    )
    callback.on_complete(True, "host", 22)
    assert "complete:True" in recorder.events
    assert any("/etc/hosts" in e for e in recorder.events)


def test_netbox_is_installed_once_with_the_nats_password():
    """NVCM accepts a supplied natsPassword (installer/secrets.py), so NetBox can
    be configured with it up front instead of being upgraded a second time once
    NVCM's deployer has generated one."""
    cb = _callback_for_script()
    script = cb.bring_up_script("/i.tar", "/c.tgz")
    assert script.count("helm upgrade --install") == 1
    assert f"nvcm.natsPassword={cb.nats_password}" in script


def test_air_netbox_uses_overridable_demo_login():
    script = _callback_for_script().bring_up_script("/i.tar", "/c.tgz")
    assert "netbox.superuser.name=demo" in script
    assert "netbox.superuser.password=demo" in script

    custom = _callback_for_script(
        netbox_superuser_name="operator",
        netbox_superuser_password="lab-password",
    ).bring_up_script("/i.tar", "/c.tgz")
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
        netbox_superuser_password=r"lab,pass\word",
    ).bring_up_script("/i.tar", "/c.tgz")
    assert r"netbox.superuser.password=lab\,pass\\word" in script


def test_helm_set_value_escapes_only_what_helm_reads():
    assert cluster.helm_set_value("plain") == "plain"
    assert cluster.helm_set_value("a.b") == "a.b"
    assert cluster.helm_set_value("a,b") == r"a\,b"
    assert cluster.helm_set_value("a\\b") == r"a\\b"


def test_tui_import_failure_stays_air_support_unavailable(monkeypatch):
    """textual arrives with the `air` extra, so without it the import must not
    raise a bare ImportError.

    cli.py guards this import with `except AirSupportUnavailable`; anything else
    escaping here takes down every `air` subcommand, including `air tunnel`,
    which is documented to work without the extra.
    """
    module = "nv_config_manager_dcim_netbox.installer.air.tui"
    real_import = builtins.__import__

    def without_textual(name, *args, **kwargs):
        if name == "textual" or name.startswith("textual."):
            raise ImportError("No module named 'textual'")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, module, raising=False)
    monkeypatch.setattr(builtins, "__import__", without_textual)
    with pytest.raises(AirSupportUnavailable):
        importlib.import_module(module)


def test_generated_config_pins_the_box_password():
    """SimConfig mints a new one per run, and nothing records it.

    cloud-init sets the box's `nvcm` password from this value, so an unrecorded
    one cannot be used to SSH in for diagnosis and cannot match a box that was
    already provisioned. Re-running then fails in rsync with
    "Permission denied (publickey,password)".
    """
    body = air_cli._render_example("air_trial", "demo")
    written = yaml.safe_load(body)
    assert written["oob_ssh_password"], "no password pinned in the generated config"
    assert written["oob_ssh_password"].isalnum()

    # It must survive the round trip rather than being regenerated on load.
    cfg = NetBoxSimConfig(**{k: v for k, v in written.items() if k != "ngc_api_key"})
    assert cfg.oob_ssh_password == written["oob_ssh_password"]


def test_generated_configs_do_not_share_a_password():
    a = yaml.safe_load(air_cli._render_example("air_trial", "demo"))
    b = yaml.safe_load(air_cli._render_example("air_trial", "demo"))
    assert a["oob_ssh_password"] != b["oob_ssh_password"]


# -- shared TUI adapter --------------------------------------------------------


@pytest.mark.asyncio
async def test_tui_uses_netbox_config_and_population_controls():
    cfg = NetBoxSimConfig(ngc_api_key="nvapi-test", load_blueprint=False)
    app = NetBoxAirSimApp(config=cfg)

    async with app.run_test(size=(180, 100)):
        topology = app.query_one("#screen-topology", NetBoxTopologyScreen)
        panel = topology.query_one("#population-panel", NetBoxPopulationPanel)
        assert not panel.query_one("#load-blueprint").value
        assert not topology.query("#run-mock-topology-job")

        panel.query_one("#load-blueprint").value = True
        topology.write_to_config(cfg)
        assert cfg.load_blueprint is True


@pytest.mark.asyncio
async def test_tui_launch_uses_netbox_orchestrator_and_dashboard():
    cfg = NetBoxSimConfig(ngc_api_key="nvapi-test")
    app = NetBoxAirSimApp(config=cfg)

    async with app.run_test(size=(180, 100)) as pilot:
        app.switch_section("launch")
        await pilot.pause()
        launch = app.query_one("#screen-launch", NetBoxLaunchScreen)

        assert isinstance(launch.create_orchestrator(_Recorder()), NetBoxSimOrchestrator)
        assert isinstance(launch.create_simulation_manager(), NetBoxAirSimulationManager)
        assert launch._provider_status.display_name == "NetBox"
        assert launch._provider_status.access_url == "https://netbox.nvcm.air"


def test_netbox_provisioning_status_uses_netbox_shell(monkeypatch):
    manager = NetBoxAirSimulationManager(ngc_api_key="nvapi-test", ssh_password="test")
    monkeypatch.setattr(manager, "_ssh_cmd", lambda host, port: ["ssh", host, str(port)])
    completed = Mock(returncode=0, stdout="2/3|leaf-03\n")
    run = Mock(return_value=completed)
    monkeypatch.setattr(air_tui.subprocess, "run", run)

    assert manager.get_provisioning_status("worker.example", 17117) == (
        2,
        3,
        ["leaf-03"],
    )
    command = run.call_args.args[0][-1]
    assert "-n nv-config-manager" in command
    assert "deployment/netbox" in command
    assert "-c netbox" in command
    assert "/opt/netbox/venv/bin/python" in command
    assert "DeviceConfigStatus" in command
    assert "nautobot" not in command.lower()


def test_generated_config_exposes_air_inside_setting():
    generated = air_cli._render_example("air_trial", "demo")
    assert "# use_internal: false" in generated
    assert "# netbox_superuser_name: demo" in generated
    assert "# netbox_superuser_password: demo" in generated


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
    cfg = NetBoxSimConfig(mock_blueprint="air_trial", deployment_name="demo", **overrides)
    cb = NetBoxAirCallback.__new__(NetBoxAirCallback)
    cb.cfg = cfg
    cb.chart = Path("deploy/netbox")

    cb.api_token = "nbt_SECRETKEY123." + "B" * 40
    cb.nats_password = "NatsProbePassword123"
    return cb


def test_bring_up_is_one_script_covering_every_on_box_step():
    """NVCM runs a deploy as one command on the box and streams it back.

    A command at a time costs an authenticated SSH connection each, and any one
    of them can catch a freshly booted sshd mid-blip and lose the run.
    """
    script = _callback_for_script().bring_up_script("/home/nvcm/i.tar", "/home/nvcm/c.tgz")

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


def test_the_api_token_is_never_traced():
    # The script runs under `set -x` and its output is streamed back to the
    # operator, so any line carrying the token must disable tracing first.
    cb = _callback_for_script()
    script = cb.bring_up_script("/i.tar", "/c.tgz")

    lines = script.splitlines()
    for i, line in enumerate(lines):
        if cb.api_token in line:
            preceding = "\n".join(lines[max(0, i - 4) : i])
            assert "set +x" in preceding, f"token traced at line {i}: {line[:60]}"


def test_the_blueprint_load_is_skipped_when_asked():
    script = _callback_for_script(load_blueprint=False).bring_up_script("/i.tar", "/c.tgz")
    assert "nvcm_load_dcim" not in script


def test_the_sim_publishes_netbox_on_the_air_hostname():
    # A sim serves NVCM at nvcm.air, not the config-manager.local a kind cluster
    # uses, so the chart's default would put NetBox on a name nothing resolves.
    script = _callback_for_script().bring_up_script("/i.tar", "/c.tgz")
    assert "nvcm.gateway.baseHostname=nvcm.air" in script


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
            "NETBOX_BGP_REF": "abc123",
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
        lambda: {"NETBOX_BASE_IMAGE": "netbox-base:4.7", "NETBOX_BGP_REF": "abc"},
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
    cb.cfg = NetBoxSimConfig(mock_blueprint="air_trial")
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


def test_publishing_is_skipped_when_nothing_was_deployed(monkeypatch):
    """NVCM's orchestrator calls on_complete(success=True) even when `wait-setup`
    failed and it skipped `upload-files` and `run-deploy`.

    Trusting that produced two confusing errors -- a chart directory that was
    never uploaded, then "no NVCM hosts entry yet" -- instead of saying plainly
    that nothing had been deployed.
    """
    cb = _callback_for_script()
    cb._inner = _Recorder()
    logs: list[str] = []
    cb.on_log = logs.append
    cb.host, cb.port = "box", 22

    called = []
    cb.publish_route = lambda h, p: called.append("route")
    cb.publish_hostname = lambda h, p: called.append("hostname")
    # Stubbed, or building a real SshRunner raises and the check would answer
    # "not deployed" for the wrong reason.
    cb.runner = lambda h, p: object()
    monkeypatch.setattr(
        air_callback.cluster, "netbox_service_exists", lambda ns, runner=None: False
    )

    cb.on_complete(True, "box", 22)

    assert called == [], "published against a cluster with no NetBox on it"
    assert any("not on the sim's cluster" in line for line in logs), logs


def test_an_unreachable_box_counts_as_not_deployed():
    # The check runs over SSH, and the box may be gone by the time on_complete
    # fires. That must not turn a cosmetic step into a traceback.
    cb = _callback_for_script()
    cb._inner = _Recorder()
    cb.on_log = lambda _line: None

    def _explode(host, port):
        raise OSError("connection refused")

    cb.runner = _explode
    assert cb._netbox_is_deployed("box", 22) is False


def test_publishing_runs_once_netbox_is_actually_there(monkeypatch):
    cb = _callback_for_script()
    cb._inner = _Recorder()
    cb.on_log = lambda _line: None
    cb.host, cb.port = "box", 22

    called = []
    cb.publish_route = lambda h, p: called.append("route")
    cb.publish_hostname = lambda h, p: called.append("hostname")
    cb.runner = lambda h, p: object()
    monkeypatch.setattr(air_callback.cluster, "netbox_service_exists", lambda ns, runner=None: True)

    cb.on_complete(True, "box", 22)

    # Route first: the hostname entry is only useful once something answers on it.
    assert called == ["route", "hostname"]


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
    script = _callback_for_script().bring_up_script("/home/nvcm/i.tar", "/home/nvcm/c.tgz")
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


def test_patch_install_config_carries_the_nats_password_netbox_was_installed_with():
    """bring_up_script installs NetBox with `cb.nats_password`.

    Leaving it out of the uploaded install config makes NVCM's deployer mint its
    own for `nats-nv-config-manager`, so NetBox's publisher authenticates with a
    password NATS rejects and renders silently never happen.
    """
    from nv_config_manager_dcim_netbox.installer.air.callback import (
        CONFIG_MANAGER_INSTALL_CONFIG,
        NVCM_BOX_USER,
    )

    remote = f"/home/{NVCM_BOX_USER}/{CONFIG_MANAGER_INSTALL_CONFIG}"
    manager = _FakeManager(files={remote: yaml.safe_dump({"cluster": {"size": "small"}})})
    cb = _callback_for_script()
    cb._manager = manager

    cb.patch_install_config("host", 22)

    uploaded = yaml.safe_load(manager.files[remote])
    assert uploaded["secrets"]["k8s"]["nats"]["values"]["password"] == cb.nats_password
    assert uploaded["dcim"]["provider"] == "netbox"
