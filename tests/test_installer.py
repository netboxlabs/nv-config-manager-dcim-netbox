# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Deployment orchestration: token generation, config rendering, CLI wiring.

No cluster required. The parts that need one (helm install, kubectl exec) are
covered by the end-to-end steps in docs/deployment.md instead.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import click
import pytest
import yaml
from click.testing import CliRunner

from nv_config_manager_dcim_netbox.installer import cli, cluster
from nv_config_manager_dcim_netbox.installer import config as cfg
from nv_config_manager_dcim_netbox.installer.cli import _nvcm_installer_argv, main
from nv_config_manager_dcim_netbox.installer.shell import require_tools
from nv_config_manager_dcim_netbox.installer.tokens import (
    TOKEN_KEY_LENGTH,
    TOKEN_SECRET_LENGTH,
    generate_api_token,
    generate_nats_password,
    is_valid_api_token,
    is_valid_nats_password,
)

pytestmark = pytest.mark.installer

_REPO = Path(__file__).resolve().parents[1]
_CHART = _REPO / "deploy" / "netbox"


@pytest.fixture(autouse=True)
def _no_deployed_nats(monkeypatch):
    """Answer "no NATS deployed" unless a test says otherwise.

    `config`, `init` and `up` all prefer the password of a running NVCM over
    minting one. Left alone, that reads through kubectl to whatever cluster the
    developer's kubeconfig points at, which makes these tests depend on it.
    """
    monkeypatch.setattr(cluster, "read_nats_password", lambda *a, **k: None)


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


# -- config rendering ---------------------------------------------------------


def test_profile_is_a_valid_external_netbox_config():
    profile = cfg.load_profile()
    assert profile["dcim"]["provider"] == "netbox"
    # The whole point of the profile: NetBox as the provider, plus a
    # provider_packages block (which the NVCM TUI has no widget for, so it cannot
    # be authored there).
    assert profile["services"]["nautobot"] is False
    assert profile["dcim"]["provider_packages"][0]["name"] == "netbox"
    assert profile["secrets"]["k8s"]["dcim"]["values"]["token"] == cfg.TOKEN_PLACEHOLDER
    assert profile["content"]["template_plugins"] == [
        {"path": "development/air_sim/template_plugins/superpod-template-plugin"}
    ]


def test_apply_options_sets_every_computed_field():
    token = generate_api_token()
    result = cfg.apply_options(
        cfg.load_profile(),
        cfg.ConfigOptions(
            api_token=token,
            namespace="custom-ns",
            size="large",
            provider_image="reg.example/provider:9.9",
            netbox_public_url="https://netbox.example",
        ),
    )
    assert result["secrets"]["k8s"]["dcim"]["values"]["token"] == token
    assert result["cluster"]["namespace"] == "custom-ns"
    assert result["cluster"]["size"] == "large"
    assert result["dcim"]["provider_packages"][0]["image"] == "reg.example/provider:9.9"
    assert result["dcim"]["public_url"] == "https://netbox.example"


def test_apply_options_does_not_mutate_the_shipped_profile():
    profile = cfg.load_profile()
    cfg.apply_options(profile, cfg.ConfigOptions(api_token=generate_api_token()))
    assert profile["secrets"]["k8s"]["dcim"]["values"]["token"] == cfg.TOKEN_PLACEHOLDER


def test_event_subject_must_match_the_plugins_publisher():
    # deploy/netbox/values.yaml sets the plugin's nats_subject from nvcm.eventSubject;
    # if these two drift, NetBox publishes and nothing consumes.
    chart_values = yaml.safe_load((_CHART / "values.yaml").read_text())
    assert cfg.event_subject(cfg.load_profile()) == chart_values["nvcm"]["eventSubject"]


def test_written_config_is_owner_only(tmp_path):
    # It carries a live API token.
    out = cfg.write_config(
        cfg.apply_options(cfg.load_profile(), cfg.ConfigOptions(api_token=generate_api_token())),
        tmp_path / "nested" / "install.yaml",
    )
    assert out.exists()
    assert oct(out.stat().st_mode)[-3:] == "600"


# -- CLI ----------------------------------------------------------------------


def test_config_command_writes_a_loadable_config(tmp_path):
    out = tmp_path / "install.yaml"
    result = CliRunner().invoke(main, ["config", "-o", str(out), "--size", "medium"])
    assert result.exit_code == 0, result.output
    written = yaml.safe_load(out.read_text())
    assert written["cluster"]["size"] == "medium"
    assert is_valid_api_token(written["secrets"]["k8s"]["dcim"]["values"]["token"])


def test_config_command_honours_a_supplied_token(tmp_path):
    token = generate_api_token()
    out = tmp_path / "install.yaml"
    result = CliRunner().invoke(main, ["config", "-o", str(out), "--token", token])
    assert result.exit_code == 0, result.output
    assert yaml.safe_load(out.read_text())["secrets"]["k8s"]["dcim"]["values"]["token"] == token


def test_config_command_rejects_a_malformed_token(tmp_path):
    result = CliRunner().invoke(main, ["config", "-o", str(tmp_path / "x.yaml"), "--token", "nope"])
    assert result.exit_code != 0
    assert "v2 token" in result.output


def test_up_reuses_the_generated_token_when_resuming(monkeypatch, tmp_path):
    token = generate_api_token()
    out = tmp_path / "install.yaml"
    out.write_text(yaml.safe_dump({"secrets": {"k8s": {"dcim": {"values": {"token": token}}}}}))
    monkeypatch.setattr(cluster, "kind_cluster_exists", lambda name: True)
    monkeypatch.setattr(cluster, "helm_dependency_build", lambda chart: None)
    monkeypatch.setattr(cluster, "pending_release_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(cluster, "orphaned_data_volumes", lambda *args, **kwargs: [])
    seen = {}
    monkeypatch.setattr(
        cluster,
        "install_netbox",
        lambda *args, **kwargs: seen.update(token=kwargs["api_token"]),
    )
    monkeypatch.setattr(cluster, "wait_for_netbox", lambda *args, **kwargs: None)
    monkeypatch.setattr(cluster, "run_nvcm_init", lambda *args, **kwargs: "")

    result = CliRunner().invoke(
        cli.main, ["netbox", "up", "--no-kind-load", "--config-out", str(out)]
    )

    assert result.exit_code == 0, result.output
    assert seen["token"] == token
    assert yaml.safe_load(out.read_text())["secrets"]["k8s"]["dcim"]["values"]["token"] == token


def test_up_rejects_a_path_that_is_not_an_nvcm_checkout(tmp_path):
    result = CliRunner().invoke(main, ["up", "--nvcm-repo", str(tmp_path)])
    assert result.exit_code != 0
    # NVCM's chart and images are unpublished, so a checkout is mandatory; the
    # error has to say so rather than failing later at the helm step.
    assert "nv-config-manager checkout" in result.output


def test_nvcm_installer_argv_targets_their_cli_not_our_imports():
    argv = _nvcm_installer_argv(Path("/tmp/nvcm"), "validate", "/abs/cfg.yaml")
    assert argv[:2] == ["uv", "run"]
    assert argv[2:4] == ["--directory", "/tmp/nvcm"]
    assert argv[4] == "--project"
    assert argv[5].endswith("/tmp/nvcm/installer")
    assert argv[6:] == ["nv-config-manager-installer", "validate", "/abs/cfg.yaml"]


def test_nvcm_installer_runs_from_their_checkout_not_ours():
    # Their find_project_root() walks up from the working directory for
    # `deploy/` / `Makefile` / `.git`, all of which this repo also has. Without
    # --directory it picks us, and their image builds fail on contexts like
    # `components/nats-ready` that exist only in their tree.
    argv = _nvcm_installer_argv(Path("/tmp/nvcm"), "deploy", "/abs/cfg.yaml")
    assert argv[argv.index("--directory") + 1] == "/tmp/nvcm"


# -- parity with NVCM's own command surface -----------------------------------


@pytest.fixture
def their_cli(monkeypatch):
    """Capture the argv sent to NVCM's installer instead of running it."""
    seen: list[list[str]] = []
    monkeypatch.setattr(cli, "stream", lambda argv, cwd=None: seen.append(list(argv)))
    return seen


@pytest.mark.parametrize(
    ("command", "subcommand", "extra"),
    [
        (["validate"], "validate", []),
        (["generate-values"], "generate-values", ["-o", "/tmp/out", "--local-images"]),
        (
            ["deploy"],
            "deploy",
            ["--image-source", "local", "--build-images", "--kind-cluster", "x"],
        ),
    ],
)
def test_their_commands_are_forwarded_verbatim(their_cli, tmp_path, command, subcommand, extra):
    # Their flags are not re-declared here, so anything they add keeps working
    # without a change on our side. Order has to survive too.
    config = tmp_path / "install.yaml"
    config.write_text("dcim:\n  server: https://netbox.example\n")
    repo = _nvcm_stub()

    result = CliRunner().invoke(cli.main, [*command, str(config), "--nvcm-repo", str(repo), *extra])
    assert result.exit_code == 0, result.output
    argv = their_cli[0]
    assert argv[argv.index("nv-config-manager-installer") + 1] == subcommand
    if extra:
        assert argv[-len(extra) :] == extra
    # Their find_project_root() reads the working directory, so it has to be theirs.
    assert argv[argv.index("--directory") + 1] == str(repo.resolve())


@pytest.mark.parametrize("command", ["validate", "generate-values", "deploy"])
def test_their_commands_resolve_the_config_path(their_cli, tmp_path, monkeypatch, command):
    config = tmp_path / "install.yaml"
    config.write_text("dcim:\n  server: https://netbox.example\n")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        cli.main, [command, "install.yaml", "--nvcm-repo", str(_nvcm_stub())]
    )
    assert result.exit_code == 0, result.output
    argv = their_cli[0]
    assert Path(argv[argv.index(command) + 1]).is_absolute()


def test_deploy_warns_when_the_in_cluster_netbox_is_missing(their_cli, tmp_path, monkeypatch):
    # A warning, not an error: NVCM will come up and fail to reach its DCIM, but
    # pointing at a NetBox outside the cluster is legitimate and unverifiable here.
    monkeypatch.setattr(cluster, "netbox_service_exists", lambda ns, runner=None: False)
    config = tmp_path / "install.yaml"
    config.write_text("dcim:\n  server: http://netbox\ncluster:\n  namespace: nv-config-manager\n")

    result = CliRunner().invoke(cli.main, ["deploy", str(config), "--nvcm-repo", str(_nvcm_stub())])
    assert result.exit_code == 0, result.output
    assert "No netbox service" in result.output
    assert their_cli, "the warning must not stop the deploy"


def test_deploy_is_quiet_about_a_netbox_it_cannot_check(their_cli, tmp_path, monkeypatch):
    monkeypatch.setattr(cluster, "netbox_service_exists", lambda ns, runner=None: False)
    config = tmp_path / "install.yaml"
    config.write_text("dcim:\n  server: https://netbox.example.com\n")

    result = CliRunner().invoke(cli.main, ["deploy", str(config), "--nvcm-repo", str(_nvcm_stub())])
    assert result.exit_code == 0, result.output
    assert "No netbox service" not in result.output


def test_init_seeds_the_fields_their_wizard_cannot_express(monkeypatch, tmp_path):
    # Their TUI has no widget for provider_packages or the token, so both are
    # written before it opens.
    seen: list[list[str]] = []
    monkeypatch.setattr(cli, "stream", lambda argv, cwd=None: seen.append(list(argv)))
    out = tmp_path / "install.yaml"

    result = CliRunner().invoke(
        cli.main, ["init", "-c", str(out), "--nvcm-repo", str(_nvcm_stub())]
    )
    assert result.exit_code == 0, result.output
    written = yaml.safe_load(out.read_text())
    assert written["dcim"]["provider"] == "netbox"
    assert written["dcim"]["provider_packages"][0]["image"]
    assert written["services"]["nautobot"] is False
    assert is_valid_api_token(written["secrets"]["k8s"]["dcim"]["values"]["token"])
    assert seen[0][seen[0].index("init") + 1 :] == ["--config", str(out.resolve())]


def test_init_restores_what_their_wizard_drops(monkeypatch, tmp_path):
    out = tmp_path / "install.yaml"

    def wizard_drops_our_fields(argv, cwd=None):
        config = yaml.safe_load(out.read_text())
        del config["dcim"]["provider_packages"]
        config["services"]["nautobot"] = True
        config["dcim"]["provider"] = "nautobot"
        out.write_text(yaml.safe_dump(config))

    monkeypatch.setattr(cli, "stream", wizard_drops_our_fields)
    result = CliRunner().invoke(
        cli.main, ["init", "-c", str(out), "--nvcm-repo", str(_nvcm_stub())]
    )
    assert result.exit_code == 0, result.output
    written = yaml.safe_load(out.read_text())
    assert written["dcim"]["provider"] == "netbox"
    assert written["dcim"]["provider_packages"][0]["image"]
    assert written["services"]["nautobot"] is False


def test_init_keeps_the_token_already_in_the_file(monkeypatch, tmp_path):
    # Regenerating it would break the NetBox side, which already holds this one.
    token = generate_api_token()
    out = tmp_path / "install.yaml"
    out.write_text(yaml.safe_dump({"secrets": {"k8s": {"dcim": {"values": {"token": token}}}}}))
    monkeypatch.setattr(cli, "stream", lambda argv, cwd=None: None)

    result = CliRunner().invoke(
        cli.main, ["init", "-c", str(out), "--nvcm-repo", str(_nvcm_stub())]
    )
    assert result.exit_code == 0, result.output
    assert yaml.safe_load(out.read_text())["secrets"]["k8s"]["dcim"]["values"]["token"] == token


def test_netbox_up_stops_before_nvcm(monkeypatch, tmp_path):
    # It is `up` without the NVCM half, so NVCM's installer must never be called
    # and the run must not need a checkout to be present.
    seen: list[list[str]] = []
    monkeypatch.setattr(cluster, "kind_cluster_exists", lambda name: True)
    monkeypatch.setattr(cluster, "helm_dependency_build", lambda chart: None)
    monkeypatch.setattr(cluster, "pending_release_status", lambda r, ns, runner=None: None)
    monkeypatch.setattr(cluster, "orphaned_data_volumes", lambda r, ns, runner=None: [])
    monkeypatch.setattr(cluster, "install_netbox", lambda *a, **k: None)
    monkeypatch.setattr(cluster, "wait_for_netbox", lambda *a, **k: None)
    monkeypatch.setattr(cluster, "run_nvcm_init", lambda *a, **k: "")
    monkeypatch.setattr(cli, "stream", lambda argv, cwd=None: seen.append(list(argv)))

    out = tmp_path / "install.yaml"
    result = CliRunner().invoke(
        cli.main, ["netbox", "up", "--no-kind-load", "--config-out", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert not seen, "NVCM's installer was invoked by `netbox up`"
    assert "[7/7]" in result.output
    # It still leaves behind the token and config the NVCM half needs.
    assert is_valid_api_token(
        yaml.safe_load(out.read_text())["secrets"]["k8s"]["dcim"]["values"]["token"]
    )


def _up_with_cluster_mocked(monkeypatch, *, kind_exists=True):
    """Stub the cluster calls made before the combined deployer takes over."""
    monkeypatch.setattr(cluster, "kind_cluster_exists", lambda name: kind_exists)
    monkeypatch.setattr(cluster, "helm_dependency_build", lambda chart: None)
    monkeypatch.setattr(cluster, "pending_release_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(cluster, "orphaned_data_volumes", lambda *args, **kwargs: [])
    monkeypatch.setattr(cluster, "install_netbox", lambda *args, **kwargs: None)
    monkeypatch.setattr(cluster, "wait_for_netbox", lambda *args, **kwargs: None)
    monkeypatch.setattr(cluster, "run_nvcm_init", lambda *args, **kwargs: "")
    monkeypatch.setattr(cli.NetBoxDeployer, "run", lambda self: True)


def test_up_uses_the_inherited_deployer_with_provider_settings(monkeypatch, tmp_path):
    captured = {}
    _up_with_cluster_mocked(monkeypatch)
    repo = _nvcm_stub()

    class RecordingDeployer:
        def __init__(self, config, options, netbox, callback):
            captured.update(config=config, options=options, netbox=netbox, callback=callback)

        def run(self):
            return True

    monkeypatch.setattr(cli, "NetBoxDeployer", RecordingDeployer)

    result = CliRunner().invoke(
        cli.main,
        [
            "up",
            "--no-kind-load",
            "--skip-verify",
            "--config-out",
            str(tmp_path / "install.yaml"),
            "--nvcm-repo",
            str(repo),
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["config"].dcim.provider == "netbox"
    assert captured["config"].images.source.value == "local"
    assert captured["options"].project_root == repo
    assert captured["options"].load_kind is False
    assert captured["netbox"].chart == _CHART.resolve()
    assert captured["netbox"].image_repository == "nv-config-manager-netbox"
    assert "[4/4]" in result.output


def test_up_reports_an_inherited_deployer_failure(monkeypatch, tmp_path):
    _up_with_cluster_mocked(monkeypatch)
    monkeypatch.setattr(cli.NetBoxDeployer, "run", lambda self: False)

    result = CliRunner().invoke(
        cli.main,
        [
            "up",
            "--no-kind-load",
            "--skip-verify",
            "--config-out",
            str(tmp_path / "install.yaml"),
            "--nvcm-repo",
            str(_nvcm_stub()),
        ],
    )
    assert result.exit_code != 0
    assert "deployment failed" in result.output.lower()


def test_up_deploys_to_a_cluster_kind_did_not_create(monkeypatch, tmp_path):
    """`--no-create-cluster` means "use the cluster I already have", including a
    real one.

    `kind_cluster_exists` is false for any cluster kind did not create -- and for
    every cluster when kind is not installed, since the CommandError is swallowed.
    Treating that as fatal made the flag refuse to deploy anywhere but kind, which
    is exactly what `--no-kind-load` exists to support.
    """
    _up_with_cluster_mocked(monkeypatch, kind_exists=False)

    result = CliRunner().invoke(
        cli.main,
        [
            "netbox",
            "up",
            "--no-create-cluster",
            "--no-kind-load",
            "--config-out",
            str(tmp_path / "install.yaml"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "current kubectl context" in result.output


def test_netbox_up_and_up_share_their_options():
    # `netbox up` is a thin wrapper; a flag added to one must appear on the other.
    shared = {p.name for p in cli.up.params} - {"skip_nvcm", "skip_verify"}
    assert shared == {p.name for p in cli.netbox_up.params}


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


def test_config_options_shared_by_config_and_up_honour_the_profile_public_url():
    # `up` had its own copy and omitted the public_url fallback.
    base = {
        "dcim": {"public_url": "https://netbox.corp", "provider_packages": [{"image": "img:1"}]}
    }
    options = cli._config_options(base, api_token="nbt_x.y", namespace="ns", size="small")
    assert options.netbox_public_url == "https://netbox.corp"
    assert options.provider_image == "img:1"
    # An explicit flag still wins over the profile.
    explicit = cli._config_options(
        base, api_token="nbt_x.y", namespace="ns", size="small", public_url="http://other"
    )
    assert explicit.netbox_public_url == "http://other"


def test_provider_image_tag_tracks_the_package_version():
    # Hardcoded, a version bump left the installer asking for an unbuilt tag.
    assert cfg.PROVIDER_IMAGE.endswith(f":{cfg.IMAGE_TAG}")
    assert cfg.ConfigOptions(api_token="nbt_x.y").provider_image == cfg.PROVIDER_IMAGE


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


def _fake_checkout(root):
    """A directory shaped enough like an NVCM checkout to satisfy validation."""
    (root / "installer").mkdir(parents=True)
    (root / "installer" / "pyproject.toml").write_text("")
    (root / "deploy" / "helm").mkdir(parents=True)
    (root / "deploy" / "helm" / "Chart.yaml").write_text("")
    return root


def test_nvcm_repo_resolves_from_the_environment(tmp_path, monkeypatch):
    from nv_config_manager_dcim_netbox.installer.cli import _resolve_nvcm_repo

    repo = _fake_checkout(tmp_path / "nvcm")
    monkeypatch.setenv("NVCM_REPO", str(repo))
    assert _resolve_nvcm_repo(None) == repo.resolve()


def test_missing_checkout_names_the_path_and_its_source(tmp_path, monkeypatch):
    # The whole point: a relative default resolved against the wrong working
    # directory is invisible unless the error prints the absolute path it tried.
    from nv_config_manager_dcim_netbox.installer.cli import _resolve_nvcm_repo

    monkeypatch.setenv("NVCM_REPO", str(tmp_path / "nope"))
    with pytest.raises(click.ClickException) as excinfo:
        _resolve_nvcm_repo(None)
    message = str(excinfo.value)
    assert str(tmp_path / "nope") in message
    assert "$NVCM_REPO" in message


def test_wrong_directory_is_reported_as_not_a_checkout(tmp_path, monkeypatch):
    from nv_config_manager_dcim_netbox.installer.cli import _resolve_nvcm_repo

    monkeypatch.delenv("NVCM_REPO", raising=False)
    with pytest.raises(click.ClickException) as excinfo:
        _resolve_nvcm_repo(tmp_path)
    message = str(excinfo.value)
    assert "not an nv-config-manager checkout" in message
    assert "--nvcm-repo" in message


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


def test_pending_release_is_reported_with_the_command_that_clears_it(monkeypatch):
    # An interrupted `helm upgrade --install` leaves the release pending, and
    # every later run then fails with helm's opaque "another operation in
    # progress". Name the fix instead.
    monkeypatch.setattr(cluster, "kind_cluster_exists", lambda name: True)
    monkeypatch.setattr(cluster, "helm_dependency_build", lambda chart: None)
    monkeypatch.setattr(
        cluster, "pending_release_status", lambda r, ns, runner=None: "pending-install"
    )

    result = CliRunner().invoke(
        cli.main,
        ["up", "--nvcm-repo", str(_nvcm_stub()), "--no-kind-load", "--skip-nvcm"],
    )
    assert result.exit_code != 0
    assert "pending-install" in result.output
    assert "helm uninstall netbox -n nv-config-manager" in result.output


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


def test_orphaned_volumes_are_reported_with_the_command_that_clears_them(monkeypatch):
    monkeypatch.setattr(cluster, "kind_cluster_exists", lambda name: True)
    monkeypatch.setattr(cluster, "helm_dependency_build", lambda chart: None)
    monkeypatch.setattr(cluster, "pending_release_status", lambda r, ns, runner=None: None)
    monkeypatch.setattr(
        cluster,
        "orphaned_data_volumes",
        lambda r, ns, runner=None: ["data-netbox-postgresql-0"],
    )

    result = CliRunner().invoke(
        cli.main,
        ["up", "--nvcm-repo", str(_nvcm_stub()), "--no-kind-load", "--skip-nvcm"],
    )
    assert result.exit_code != 0
    assert "kubectl delete pvc -n nv-config-manager data-netbox-postgresql-0" in result.output


def _nvcm_stub():
    """A directory shaped like an NVCM checkout, for tests that must get past
    repo validation."""
    root = Path(tempfile.mkdtemp())
    (root / "installer").mkdir()
    (root / "installer" / "pyproject.toml").write_text("")
    (root / "deploy" / "helm").mkdir(parents=True)
    (root / "deploy" / "helm" / "Chart.yaml").write_text("")
    return root


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
    docker.write_text('#!/bin/sh\necho \'{"Descriptor":{"digest":"sha256:stub"}}\'\n')
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
def test_the_route_waits_for_the_gateway_api_to_exist():
    """NetBox installs before NVCM, which is what installs the Gateway API CRDs.

    Rendering an HTTPRoute against a cluster that has no such kind fails the
    whole release with "ensure CRDs are installed first", so a first install on a
    fresh cluster must simply omit it.
    """

    def render(*extra):
        result = subprocess.run(
            ["helm", "template", "nb", str(_CHART), *extra],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    fresh = render()
    assert "kind: HTTPRoute" not in fresh
    # The rest of the release must still be there, not just the route dropped.
    assert "kind: Deployment" in fresh

    with_api = render("--api-versions", "gateway.networking.k8s.io/v1")
    assert "kind: HTTPRoute" in with_api
    assert "netbox.config-manager.local" in with_api


def test_apply_dcim_sets_the_event_stream_and_subject():
    """NVCM builds the JetStream stream, its subject filter and the consumer's
    filter from these, and the chart renders the plugin's nats_subject from the
    same value.

    Left unset, NVCM uses its own defaults and creates a stream filtering on
    `nautobot` while NetBox publishes to `netbox.change`. Nothing matches, so
    every change event is dropped by the broker with no error anywhere: the
    publisher succeeds, the stream stays empty, and the consumer sits idle.
    """
    # The Air path patches the config NVCM's generator wrote, which has no events.
    patched = cfg.apply_dcim({"dcim": {}}, cfg.ConfigOptions(api_token=generate_api_token()))
    assert patched["dcim"]["event_stream"] == cfg.EVENT_STREAM
    assert patched["dcim"]["event_subject"] == cfg.EVENT_SUBJECT

    # And it must equal what the chart tells the plugin to publish on.
    chart_values = yaml.safe_load((_CHART / "values.yaml").read_text())
    assert patched["dcim"]["event_subject"] == chart_values["nvcm"]["eventSubject"]


def test_the_shipped_profile_agrees_with_apply_dcim():
    profile = cfg.load_profile()
    assert profile["dcim"]["event_stream"] == cfg.EVENT_STREAM
    assert profile["dcim"]["event_subject"] == cfg.EVENT_SUBJECT


def test_nats_password_is_seeded_where_nvcm_reads_it():
    """NVCM generates a NATS password unless the config supplies one.

    Supplying it is what lets NetBox be installed once, with the password
    already set, instead of upgraded to switch its publisher on. NATS has its
    own secret group independently of the selected DCIM provider.
    """
    password = generate_nats_password()
    assert is_valid_nats_password(password)

    patched = cfg.apply_dcim(
        {}, cfg.ConfigOptions(api_token=generate_api_token(), nats_password=password)
    )
    group = patched["secrets"]["k8s"]["nats"]
    assert group["enabled"] is True
    assert group["values"]["password"] == password


def test_nats_password_matches_the_alphabet_nvcm_enforces():
    # It is interpolated unquoted into NVCM's NATS config, so their validator
    # requires [A-Za-z][A-Za-z0-9]* and at least 16 characters.
    for _ in range(20):
        assert re.fullmatch(r"[A-Za-z][A-Za-z0-9]{15,}", generate_nats_password())


def _written_nats_password(path: Path) -> str:
    return yaml.safe_load(path.read_text())["secrets"]["k8s"]["nats"]["values"]["password"]


def test_config_command_keeps_a_nats_password_already_present(tmp_path):
    """Regenerating it would leave NVCM's NATS users and a deployed NetBox
    disagreeing until both are rolled.

    Re-run exactly as a user would, with no --profile: the password lives in the
    file this command *writes*, and reading the profile for it (which never has
    one) minted a fresh password every invocation. The mismatch surfaces only
    later, as `Authorization Violation` in the NetBox log with renders silently
    not happening.
    """
    out = tmp_path / "install.yaml"
    result = CliRunner().invoke(main, ["config", "-o", str(out)])
    assert result.exit_code == 0, result.output
    first = _written_nats_password(out)

    again = CliRunner().invoke(main, ["config", "-o", str(out)])
    assert again.exit_code == 0, again.output
    assert _written_nats_password(out) == first

    # And the same when the profile is passed explicitly, which is what a caller
    # driving NVCM's installer by hand does.
    third = CliRunner().invoke(main, ["config", "-o", str(out), "--profile", str(out)])
    assert third.exit_code == 0, third.output
    assert _written_nats_password(out) == first


def test_a_deployed_nats_password_beats_the_one_in_the_config(tmp_path, monkeypatch):
    """NVCM's deployer skips any secret that already exists (`deployer.py`,
    `_apply_secret`: `if exists and not self.options.recreate_secrets`).

    So on a redeploy the natsPassword we put in the install config is read,
    validated, and then not applied -- nats-nv-config-manager keeps whatever it
    was first created with. Writing anything else into the NetBox release leaves
    the publisher authenticating with a password NATS rejects, and the only
    trace is `Authorization Violation` in the NetBox log while renders silently
    stop happening.
    """
    monkeypatch.setattr(cluster, "read_nats_password", lambda *a, **k: "DeployedPassword123456")

    out = tmp_path / "install.yaml"
    result = CliRunner().invoke(main, ["config", "-o", str(out)])
    assert result.exit_code == 0, result.output

    values = yaml.safe_load(out.read_text())["secrets"]["k8s"]["nats"]["values"]
    assert values["password"] == "DeployedPassword123456"


def test_a_deployed_nats_password_nvcm_would_reject_fails_loudly(tmp_path, monkeypatch):
    # Writing it back would abort the deploy inside their validator, far from
    # the cause. Say which secret is wrong and how to clear it instead.
    monkeypatch.setattr(cluster, "read_nats_password", lambda *a, **k: "9-bad-password-value")

    result = CliRunner().invoke(main, ["config", "-o", str(tmp_path / "install.yaml")])
    assert result.exit_code != 0
    assert cluster.NVCM_NATS_SECRET in result.output
    assert "--recreate-secrets" in result.output


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
