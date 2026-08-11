# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Deployment orchestration: token generation, config rendering, CLI wiring.

No cluster required. The parts that need one (helm install, kubectl exec) are
covered by the end-to-end steps in docs/deployment.md instead.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

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
    is_valid_api_token,
)

pytestmark = pytest.mark.installer

_REPO = Path(__file__).resolve().parents[1]
_CHART = _REPO / "deploy" / "netbox"


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


def test_up_rejects_a_path_that_is_not_an_nvcm_checkout(tmp_path):
    result = CliRunner().invoke(main, ["up", "--nvcm-repo", str(tmp_path)])
    assert result.exit_code != 0
    # NVCM's chart and images are unpublished, so a checkout is mandatory; the
    # error has to say so rather than failing later at the helm step.
    assert "nv-config-manager checkout" in result.output


def test_nvcm_installer_argv_targets_their_cli_not_our_imports():
    argv = _nvcm_installer_argv(Path("/tmp/nvcm"), "validate", "cfg.yaml")
    assert argv[:3] == ["uv", "run", "--project"]
    assert argv[3].endswith("/tmp/nvcm/installer")
    assert argv[4:] == ["nv-config-manager-installer", "validate", "cfg.yaml"]


def test_require_tools_names_everything_missing():
    with pytest.raises(RuntimeError) as excinfo:
        require_tools("definitely-not-a-real-binary-xyz", "also-not-real-xyz")
    message = str(excinfo.value)
    assert "definitely-not-a-real-binary-xyz" in message
    assert "also-not-real-xyz" in message


# -- helm invocation ----------------------------------------------------------


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
