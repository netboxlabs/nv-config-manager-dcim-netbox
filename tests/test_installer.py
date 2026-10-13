# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""The `nvcm-netbox` CLI, the cluster helpers, the chart and the air-gapped bundle.

No cluster required. The parts that need one (helm install, kubectl exec) are
covered by the end-to-end steps in docs/deployment.md instead. The installer
provider plugin itself is covered by test_plugin.py.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner
from nv_config_manager_dcim import DCIM_EVENT_CONTRACT_VERSION
from nv_config_manager_installer.deployer import StepStatus

from nv_config_manager_dcim_netbox.installer import cli, cluster, standalone
from nv_config_manager_dcim_netbox.installer.config import (
    DEFAULT_PROFILE,
    EVENT_STREAM,
    EVENT_SUBJECT,
)
from nv_config_manager_dcim_netbox.installer.shell import require_tools
from nv_config_manager_dcim_netbox.installer.tokens import (
    TOKEN_KEY_LENGTH,
    TOKEN_SECRET_LENGTH,
    generate_api_token,
    generate_nats_password,
    is_valid_api_token,
)

pytestmark = pytest.mark.installer

_REPO = Path(__file__).resolve().parents[1]
_CHART = _REPO / "deploy" / "netbox"


@pytest.fixture(autouse=True)
def _no_deployed_nats(monkeypatch):
    """Answer "no NATS deployed" unless a test says otherwise.

    `verify` reads the running NVCM's NATS password through kubectl. Left alone,
    that depends on whatever cluster the developer's kubeconfig points at.
    """
    monkeypatch.setattr(cluster, "read_nats_password", lambda *a, **k: None)


# -- CLI ----------------------------------------------------------------------


def test_load_topology_names_the_blueprint_root_it_tried(tmp_path, monkeypatch):
    # A relative default resolved against the wrong working directory is
    # invisible unless the error prints the absolute path it tried.
    monkeypatch.setenv("NVCM_REPO", str(tmp_path / "nope"))
    monkeypatch.setattr(cli, "require_tools", lambda *names: None)

    result = CliRunner().invoke(cli.main, ["netbox", "load-topology"])

    assert result.exit_code != 0
    assert str(tmp_path / "nope" / "development" / "mock_topology" / "context") in result.output
    assert "$NVCM_REPO" in result.output


def test_load_topology_merges_the_shared_defaults(tmp_path, monkeypatch):
    root = tmp_path / "development" / "mock_topology" / "context"
    (root / "common").mkdir(parents=True)
    (root / "common" / "defaults.yaml").write_text("global_defaults: {}\n")
    (root / "air_trial").mkdir()
    (root / "air_trial" / "site.yaml").write_text("devices: []\n")
    monkeypatch.setenv("NVCM_REPO", str(tmp_path))
    monkeypatch.setattr(cli, "require_tools", lambda *names: None)
    seen = {}

    def fake_load(blueprint, namespace, *, deployment_name, runner=None):
        seen["files"] = sorted(p.name for p in Path(blueprint).iterdir())
        seen["deployment_name"] = deployment_name
        return "loaded"

    monkeypatch.setattr(cluster, "load_topology", fake_load)

    result = CliRunner().invoke(cli.main, ["netbox", "load-topology"])

    assert result.exit_code == 0, result.output
    assert seen == {"files": ["defaults.yaml", "site.yaml"], "deployment_name": "air_trial"}


def _netbox_up(monkeypatch, *args, **hook_results):
    """Run `netbox up` with the cluster side faked; return the result and calls."""
    calls: list = []
    monkeypatch.setattr(cli, "require_tools", lambda *names: None)
    monkeypatch.setattr(standalone, "K8sClient", lambda: _FakeK8s())
    monkeypatch.setattr(cluster, "pending_release_status", lambda *a: hook_results.get("stuck"))
    monkeypatch.setattr(cluster, "orphaned_data_volumes", lambda *a: hook_results.get("stale", []))
    monkeypatch.setattr(cluster, "helm_dependency_build", lambda chart: None)
    monkeypatch.setattr(
        cluster, "install_netbox", lambda chart, **kwargs: calls.append(("install", kwargs))
    )
    monkeypatch.setattr(cluster, "wait_for_netbox", lambda ns, timeout: calls.append(("wait",)))
    monkeypatch.setattr(
        cluster, "run_nvcm_init", lambda ns, token: calls.append(("init", token)) or ""
    )
    result = CliRunner().invoke(cli.main, ["netbox", "up", *args])
    return result, calls


class _FakeK8s:
    secrets: dict[str, dict[str, str]] = {}

    def __init__(self) -> None:
        self.secrets = dict(_FakeK8s.secrets)

    def ensure_namespace(self, name):
        return True

    def secret_exists(self, name, namespace):
        return name in self.secrets

    def read_secret_data(self, name, namespace):
        return dict(self.secrets.get(name, {}))

    def apply_secret(self, name, namespace, data):
        self.secrets[name] = dict(data)
        _FakeK8s.secrets[name] = dict(data)


@pytest.fixture(autouse=True)
def _fresh_fake_cluster():
    _FakeK8s.secrets = {}
    yield
    _FakeK8s.secrets = {}


@pytest.mark.skipif(
    not any(
        ep.name == "netbox"
        for ep in __import__("importlib.metadata", fromlist=["entry_points"]).entry_points(
            group="nv_config_manager.installer.dcim"
        )
    ),
    reason="package not installed with [installer]",
)
def test_netbox_up_installs_netbox_with_the_token_nvcm_will_find(monkeypatch):
    """`netbox up` runs the provider's pre-Helm steps on their own and leaves the
    dcim-token Secret behind, so NVCM's deploy keeps it and both sides agree."""
    result, calls = _netbox_up(monkeypatch)

    assert result.exit_code == 0, result.output
    assert [c[0] for c in calls] == ["install", "wait", "init"]
    token = calls[0][1]["api_token"]
    assert is_valid_api_token(token)
    assert _FakeK8s.secrets["dcim-token"]["token"] == token
    assert calls[0][1]["nats_password"] == _FakeK8s.secrets["nats-nv-config-manager"]["password"]
    assert calls[2] == ("init", token)
    assert "NetBox is up." in result.output
    assert "get secret dcim-token" in result.output


@pytest.mark.skipif(
    not any(
        ep.name == "netbox"
        for ep in __import__("importlib.metadata", fromlist=["entry_points"]).entry_points(
            group="nv_config_manager.installer.dcim"
        )
    ),
    reason="package not installed with [installer]",
)
def test_netbox_up_reports_a_stuck_release_without_a_traceback(monkeypatch):
    result, calls = _netbox_up(monkeypatch, stuck="pending-install")

    assert result.exit_code != 0
    assert "pending-install" in result.output
    assert "helm uninstall netbox -n nv-config-manager" in result.output
    assert "Traceback" not in result.output
    assert calls == []


def test_netbox_up_refuses_an_external_netbox_config(tmp_path, monkeypatch):
    config = yaml.safe_load(DEFAULT_PROFILE.read_text())
    config["dcim"]["local"] = False
    config["dcim"]["server"] = "https://netbox.example.com"
    path = tmp_path / "install.yaml"
    path.write_text(yaml.safe_dump(config))
    monkeypatch.setattr(cli, "require_tools", lambda *names: None)

    result = CliRunner().invoke(cli.main, ["netbox", "up", "-c", str(path)])

    assert result.exit_code != 0
    assert "dcim.local: false" in result.output


def test_step_printer_shows_why_a_step_was_skipped(capsys):
    from nv_config_manager_installer.deployer import DeployStep

    step = DeployStep("install-netbox", "Install NetBox")
    step.status = StepStatus.SKIPPED
    step.output.append("Dry run")
    cli._StepPrinter().on_step_update(step)
    out = capsys.readouterr().out
    assert "Install NetBox" in out and "Dry run" in out


def test_local_postgresql_has_render_batch_headroom():
    chart_values = yaml.safe_load((_CHART / "values.yaml").read_text())

    resources = chart_values["netbox"]["postgresql"]["primary"]["resources"]
    assert resources["requests"]["memory"] == "256Mi"
    assert resources["limits"]["memory"] == "1Gi"


# -- tokens -------------------------------------------------------------------


def test_generated_token_matches_netbox_v2_format():
    # Mirrors netbox/users/constants.py. The plugin's nvcm_init splits on the dot
    # and stores key + secret separately; a malformed token is silently skipped,
    # so the shape has to be exact.
    token = generate_api_token()
    assert token.startswith("nbt_")
    key, secret = token[4:].split(".", 1)
    assert len(key) == TOKEN_KEY_LENGTH == 12
    assert len(secret) == TOKEN_SECRET_LENGTH == 40
    assert key.isalnum() and secret.isalnum()


def test_generated_tokens_are_unique():
    assert len({generate_api_token() for _ in range(50)}) == 50


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not-a-token",
        "nbt_short.abc",
        # v1-style, no dot
        "0123456789abcdef0123456789abcdef01234567",
        # right shape, wrong prefix
        "tok_AAAAAAAAAAAA." + "B" * 40,
        # non-alphanumeric in the secret
        "nbt_AAAAAAAAAAAA." + "-" * 40,
    ],
)
def test_invalid_tokens_are_rejected(value):
    assert not is_valid_api_token(value)


def test_valid_token_accepted():
    assert is_valid_api_token(generate_api_token())


def test_require_tools_names_everything_missing():
    with pytest.raises(RuntimeError) as excinfo:
        require_tools("definitely-not-a-real-binary-xyz", "also-not-real-xyz")
    message = str(excinfo.value)
    assert "definitely-not-a-real-binary-xyz" in message
    assert "also-not-real-xyz" in message


# -- helm invocation ----------------------------------------------------------


def test_chart_dependency_build_registers_the_upstream_repository(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(cluster, "stream", lambda argv: calls.append(argv))

    cluster.helm_dependency_build(tmp_path)

    assert calls == [
        [
            "helm",
            "repo",
            "add",
            cluster.NETBOX_CHART_REPOSITORY,
            cluster.NETBOX_CHART_REPOSITORY_URL,
            "--force-update",
        ],
        ["helm", "dependency", "build", str(tmp_path)],
    ]


class _RecordingRunner:
    """Captures the argv `cluster` builds instead of running it."""

    def __init__(self) -> None:
        self.argv: list[str] = []

    def run(self, argv, *, stdin=None):
        self.argv = list(argv)
        return ""

    def stream(self, argv):
        self.argv = list(argv)

    def upload(self, local, remote):
        raise NotImplementedError


def _install_argv(**kwargs):
    runner = _RecordingRunner()
    cluster.install_netbox(
        "chart",
        release="nb",
        namespace="ns",
        api_token="nbt_x.y",
        image_repository="nv-config-manager-netbox",
        runner=runner,
        **kwargs,
    )
    return runner.argv


def test_install_netbox_omits_the_image_tag_when_none_was_requested():
    # `--set netbox.image.tag=` overrides the chart's tag with an empty string; the
    # subchart then falls back to its appVersion, an image that was never built.
    argv = _install_argv()
    assert not any(arg.startswith("netbox.image.tag") for arg in argv)
    assert "netbox.image.repository=nv-config-manager-netbox" in argv


def test_install_netbox_sets_an_explicit_image_tag():
    assert "netbox.image.tag=1.2.3" in _install_argv(image_tag="1.2.3")


def test_install_netbox_passes_the_workflow_url_through():
    assert "nvcm.workflowUrl=https://cm.local" in _install_argv(workflow_url="https://cm.local")


def test_install_netbox_omits_an_unset_workflow_url():
    # The chart guards on it, and an empty --set would render `workflow_url: ""`,
    # which the plugin reads as configured and turns into links to nowhere.
    assert not any(arg.startswith("nvcm.workflowUrl") for arg in _install_argv(workflow_url=""))


def test_workflow_url_is_the_cluster_hostname_under_the_gateway_scheme():
    assert cluster.workflow_url("config-manager.local") == "https://config-manager.local"
    assert cluster.workflow_url("config-manager.local", tls=False) == "http://config-manager.local"
    # Trailing slashes would double up against the `/workflows/...` the plugin appends.
    assert cluster.workflow_url("cm.local/") == "https://cm.local"


def test_workflow_url_is_empty_without_a_hostname():
    # Not "https://": the plugin treats any non-empty value as configured, so a
    # scheme-only URL would render Deploy as a link to nowhere instead of text.
    assert cluster.workflow_url(None) == ""
    assert cluster.workflow_url("") == ""
    assert cluster.workflow_url("   ") == ""


# -- chart --------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
@pytest.mark.skipif(
    not (_CHART / "charts").is_dir(), reason="chart deps not fetched (helm dependency build)"
)
def test_chart_renders_and_wires_the_publisher():
    rendered = subprocess.run(
        [
            "helm",
            "template",
            "nb",
            str(_CHART),
            "--set",
            f"netbox.superuser.apiToken={generate_api_token()}",
            "--set",
            "nvcm.natsPassword=test-password",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    # Both plugins enabled, or the provider has no models and no BGP data.
    assert "netbox_nv_config_manager" in rendered
    assert "netbox_bgp" in rendered
    # The publisher config must reach NetBox as a Secret, not chart values --
    # it carries the NATS password.
    assert "nvcm-netbox-publisher" in rendered
    assert "event_publisher: nats" in rendered
    # Stable names the CLI and the installer profile both hardcode.
    assert "\n  name: netbox\n" in rendered

    # Authentication required. The upstream chart defaults this off, NetBox 4.7
    # warns about it and 5.0 drops unauthenticated access entirely.
    #
    # Pinned because the obvious reason to flip it back -- silencing the startup
    # FutureWarning -- does not work: NetBox warns either way, since the chart
    # renders LOGIN_REQUIRED unconditionally. Turning it off buys the *louder*
    # warning and an open instance. See deploy/netbox/values.yaml.
    assert "LOGIN_REQUIRED: true" in rendered
    # Not set here, so the Deploy / See Diff controls stay inert rather than
    # linking into a workflow UI this release knows nothing about.
    assert "workflow_url" not in rendered


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
@pytest.mark.skipif(
    not (_CHART / "charts").is_dir(), reason="chart deps not fetched (helm dependency build)"
)
def test_chart_wires_the_workflow_url_into_the_plugin_config():
    rendered = subprocess.run(
        [
            "helm",
            "template",
            "nb",
            str(_CHART),
            "--set",
            f"netbox.superuser.apiToken={generate_api_token()}",
            "--set",
            "nvcm.workflowUrl=https://config-manager.local",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert 'workflow_url: "https://config-manager.local"' in rendered


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
@pytest.mark.skipif(not (_CHART / "charts").is_dir(), reason="chart deps not fetched")
def test_chart_image_tag_must_not_be_overridden_with_an_empty_string():
    # Why install_netbox omits the flag: the subchart's image helper treats a falsy
    # tag as unset and falls back to its appVersion.
    def rendered_netbox_image(*extra):
        out = subprocess.run(
            ["helm", "template", "nb", str(_CHART), *extra],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        return {
            line.split("image:", 1)[1].strip().strip('"')
            for line in out.splitlines()
            if "image:" in line and "nv-config-manager-netbox" in line
        }

    chart_values = yaml.safe_load((_CHART / "values.yaml").read_text())
    chart_tag = chart_values["netbox"]["image"]["tag"]
    assert rendered_netbox_image() == {f"nv-config-manager-netbox:{chart_tag}"}
    # An empty override does not keep the chart's tag -- it replaces it.
    assert rendered_netbox_image("--set", "netbox.image.tag=") != {
        f"nv-config-manager-netbox:{chart_tag}"
    }


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
@pytest.mark.skipif(not (_CHART / "charts").is_dir(), reason="chart deps not fetched")
def test_chart_falls_back_to_log_publisher_without_a_nats_password():
    # NetBox must come up before NVCM exists (it owns the API token), so a missing
    # NATS password has to degrade rather than fail.
    rendered = subprocess.run(
        ["helm", "template", "nb", str(_CHART)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "event_publisher: log" in rendered
    assert "event_publisher: nats" not in rendered


def test_publish_test_event_keeps_the_password_out_of_argv():
    # As a `kubectl run` argument it is persisted in the pod spec; use stdin.
    runner = _RecordingRunner()
    cluster.publish_test_event("ns", "subject", "s3cr3t-nats-pw", "7", runner=runner)
    assert "s3cr3t-nats-pw" not in " ".join(runner.argv)
    assert runner.argv[-2:] == ["sh", "-s"]


def test_install_netbox_appends_extra_set_overrides_last():
    # netbox_helm_set was a documented field nothing read, so it was dropped.
    argv = _install_argv(image_tag="1.2.3", extra_set=["netbox.replicaCount=3"])
    assert "netbox.replicaCount=3" in argv
    assert argv.index("netbox.replicaCount=3") > argv.index("netbox.image.tag=1.2.3")


# -- locating the NVCM checkout ------------------------------------------------


def test_nvcm_repo_precedence_flag_then_env_then_default(tmp_path, monkeypatch):
    from nv_config_manager_dcim_netbox.installer.cli import _nvcm_repo_candidate

    monkeypatch.setenv("NVCM_REPO", str(tmp_path / "from-env"))
    assert _nvcm_repo_candidate(None)[0] == tmp_path / "from-env"
    # An explicit flag wins over the environment.
    assert _nvcm_repo_candidate(tmp_path / "from-flag")[0] == tmp_path / "from-flag"

    monkeypatch.delenv("NVCM_REPO")
    path, source = _nvcm_repo_candidate(None)
    assert path.name == "nv-config-manager" and source == "the default"


# -- failure reporting ---------------------------------------------------------


def test_a_failed_tool_call_is_an_error_not_a_traceback(monkeypatch):
    # helm and kubectl failures are ordinary outcomes; a stack trace buries the
    # tool's own message, which is the part that says what went wrong.
    from nv_config_manager_dcim_netbox.installer.shell import CommandError

    @cli.main.command("boom")
    def _boom():
        raise CommandError(["helm", "upgrade"], 1)

    result = CliRunner().invoke(cli.main, ["boom"])
    assert result.exit_code != 0
    assert "Error: `helm upgrade` exited 1" in result.output
    assert "Traceback" not in result.output


def test_data_volumes_are_orphaned_only_once_their_secret_is_gone(monkeypatch):
    from nv_config_manager_dcim_netbox.installer.shell import CommandError

    pvcs = "data-netbox-postgresql-0\nvalkey-data-netbox-valkey-primary-0\nother-pvc\n"

    def fake_kubectl(*args, namespace=None, stdin=None, runner=None):
        if args[1] == "pvc":
            return pvcs
        raise CommandError(["kubectl", "get", "secret"], 1)

    monkeypatch.setattr(cluster, "kubectl", fake_kubectl)
    assert cluster.orphaned_data_volumes("netbox", "nv-config-manager") == [
        "data-netbox-postgresql-0",
        "valkey-data-netbox-valkey-primary-0",
    ]

    # The Secret survives uninstall, so its presence means the passwords still
    # match the data on those volumes and they are fine to reuse.
    monkeypatch.setattr(
        cluster, "kubectl", lambda *a, namespace=None, stdin=None, runner=None: pvcs
    )
    assert cluster.orphaned_data_volumes("netbox", "nv-config-manager") == []


# -- air-gapped bundle --------------------------------------------------------

_AIRGAPPED = _REPO / "deploy" / "airgapped"


@pytest.fixture
def airgapped_images():
    """The bundle's image list, or a skip when the registry is unreachable.

    Resolving digests needs Docker Hub, which rate-limits anonymous callers, so
    this cannot be a hard dependency of the suite.
    """
    if shutil.which("helm") is None:
        pytest.skip("helm not installed")
    if not (_CHART / "charts").is_dir():
        pytest.skip("chart deps not fetched (helm dependency build)")
    result = subprocess.run(
        [str(_AIRGAPPED / "netbox-images.sh")], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        if "Could not resolve a digest" in result.stderr:
            pytest.skip(f"registry unreachable: {result.stderr.strip().splitlines()[-1]}")
        pytest.fail(result.stderr)
    return result.stdout.split()


def test_airgapped_image_list_covers_the_chart_and_the_provider(airgapped_images):
    # Read off the rendered chart rather than hand-maintained, so a chart bump
    # cannot silently drop an image from the bundle.
    images = airgapped_images

    assert any(i.startswith("nv-config-manager-netbox:") for i in images)
    # In neither chart: NVCM installs it from dcim.provider_packages.
    assert any(i.startswith("nv-config-manager-dcim-netbox:") for i in images)
    assert any("postgresql" in i for i in images)
    assert any("valkey" in i for i in images)


def test_airgapped_images_from_a_registry_are_digest_pinned(airgapped_images):
    # The bitnami subcharts ship `tag: latest`, which means nothing offline and
    # drifts between building a bundle and installing from it.
    for image in airgapped_images:
        if image.startswith("nv-config-manager-"):
            continue  # Ours, in no registry; carried as a tarball.
        assert "@sha256:" in image, f"{image} is not digest-pinned"


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
@pytest.mark.skipif(
    not (_CHART / "charts").is_dir(), reason="chart deps not fetched (helm dependency build)"
)
def test_every_chart_image_can_be_repointed_at_a_private_registry():
    # install-netbox.sh repoints these. If a chart bump adds an image with its
    # own registry value, this catches it before an offline install does.
    overrides = [
        "netbox.global.security.allowInsecureImages=true",
        "netbox.image.registry=reg.example.com/x",
        "netbox.postgresql.image.registry=reg.example.com/x",
        "netbox.valkey.image.registry=reg.example.com/x",
        "netbox.init.image.registry=reg.example.com/x",
        "netbox.test.image.registry=reg.example.com/x",
        "netbox.worker.waitForBackend.image.registry=reg.example.com/x",
    ]
    argv = ["helm", "template", "nb", str(_CHART)]
    for override in overrides:
        argv += ["--set", override]
    rendered = subprocess.run(argv, capture_output=True, text=True, check=False)
    assert rendered.returncode == 0, rendered.stderr

    stray = []
    for line in rendered.stdout.splitlines():
        stripped = line.strip()
        if not stripped.startswith("image:"):
            continue
        image = stripped.split(":", 1)[1].strip().strip('"')
        if image.startswith("reg.example.com/"):
            continue
        stray.append(image)
    # The `helm test` hook hardcodes busybox. Nothing creates that Pod during an
    # install, so it does not affect an offline deployment.
    assert all(s.startswith("busybox") for s in stray), stray


def test_the_image_list_creates_its_output_directory(tmp_path, monkeypatch):
    """`make airgapped-images` writes into gitignored generated/, which nothing
    creates.

    Piping through `tee` masked this: a `tee` that cannot open its file exits 1,
    the `|| cat` fallback then drained an already-consumed stdin, and the pipeline
    still reported success -- so the target printed nothing, wrote nothing, and
    exited 0.
    """
    if shutil.which("helm") is None:
        pytest.skip("helm not installed")
    if not (_CHART / "charts").is_dir():
        pytest.skip("chart deps not fetched (helm dependency build)")

    import os

    # Digest resolution goes to Docker Hub, which rate-limits anonymous callers.
    # Stubbed so this tests the script's own output handling rather than the
    # network; the digests themselves are covered by the fixture above.
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    docker = stub_bin / "docker"
    docker.write_text(f"#!/bin/sh\necho sha256:{'b' * 64}\n")
    docker.chmod(0o755)

    out = tmp_path / "does" / "not" / "exist" / "images.txt"
    result = subprocess.run(
        [str(_AIRGAPPED / "netbox-images.sh"), "-o", str(out)],
        capture_output=True,
        text=True,
        check=False,
        env=dict(os.environ, PATH=f"{stub_bin}:{os.environ['PATH']}"),
    )
    assert result.returncode == 0, result.stderr
    assert out.is_file(), "the output file was not written"
    assert out.read_text().strip(), "the output file is empty"
    # Also echoed, so the calling script can log what it collected.
    assert result.stdout.strip() == out.read_text().strip()


def _bundle(tmp_path: Path, image_list: str) -> Path:
    """Lay out a bundle the way create-airgapped.sh does, around the real script."""
    root = tmp_path / "bundle"
    (root / "netbox-chart").mkdir(parents=True)
    (root / "images").mkdir()
    (root / "images" / "image-list.txt").write_text(image_list)
    shutil.copy(_AIRGAPPED / "install-netbox.sh", root / "install-netbox.sh")
    (root / "install-netbox.sh").chmod(0o755)

    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    for tool in ("helm", "kubectl"):
        script = stub_bin / tool
        script.write_text(f'#!/bin/sh\necho "$@" >> "{tmp_path}/{tool}.argv"\nexit 0\n')
        script.chmod(0o755)
    return root


def _run_install(root: Path, tmp_path: Path):
    import os

    env = dict(os.environ, PATH=f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    return subprocess.run(
        [str(root / "install-netbox.sh"), "--registry", "reg.example.com/nvcm"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_airgapped_install_mints_a_token_under_pipefail(tmp_path):
    """`head -c` closes the pipe as soon as it has enough, so the `tr` reading
    /dev/urandom takes SIGPIPE and the pipeline exits 141.

    Under `set -euo pipefail` that aborted the script at the token line, with no
    message and nothing installed -- on the documented default path, where no
    --token is passed.
    """
    root = _bundle(tmp_path, "docker.io/bitnami/postgresql@sha256:aaa\n")
    result = _run_install(root, tmp_path)

    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    tokens = [w for w in result.stdout.split() if w.startswith("nbt_")]
    assert tokens, result.stdout
    assert is_valid_api_token(tokens[0]), tokens[0]


def test_airgapped_install_asks_for_the_tags_the_bundle_actually_pushed(tmp_path):
    """NVCM's upload-to-registry.sh rewrites a digest reference into a *tag*
    (`target_ref`: `foo@sha256:abc` -> `$REGISTRY/foo:sha256-abc`), and
    netbox-images.sh digest-pins every image in the bundle that came from a
    registry.

    So overriding image.registry alone left the chart asking for the original
    `:latest` / `:1.38.0` tags, which were never pushed: ImagePullBackOff on every
    pod of an air-gapped install.
    """
    root = _bundle(
        tmp_path,
        "registry-1.docker.io/bitnami/postgresql@sha256:aaa\n"
        "registry-1.docker.io/bitnami/valkey@sha256:bbb\n"
        "docker.io/busybox@sha256:ccc\n"
        "docker.io/rancher/kubectl@sha256:ddd\n"
        "nv-config-manager-netbox:0.1.0\n",
    )
    result = _run_install(root, tmp_path)
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"

    argv = (tmp_path / "helm.argv").read_text()
    for expected in (
        "netbox.postgresql.image.tag=sha256-aaa",
        "netbox.valkey.image.tag=sha256-bbb",
        "netbox.init.image.tag=sha256-ccc",
        "netbox.test.image.tag=sha256-ccc",
        "netbox.worker.waitForBackend.image.tag=sha256-ddd",
    ):
        assert expected in argv, f"{expected} not in {argv}"


def test_airgapped_install_says_so_when_it_cannot_map_a_tag(tmp_path):
    # Silence here means an ImagePullBackOff ten minutes into an offline install
    # with nothing pointing at the cause.
    root = _bundle(tmp_path, "registry-1.docker.io/bitnami/postgresql@sha256:aaa\n")
    result = _run_install(root, tmp_path)

    assert result.returncode == 0, result.stderr
    assert "bitnami/valkey" in result.stderr
    assert "netbox.valkey.image.tag" in result.stderr


def test_airgapped_install_wires_a_pull_secret_into_every_pod(tmp_path):
    """The upload step authenticates, so the registry it pushes to usually needs
    credentials to pull from as well.

    One `netbox.global.imagePullSecrets` covers all six pod specs: Helm
    propagates a chart's `global` down through the bitnami subcharts.
    """
    root = _bundle(tmp_path, "nv-config-manager-netbox:0.1.0\n")
    import os

    env = dict(os.environ, PATH=f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    result = subprocess.run(
        [
            str(root / "install-netbox.sh"),
            "--registry",
            "reg.example.com/nvcm",
            "--pull-secret",
            "my-creds",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    argv = (tmp_path / "helm.argv").read_text()
    assert "netbox.global.imagePullSecrets[0].name=my-creds" in argv, argv


def test_airgapped_install_creates_the_pull_secret_from_credentials(tmp_path):
    # The secret is per-host; --registry carries the project path too.
    root = _bundle(tmp_path, "nv-config-manager-netbox:0.1.0\n")
    import os

    env = dict(os.environ, PATH=f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    result = subprocess.run(
        [
            str(root / "install-netbox.sh"),
            "--registry",
            "reg.example.com/nvcm",
            "--registry-username",
            "robot",
            "--registry-password",
            "hunter2",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    kubectl = (tmp_path / "kubectl.argv").read_text()
    assert "--docker-server=reg.example.com" in kubectl, kubectl
    assert "--docker-username=robot" in kubectl, kubectl
    argv = (tmp_path / "helm.argv").read_text()
    assert "netbox.global.imagePullSecrets[0].name=netbox-registry-credentials" in argv, argv


def test_the_chart_carries_a_pull_secret_into_every_pod_spec():
    """`netbox.global` is the netbox subchart's global, not Helm's top-level one.

    It still reaches PostgreSQL and Valkey because Helm propagates each level's
    global downwards -- but that is worth pinning, since the alternative is a
    secret that silently covers only the NetBox pods.
    """
    if shutil.which("helm") is None:
        pytest.skip("helm not installed")
    if not (_CHART / "charts").is_dir():
        pytest.skip("chart deps not fetched (helm dependency build)")
    rendered = subprocess.run(
        [
            "helm",
            "template",
            "nb",
            str(_CHART),
            "--set",
            "netbox.global.imagePullSecrets[0].name=creds",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rendered.returncode == 0, rendered.stderr
    specs = [d for d in yaml.safe_load_all(rendered.stdout) if d]
    pod_specs = [
        d["spec"]["template"]["spec"]
        for d in specs
        if d.get("kind") in {"Deployment", "StatefulSet"}
    ]
    assert pod_specs, "no workloads rendered"
    for spec in pod_specs:
        names = [s.get("name") for s in spec.get("imagePullSecrets", [])]
        assert "creds" in names, spec.get("containers", [{}])[0].get("image")


# -- the bundle build ---------------------------------------------------------


def _fake_nvcm_repo(tmp_path: Path) -> Path:
    """An NVCM checkout whose create-airgapped.sh only records how it was called."""
    repo = tmp_path / "nvcm"
    (repo / "deploy" / "airgapped").mkdir(parents=True)
    theirs = repo / "deploy" / "airgapped" / "create-airgapped.sh"
    theirs.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" > "{tmp_path}/theirs.argv"\n'
        # A bundle in the layout the merge step asserts.
        'out=""; prev=""\n'
        'for a in "$@"; do [ "$prev" = "--output" ] && out="$a"; prev="$a"; done\n'
        'root="$out/nv-config-manager-airgapped-v0.1.0-test"\n'
        'mkdir -p "$root/helm" "$root/images" "$root/installer"\n'
        'echo "# bundle" > "$root/README.md"\n'
        'cd "$out"\n'
        "tar -czf nv-config-manager-airgapped-v0.1.0-test.tar.gz "
        "nv-config-manager-airgapped-v0.1.0-test\n"
        "rm -rf nv-config-manager-airgapped-v0.1.0-test\n"
        "shasum -a 256 nv-config-manager-airgapped-v0.1.0-test.tar.gz "
        "> nv-config-manager-airgapped-v0.1.0-test.tar.gz.sha256\n"
    )
    theirs.chmod(0o755)
    return repo


def _run_bundle(tmp_path: Path, *args: str, images: str = "nv-config-manager-netbox:0.1.0\n"):
    """Run the real wrapper inside a copy of the repo layout it expects.

    A copy rather than the checkout itself: the wrapper calls
    `netbox-images.sh` by path, and that one needs helm and a live registry.
    What is under test is what the wrapper does with the list, not how the list
    is produced.
    """
    import os

    repo = _fake_nvcm_repo(tmp_path)
    here = tmp_path / "provider" / "deploy" / "airgapped"
    here.mkdir(parents=True)
    shutil.copy(_AIRGAPPED / "create-airgapped.sh", here / "create-airgapped.sh")
    (here / "create-airgapped.sh").chmod(0o755)
    (here / "install-netbox.sh").write_text("#!/bin/sh\nexit 0\n")
    lister = here / "netbox-images.sh"
    lister.write_text(f'#!/bin/sh\nprintf %s \'{images}\' > "$2"\ncat "$2"\n')
    lister.chmod(0o755)
    # The chart is copied into the bundle, and its dependency must already be
    # vendored or the wrapper runs `helm dependency build` against the network.
    chart = tmp_path / "provider" / "deploy" / "netbox"
    (chart / "charts").mkdir(parents=True)
    (chart / "Chart.yaml").write_text("name: nvcm-netbox\n")
    (chart / "charts" / "netbox-0.0.0.tgz").write_bytes(b"")

    stub_bin = tmp_path / "bin"
    stub_bin.mkdir(exist_ok=True)
    (stub_bin / "docker").write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" >> "{tmp_path}/docker.argv"\n'
        # `docker image inspect --format {{.Architecture}}` and the plain
        # existence check both go through here.
        'case "$*" in *Architecture*) echo "$NVCM_TEST_ARCH" ;; esac\n'
        "exit 0\n"
    )
    (stub_bin / "docker").chmod(0o755)

    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    env = dict(
        os.environ,
        PATH=f"{stub_bin}:{os.environ['PATH']}",
        NVCM_REPO=str(repo),
        OUTPUT_DIR=str(out),
        NVCM_TEST_ARCH=os.environ.get("NVCM_TEST_ARCH", "arm64"),
    )
    result = subprocess.run(
        [str(here / "create-airgapped.sh"), *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    return result, out, tmp_path


def test_the_bundle_build_asks_for_the_local_image_fallback(tmp_path):
    """Both of this bundle's own images exist in no registry, so their script's
    `docker pull` of them fails and, without the flag, it stops with an
    incomplete image set rather than saving what `make images` built.
    """
    result, _, tmp = _run_bundle(tmp_path, "--version", "v0.1.0", "--arch", "arm64")
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    argv = (tmp / "theirs.argv").read_text().split("\n")
    assert "--local-image-fallback" in argv, argv


def test_the_bundle_build_stages_digest_pins_as_tags(tmp_path):
    """A digest reference survives neither `docker save --platform` nor the
    Skopeo archive fallback, so the pinned images are pulled by digest and
    re-tagged `<repo>:sha256-<digest>` before their script sees the list.
    """
    digest = "sha256:" + "a" * 64
    result, out, tmp = _run_bundle(
        tmp_path,
        "--version",
        "v0.1.0",
        "--arch",
        "arm64",
        images=f"registry-1.docker.io/bitnami/valkey@{digest}\n",
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"

    staged = (out / "netbox-extra-images.staged.txt").read_text().split()
    assert staged == [f"registry-1.docker.io/bitnami/valkey:sha256-{'a' * 64}"], staged
    # The tag is exactly what upload-to-registry.sh would have pushed a digest
    # reference to, so install-netbox.sh still finds it.
    docker = (tmp / "docker.argv").read_text()
    assert "tag" in docker.split("\n"), docker


def test_the_bundle_build_refuses_an_image_built_for_another_architecture(tmp_path):
    import os

    os.environ["NVCM_TEST_ARCH"] = "amd64"
    try:
        result, _, _ = _run_bundle(tmp_path, "--version", "v0.1.0", "--arch", "arm64")
    finally:
        del os.environ["NVCM_TEST_ARCH"]
    assert result.returncode != 0
    assert "is amd64, but this bundle is for arm64" in result.stderr, result.stderr
    assert "make images PLATFORM=linux/arm64" in result.stderr, result.stderr


def test_the_bundle_build_ignores_a_tarball_from_an_earlier_run(tmp_path):
    # An interrupted run leaves one behind, and merging that would ship a bundle
    # built from a different version's images under this version's name.
    out = tmp_path / "out"
    out.mkdir()
    stale = out / "nv-config-manager-airgapped-v0.0.9-old.tar.gz"
    stale.write_bytes(b"stale")

    result, out, _ = _run_bundle(tmp_path, "--version", "v0.1.0", "--arch", "arm64")
    assert result.returncode == 0, result.stderr
    assert stale.read_bytes() == b"stale", "the earlier run's tarball was consumed"
    assert not (out / "nv-config-manager-netbox-airgapped-v0.0.9-old.tar.gz").exists()


def test_the_bundle_build_leaves_a_checksum_for_the_bundle_it_shipped(tmp_path):
    """Their script checksums the tarball this one replaces. Left alone the
    output directory holds a checksum naming a file that is not there, and the
    bundle that is there has none.
    """
    result, out, _ = _run_bundle(tmp_path, "--version", "v0.1.0", "--arch", "arm64")
    assert result.returncode == 0, result.stderr

    leftovers = sorted(p.name for p in out.glob("nv-config-manager-airgapped-*"))
    assert leftovers == [], leftovers
    bundle = out / "nv-config-manager-netbox-airgapped-v0.1.0-test.tar.gz"
    assert bundle.is_file()
    checksum = out / (bundle.name + ".sha256")
    assert checksum.is_file(), sorted(p.name for p in out.iterdir())
    assert bundle.name in checksum.read_text()
    verified = subprocess.run(
        ["shasum", "-a", "256", "-c", checksum.name],
        cwd=out,
        capture_output=True,
        text=True,
        check=False,
    )
    assert verified.returncode == 0, verified.stdout + verified.stderr


def test_verify_waits_for_the_consumer_instead_of_racing_it(monkeypatch):
    # The consumer pulls asynchronously. Reading its log once, immediately after
    # publishing, reported a working loop as broken.
    monkeypatch.setattr(cli, "VERIFY_POLL_INTERVAL", 0)
    monkeypatch.setattr(cluster, "read_nats_password", lambda ns: "pw")
    monkeypatch.setattr(cluster, "publish_test_event", lambda *a, **k: "")

    calls = {"n": 0}

    def slow_consumer(namespace, since="60s", runner=None):
        calls["n"] += 1
        if calls["n"] < 3:
            return ""
        return '{"message": "Starting batch queue operation for 1 devices"}'

    monkeypatch.setattr(cluster, "dcim_consumer_logs", slow_consumer)
    result = CliRunner().invoke(cli.main, ["verify"])
    assert result.exit_code == 0, result.output
    assert "Event dispatched" in result.output
    assert calls["n"] == 3


def test_verify_fails_when_netbox_publishes_to_a_different_subject(monkeypatch):
    """Subject, stream filter and chart value must agree; when they do not the
    broker drops every event and nothing logs it."""
    monkeypatch.setattr(cluster, "read_nats_password", lambda ns: "pw")
    monkeypatch.setattr(
        cluster,
        "netbox_publisher_status",
        lambda ns: cluster.PublisherStatus("nats", True, "nautobot.change"),
    )

    result = CliRunner().invoke(cli.main, ["verify"])

    assert result.exit_code != 0
    assert "nautobot.change" in result.output
    assert EVENT_SUBJECT in result.output


def test_the_publisher_probe_is_valid_python():
    """It runs as `manage.py shell -c <snippet>` in the NetBox pod, where a syntax
    error is indistinguishable from an unreachable pod: the CommandError handler
    reports the backend as unknown and `verify` carries on."""
    import ast

    runner = _RecordingRunner()
    cluster.netbox_publisher_status("ns", runner=runner)

    snippet = runner.argv[-1]
    ast.parse(snippet)
    assert "nats_stream" in snippet
    assert "CONTRACT_VERSION" in snippet


def test_verify_fails_when_the_plugin_stamps_a_different_contract_version(monkeypatch):
    """The consumer exact-matches contract_version, so a mismatch is not a partial
    parse -- every event is rejected while the publish reports success."""
    monkeypatch.setattr(cluster, "read_nats_password", lambda ns: "pw")
    monkeypatch.setattr(
        cluster,
        "netbox_publisher_status",
        lambda ns: cluster.PublisherStatus("nats", True, EVENT_SUBJECT, EVENT_STREAM, "0.9"),
    )

    result = CliRunner().invoke(cli.main, ["verify"])

    assert result.exit_code != 0
    assert "0.9" in result.output


def test_verify_fails_when_netbox_publishes_to_a_different_stream(monkeypatch):
    """A wrong stream name is not silent -- the plugin's publisher checks it at
    connect -- but the failure lands in NetBox's log, not in front of whoever ran
    the deploy."""
    monkeypatch.setattr(cluster, "read_nats_password", lambda ns: "pw")
    monkeypatch.setattr(
        cluster,
        "netbox_publisher_status",
        lambda ns: cluster.PublisherStatus("nats", True, EVENT_SUBJECT, "nautobot"),
    )

    result = CliRunner().invoke(cli.main, ["verify"])

    assert result.exit_code != 0
    assert "nautobot" in result.output
    assert EVENT_STREAM in result.output


def test_verify_accepts_a_matching_subject(monkeypatch):
    monkeypatch.setattr(cli, "VERIFY_POLL_INTERVAL", 0)
    monkeypatch.setattr(cluster, "read_nats_password", lambda ns: "pw")
    monkeypatch.setattr(
        cluster,
        "netbox_publisher_status",
        lambda ns: cluster.PublisherStatus(
            "nats", True, EVENT_SUBJECT, EVENT_STREAM, DCIM_EVENT_CONTRACT_VERSION
        ),
    )
    monkeypatch.setattr(cluster, "publish_test_event", lambda *a, **k: "")
    monkeypatch.setattr(
        cluster,
        "dcim_consumer_logs",
        lambda *a, **k: '{"message": "Starting batch queue operation for 1 devices"}',
    )

    result = CliRunner().invoke(cli.main, ["verify"])
    assert result.exit_code == 0, result.output


def test_verify_explains_a_device_that_does_not_exist(monkeypatch):
    # A 400 from NetBox still proves the whole path; only the last hop failed.
    monkeypatch.setattr(cli, "VERIFY_POLL_INTERVAL", 0)
    monkeypatch.setattr(cluster, "read_nats_password", lambda ns: "pw")
    monkeypatch.setattr(cluster, "publish_test_event", lambda *a, **k: "")
    monkeypatch.setattr(
        cluster,
        "dcim_consumer_logs",
        lambda ns, since="60s", runner=None: (
            '{"message": "Batch operation completed: 0 queued, 1 failed"}\n'
            '{"message": "Render request for 1 was not queued: NetBox rejected the request '
            '(400): device_id Select a valid choice. 1 is not one of the available choices."}'
        ),
    )
    result = CliRunner().invoke(cli.main, ["verify"])
    assert result.exit_code == 0, result.output
    assert "NetBox has no device 1" in result.output


def test_verify_publishes_through_the_in_cluster_nats_box(monkeypatch):
    # `kubectl run natsio/nats-box:latest` needs Docker Hub, which fails on an
    # air-gapped cluster and under Hub's anonymous rate limit.
    argv: list[list[str]] = []
    monkeypatch.setattr(
        cluster,
        "kubectl",
        lambda *a, namespace=None, stdin=None, runner=None: argv.append(list(a)) or "",
    )
    cluster.publish_test_event("ns", "netbox.change", "pw", "1")
    assert argv[0][0] == "exec"
    assert cluster.NATS_BOX_DEPLOYMENT in argv[0][2]
    assert not any("--image" in str(part) for part in argv[0])


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
@pytest.mark.skipif(
    not (_CHART / "charts").is_dir(), reason="chart deps not fetched (helm dependency build)"
)
def test_the_chart_publishes_no_route_of_its_own():
    """NVCM's chart renders the NetBox HTTPRoutes from the provider's dcim.local
    values, so this chart must not render one even when the API is registered."""

    def render(*extra):
        result = subprocess.run(
            ["helm", "template", "nb", str(_CHART), *extra],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    with_api = render("--api-versions", "gateway.networking.k8s.io/v1")
    assert "kind: HTTPRoute" not in with_api
    assert "kind: Deployment" in with_api


def test_nats_password_matches_the_alphabet_nvcm_enforces():
    # It is interpolated unquoted into NVCM's NATS config, so their validator
    # requires [A-Za-z][A-Za-z0-9]* and at least 16 characters.
    for _ in range(20):
        assert re.fullmatch(r"[A-Za-z][A-Za-z0-9]{15,}", generate_nats_password())


def test_kind_refuses_images_the_node_cannot_run(monkeypatch):
    """`make images PLATFORM=...` targets one architecture, which the DSX Air
    path needs. Loading those into a kind node of another architecture fails two
    ways: NetBox dies with "exec format error", and the provider wheelhouse
    installs nothing because pip rejects every wheel, so the pod starts with no
    provider at all.
    """
    monkeypatch.setattr(cluster, "kind_node_arch", lambda c: "arm64")
    monkeypatch.setattr(cluster, "image_arch", lambda i: "amd64")

    with pytest.raises(Exception) as excinfo:
        cluster.check_image_arch(["netbox:0.1.0"], "nv-config-manager")
    message = str(excinfo.value)
    assert "arm64" in message
    assert "make images PLATFORM=linux/arm64" in message


def test_kind_accepts_matching_images(monkeypatch):
    monkeypatch.setattr(cluster, "kind_node_arch", lambda c: "arm64")
    monkeypatch.setattr(cluster, "image_arch", lambda i: "arm64")
    cluster.check_image_arch(["netbox:0.1.0"], "nv-config-manager")


def test_no_kind_node_is_not_an_arch_failure(monkeypatch):
    # Nothing to ask; the load step reports its own failure.
    monkeypatch.setattr(cluster, "kind_node_arch", lambda c: "")
    cluster.check_image_arch(["netbox:0.1.0"], "nv-config-manager")
