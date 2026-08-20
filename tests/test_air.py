# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""DSX Air path: config, the install-config patch, and the bring-up sequence.

No simulation and no cluster. The parts that need one are covered by the
end-to-end steps in docs/air-sim.md.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from nv_config_manager_dcim_netbox.installer import config as base_config
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
air_cli = pytest.importorskip(
    "nv_config_manager_dcim_netbox.installer.air.cli",
    reason="the `air` extra is not installed",
    exc_type=ImportError,
)
NetBoxSimConfig = air_config.NetBoxSimConfig
NetBoxAirCallback = air_callback.NetBoxAirCallback


# -- the mock-topology flag ----------------------------------------------------


def test_blueprint_still_builds_the_fabric_and_templates_without_the_dcim_job():
    """`run_mock_topology_job` drives three things; only one is unwanted.

    Clearing it outright left the orchestrator with no topology at all
    ("topology_path is required when run_mock_topology_job is disabled") and
    silently dropped the demo template plugin.
    """
    from nv_config_manager_installer.air_sim.installer_config import (
        build_content_jobs,
        build_template_plugins,
    )

    cfg = NetBoxSimConfig(mock_blueprint="air_trial", deployment_name="demo")
    air_cli._use_blueprint_without_the_dcim_job(cfg)

    # 1. The sim still gets a fabric to boot.
    assert cfg.topology_path
    design = yaml.safe_load(Path(cfg.topology_path).read_text())
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
    air_cli._use_blueprint_without_the_dcim_job(cfg)
    assert cfg.topology_path == "/my/topology.yaml"


def test_nothing_to_preserve_when_the_job_is_already_off():
    cfg = NetBoxSimConfig(mock_blueprint="air_trial", deployment_name="demo")
    cfg.run_mock_topology_job = False
    air_cli._use_blueprint_without_the_dcim_job(cfg)
    assert cfg.topology_path == ""
    assert cfg.template_plugin_paths == []


# -- config -------------------------------------------------------------------


def test_sim_clones_the_branch_that_has_the_dcim_framework():
    # SimConfig defaults this to `main`, and cloud-init clones it onto the sim
    # box. `main` carries none of the pluggable-DCIM framework, so a sim built
    # with the base default cannot deploy this provider at all.
    assert NetBoxSimConfig().config_manager_ref == base_config.NVCM_BRANCH
    assert base_config.NVCM_BRANCH != "main"


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


def test_blueprint_dir_points_into_the_clone_on_the_box():
    cfg = NetBoxSimConfig(mock_blueprint="air_trial")
    assert cfg.blueprint_dir("/home/nvcm/nv-config-manager").endswith(
        "/development/mock_topology/context/air_trial"
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
    """`make images` builds for the host; a sim box is x86_64.

    Built on Apple Silicon and loaded onto the box, the containers die instantly
    with "exec format error", which surfaces ten minutes later as a helm timeout
    with nothing pointing at the cause.
    """
    cb = _arch_callback(monkeypatch, "x86_64", "arm64")
    with pytest.raises(RuntimeError) as excinfo:
        cb.check_image_arch("host", 22)
    message = str(excinfo.value)
    assert "amd64" in message
    # It has to say how to fix it, not just that it is wrong.
    assert "make images PLATFORM=linux/amd64" in message


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
    """sshpass's own help calls `-p` "security unwise" — any same-user `ps` reads
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

    SshRunner retries that, but the bring-up used to hand the command straight to
    NVCM's `_ssh_run_and_tail`, which does not retry and reports a refused
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
