# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""The installer provider plugin: discovery, values, steps and deploy hooks.

No cluster. The hooks run against a fake ProviderDeployContext and the
`cluster` module's helm/kubectl calls are monkeypatched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path

import pytest
import yaml
from nv_config_manager_installer.deployer import Deployer, DeployOptions, DeployStep, StepStatus
from nv_config_manager_installer.providers.api import INSTALLER_DCIM_ENTRY_POINT_GROUP
from nv_config_manager_installer.providers.registry import resolve_provider
from nv_config_manager_installer.schema import NVConfigManagerInstallConfig
from nv_config_manager_installer.secrets import generate_secrets

from nv_config_manager_dcim_netbox.installer import cluster
from nv_config_manager_dcim_netbox.installer.config import (
    DEFAULT_CHART,
    DEFAULT_PROFILE,
    EVENT_STREAM,
    EVENT_SUBJECT,
    IMAGE_TAG,
    PROVIDER_IMAGE,
)
from nv_config_manager_dcim_netbox.installer.plugin import (
    LOCAL_SERVER,
    NetBoxInstallerProvider,
    NetBoxInstallerSettings,
)
from nv_config_manager_dcim_netbox.installer.shell import CommandError
from nv_config_manager_dcim_netbox.installer.standalone import StandaloneDeployContext
from nv_config_manager_dcim_netbox.installer.tokens import generate_api_token, is_valid_api_token

pytestmark = pytest.mark.installer

_CHART = Path(__file__).resolve().parents[1] / "deploy" / "netbox"


def _entry_point_installed() -> bool:
    return any(ep.name == "netbox" for ep in entry_points(group=INSTALLER_DCIM_ENTRY_POINT_GROUP))


def _config(**dcim: object) -> NVConfigManagerInstallConfig:
    config = NVConfigManagerInstallConfig.from_yaml(DEFAULT_PROFILE)
    for key, value in dcim.items():
        setattr(config.dcim, key, value)
    return config


@dataclass
class RecordingCallback:
    updates: list[tuple[str, StepStatus]] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)

    def on_step_update(self, step: DeployStep) -> None:
        self.updates.append((step.id, step.status))

    def on_log(self, message: str) -> None:
        self.logs.append(message)

    def on_complete(self, success: bool, endpoints: list[str]) -> None:
        pass


class FakeK8s:
    """Secrets only, which is all the hooks read outside Deployer.run()."""

    def __init__(self, secrets: dict[str, dict[str, str]] | None = None) -> None:
        self.secrets = secrets or {}
        self.namespaces: list[str] = []

    def ensure_namespace(self, name: str) -> bool:
        self.namespaces.append(name)
        return True

    def secret_exists(self, name: str, namespace: str) -> bool:
        return name in self.secrets

    def read_secret_data(self, name: str, namespace: str) -> dict[str, str]:
        return dict(self.secrets.get(name, {}))

    def apply_secret(self, name: str, namespace: str, data: dict[str, str]) -> None:
        self.secrets[name] = dict(data)


def _ctx(
    *,
    dry_run: bool = False,
    local: bool = True,
    secrets: dict[str, dict[str, str]] | None = None,
    recreate_secrets: bool = False,
    settings: dict | None = None,
):
    config = _config(local=local, server="" if local else "https://netbox.example.com")
    if settings is not None:
        config.dcim.settings = settings
    provider = NetBoxInstallerProvider()
    callback = RecordingCallback()
    ctx = StandaloneDeployContext(
        config,
        DeployOptions(dry_run=dry_run, helm_timeout="7m", recreate_secrets=recreate_secrets),
        callback,
        provider,
        k8s=FakeK8s(secrets),
    )
    token = generate_api_token()
    ctx._secrets_state = {"dcim_token": token, "nats_password": "NatsSecretPassword1"}
    return provider, ctx, callback, token


# -- discovery -----------------------------------------------------------------


@pytest.mark.skipif(not _entry_point_installed(), reason="package not installed with [installer]")
def test_the_installer_discovers_the_netbox_provider():
    provider = resolve_provider(_config())
    assert isinstance(provider, NetBoxInstallerProvider)
    assert provider.metadata.name == "netbox"
    assert provider.metadata.token_secret_name == "dcim-token"
    assert provider.metadata.event_stream == EVENT_STREAM
    assert provider.metadata.event_subject == EVENT_SUBJECT


def test_the_profile_is_a_valid_local_netbox_config():
    config = _config()
    assert config.dcim.provider == "netbox"
    assert config.dcim.local is True
    assert config.dcim.provider_packages[0].name == "netbox"
    assert config.dcim.public_url == "https://netbox.config-manager.local"
    assert config.sites[0].name == "TRIAL01 - air_trial"
    settings = NetBoxInstallerProvider().settings(config)
    assert isinstance(settings, NetBoxInstallerSettings)
    assert NetBoxInstallerProvider().validate_config(config) == []


def test_settings_reject_unknown_keys():
    with pytest.raises(ValueError):
        NetBoxInstallerSettings.model_validate({"netbox_image": "x"})


def test_settings_defaults_track_the_package_and_the_chart():
    settings = NetBoxInstallerSettings()
    assert settings.chart == DEFAULT_CHART == _CHART
    assert settings.image_tag == IMAGE_TAG
    assert settings.provider_image == PROVIDER_IMAGE
    assert PROVIDER_IMAGE.endswith(f":{IMAGE_TAG}")
    assert settings.netbox_hostname("config-manager.local") == "netbox.config-manager.local"
    assert settings.netbox_image == f"nv-config-manager-netbox:{IMAGE_TAG}"
    custom = NetBoxInstallerSettings(hostname="nb.example.com")
    assert custom.netbox_hostname("config-manager.local") == "nb.example.com"


def test_event_settings_agree_with_the_chart():
    """The chart renders the plugin's nats_subject and nats_stream from these.

    Left to disagree, NetBox publishes to a stream NVCM did not create and every
    change event is lost with nothing logged on the NVCM side.
    """
    chart_values = yaml.safe_load((_CHART / "values.yaml").read_text())
    settings = NetBoxInstallerSettings()
    assert settings.event_subject == chart_values["nvcm"]["eventSubject"]
    assert settings.event_stream == chart_values["nvcm"]["eventStream"]
    assert chart_values["netbox"]["image"]["tag"] == IMAGE_TAG


# -- configuration ---------------------------------------------------------------


def test_external_netbox_requires_a_server():
    config = _config(local=False, server="")
    assert any("dcim.server" in e for e in NetBoxInstallerProvider().validate_config(config))
    config.dcim.server = "https://netbox.example.com"
    assert NetBoxInstallerProvider().validate_config(config) == []


def test_a_supplied_token_must_be_a_v2_token():
    config = _config()
    config.secrets.k8s.dcim.values["token"] = "not-a-token"
    errors = NetBoxInstallerProvider().validate_config(config)
    assert any("v2 token" in e for e in errors)
    config.secrets.k8s.dcim.values["token"] = generate_api_token()
    assert NetBoxInstallerProvider().validate_config(config) == []


# -- secrets ---------------------------------------------------------------------


def test_the_generated_dcim_token_is_a_netbox_v2_token():
    """NVCM's own fallback is a 40-char token NetBox would silently refuse."""
    state = generate_secrets(_config(), NetBoxInstallerProvider())
    assert is_valid_api_token(state["dcim_token"])


def test_a_configured_token_is_kept():
    config = _config()
    token = generate_api_token()
    config.secrets.k8s.dcim.values["token"] = token
    assert generate_secrets(config, NetBoxInstallerProvider())["dcim_token"] == token


# -- helm values -----------------------------------------------------------------


def test_helm_values_wire_the_provider_stream_and_packages():
    config = _config()
    values = NetBoxInstallerProvider().helm_values(config, {}, is_local=True, local_tags=None)

    assert values["dcim"]["displayName"] == "NetBox"
    assert values["dcim"]["server"] == LOCAL_SERVER == "http://netbox"
    assert values["dcim"]["readiness"] == {"path": "/login/"}
    assert values["dcim"]["events"] == {"stream": EVENT_STREAM, "subject": EVENT_SUBJECT}
    assert values["dcim"]["providerPackages"]["enabled"] is True
    assert values["dcim"]["providerPackages"]["images"][0]["image"] == (
        config.dcim.provider_packages[0].image
    )
    stream = values["externalServices"]["nats"]["streams"]["dcim"]
    assert stream == {
        "name": EVENT_STREAM,
        "subject": EVENT_SUBJECT,
        "subjects": [EVENT_SUBJECT],
        "consumer": {"name": "nv-config-manager-dcim"},
    }
    assert values["nautobotNats"] == {"accounts": {"nautobot": {"enabled": False}}}


def test_helm_values_follow_the_settings_block():
    config = _config()
    config.dcim.settings = {
        "event_stream": "nb",
        "event_subject": "nb.change",
        "consumer_name": "nb-consumer",
        "readiness_path": "/api/status/",
    }
    config.dcim.provider_packages = []
    values = NetBoxInstallerProvider().helm_values(config, {}, is_local=True, local_tags=None)
    assert values["dcim"]["events"] == {"stream": "nb", "subject": "nb.change"}
    assert values["dcim"]["readiness"]["path"] == "/api/status/"
    assert values["externalServices"]["nats"]["streams"]["dcim"]["consumer"]["name"] == (
        "nb-consumer"
    )
    # No provider_packages in the config: the settings' wheelhouse image is used.
    assert values["dcim"]["providerPackages"]["images"] == [
        {"name": "netbox", "image": PROVIDER_IMAGE, "pullPolicy": "IfNotPresent"}
    ]


def test_external_netbox_keeps_the_configured_server():
    config = _config(local=False, server="https://netbox.example.com")
    values = NetBoxInstallerProvider().helm_values(config, {}, is_local=False, local_tags=None)
    assert "server" not in values["dcim"]


# -- steps -----------------------------------------------------------------------


def test_provider_steps_surround_the_umbrella_helm_step():
    ids = [step.id for step in Deployer.create_steps(NetBoxInstallerProvider())]
    helm = ids.index("helm-install")
    assert ids[helm - 2 : helm] == ["install-netbox", "bootstrap-netbox"]
    assert ids[helm + 1] == "publish-netbox"


def test_endpoints_name_the_netbox_route():
    provider = NetBoxInstallerProvider()
    assert provider.endpoints(_config(), "config-manager.local", "https") == [
        "NetBox:           https://netbox.config-manager.local"
    ]
    external = _config(local=False, server="https://netbox.example.com")
    assert provider.endpoints(external, "cm.local", "https") == [
        "NetBox (ext):     https://netbox.example.com"
    ]


# -- deploy hooks ----------------------------------------------------------------


def _stub_install(monkeypatch, calls: list) -> None:
    monkeypatch.setattr(cluster, "pending_release_status", lambda *args: None)
    monkeypatch.setattr(cluster, "orphaned_data_volumes", lambda *args: [])
    monkeypatch.setattr(
        cluster, "helm_dependency_build", lambda chart: calls.append(("deps", chart))
    )
    monkeypatch.setattr(
        cluster, "install_netbox", lambda chart, **kwargs: calls.append(("install", chart, kwargs))
    )
    monkeypatch.setattr(
        cluster,
        "wait_for_netbox",
        lambda namespace, timeout: calls.append(("wait", namespace, timeout)),
    )
    monkeypatch.setattr(
        cluster,
        "run_nvcm_init",
        lambda namespace, api_token: calls.append(("init", namespace, api_token)) or "ready",
    )


def test_pre_helm_hook_installs_netbox_with_the_shared_secrets(monkeypatch):
    provider, ctx, callback, token = _ctx(
        settings={
            "chart": "/charts/netbox",
            "release": "provider-netbox",
            "image_repository": "registry.example/netbox",
            "image_tag": "4.7-test",
            "helm_set": ["feature.enabled=true"],
        }
    )
    calls: list = []
    _stub_install(monkeypatch, calls)

    provider.pre_helm_install(ctx)

    assert calls[0] == ("deps", Path("/charts/netbox"))
    _, chart, kwargs = calls[1]
    assert chart == "/charts/netbox"
    assert kwargs["release"] == "provider-netbox"
    assert kwargs["api_token"] == token
    assert kwargs["nats_password"] == "NatsSecretPassword1"
    assert kwargs["image_repository"] == "registry.example/netbox"
    assert kwargs["image_tag"] == "4.7-test"
    assert kwargs["timeout"] == "7m"
    assert kwargs["workflow_url"] == "https://config-manager.local"
    assert kwargs["extra_set"][-1] == "feature.enabled=true"
    assert [call[0] for call in calls] == ["deps", "install", "wait", "init"]
    assert calls[3] == ("init", "nv-config-manager", token)
    assert callback.updates[-1] == ("bootstrap-netbox", StepStatus.SUCCESS)


def test_pre_helm_hook_binds_the_chart_to_the_installer_settings(monkeypatch):
    """NATS user/service, stream and subject come from the installer, so the
    chart cannot disagree with the stream NVCM provisions."""
    provider, ctx, _, _ = _ctx(settings={"superuser_password": "lab,pass"})
    calls: list = []
    _stub_install(monkeypatch, calls)

    provider.pre_helm_install(ctx)

    overrides = calls[1][2]["extra_set"]
    assert "nvcm.natsService=nv-config-manager-nats" in overrides
    assert "nvcm.natsUser=nv-config-manager" in overrides
    assert f"nvcm.eventStream={EVENT_STREAM}" in overrides
    assert f"nvcm.eventSubject={EVENT_SUBJECT}" in overrides
    assert "nvcm.gateway.baseHostname=config-manager.local" in overrides
    assert "netbox.superuser.name=admin" in overrides
    # helm --set reads `,` as the next assignment.
    assert r"netbox.superuser.password=lab\,pass" in overrides


def test_pre_helm_hook_adopts_the_secrets_already_in_the_cluster(monkeypatch):
    """NVCM skips existing Secrets, so on a re-run NetBox must be installed with
    the cluster's token and NATS password rather than the regenerated state."""
    existing_token = generate_api_token()
    provider, ctx, _, generated = _ctx(
        secrets={
            "dcim-token": {"token": existing_token},
            cluster.NVCM_NATS_SECRET: {"password": "DeployedNatsPassword1"},
        }
    )
    calls: list = []
    _stub_install(monkeypatch, calls)

    provider.pre_helm_install(ctx)

    kwargs = calls[1][2]
    assert kwargs["api_token"] == existing_token != generated
    assert kwargs["nats_password"] == "DeployedNatsPassword1"
    assert calls[3][2] == existing_token


def test_recreate_secrets_uses_the_regenerated_state(monkeypatch):
    provider, ctx, _, generated = _ctx(
        secrets={"dcim-token": {"token": generate_api_token()}}, recreate_secrets=True
    )
    calls: list = []
    _stub_install(monkeypatch, calls)

    provider.pre_helm_install(ctx)

    assert calls[1][2]["api_token"] == generated


def test_pre_helm_hook_refuses_to_run_without_the_shared_secrets(monkeypatch):
    # ESO deployments leave the installer state empty; say so instead of KeyError.
    provider, ctx, _, _ = _ctx()
    ctx._secrets_state = {}
    with pytest.raises(RuntimeError, match="secrets.method"):
        provider.pre_helm_install(ctx)


def test_pending_release_is_reported_with_the_command_that_clears_it(monkeypatch):
    provider, ctx, _, _ = _ctx()
    monkeypatch.setattr(cluster, "pending_release_status", lambda *args: "pending-install")
    with pytest.raises(RuntimeError) as excinfo:
        provider.pre_helm_install(ctx)
    assert "pending-install" in str(excinfo.value)
    assert "helm uninstall netbox -n nv-config-manager" in str(excinfo.value)


def test_orphaned_volumes_are_reported_with_the_command_that_clears_them(monkeypatch):
    provider, ctx, _, _ = _ctx()
    monkeypatch.setattr(cluster, "pending_release_status", lambda *args: None)
    monkeypatch.setattr(
        cluster, "orphaned_data_volumes", lambda *args: ["data-netbox-postgresql-0"]
    )
    with pytest.raises(RuntimeError) as excinfo:
        provider.pre_helm_install(ctx)
    assert "kubectl delete pvc -n nv-config-manager data-netbox-postgresql-0" in str(excinfo.value)


def test_external_netbox_skips_every_provider_step():
    provider, ctx, callback, _ = _ctx(local=False)
    provider.pre_helm_install(ctx)
    provider.post_helm_install(ctx)
    assert dict(callback.updates) == {
        "install-netbox": StepStatus.SKIPPED,
        "bootstrap-netbox": StepStatus.SKIPPED,
        "publish-netbox": StepStatus.SKIPPED,
    }


def test_dry_run_skips_all_provider_cluster_calls(monkeypatch):
    provider, ctx, callback, _ = _ctx(dry_run=True)

    def unexpected(*args, **kwargs):
        pytest.fail("provider cluster operation ran during a dry run")

    monkeypatch.setattr(cluster, "pending_release_status", unexpected)
    monkeypatch.setattr(cluster, "wait_for_netbox", unexpected)
    monkeypatch.setattr(cluster, "publish_netbox_route", unexpected)

    provider.pre_helm_install(ctx)
    provider.post_helm_install(ctx)

    statuses = dict(callback.updates)
    assert statuses["install-netbox"] == StepStatus.SKIPPED
    assert statuses["bootstrap-netbox"] == StepStatus.SKIPPED
    assert statuses["publish-netbox"] == StepStatus.SKIPPED


def test_post_helm_hook_publishes_route(monkeypatch):
    provider, ctx, callback, _ = _ctx()
    calls = []
    monkeypatch.setattr(
        cluster,
        "publish_netbox_route",
        lambda chart, **kwargs: calls.append(("route", chart, kwargs)),
    )
    monkeypatch.setattr(cluster, "netbox_route_exists", lambda namespace: True)

    provider.post_helm_install(ctx)

    assert [call[0] for call in calls] == ["route"]
    assert calls[0][1] == str(DEFAULT_CHART)
    assert calls[0][2]["timeout"] == "7m"
    assert callback.updates[-1] == ("publish-netbox", StepStatus.SUCCESS)


def test_missing_route_is_a_visible_nonfatal_warning(monkeypatch):
    provider, ctx, callback, _ = _ctx()
    monkeypatch.setattr(cluster, "publish_netbox_route", lambda *args, **kwargs: None)
    monkeypatch.setattr(cluster, "netbox_route_exists", lambda namespace: False)

    provider.post_helm_install(ctx)

    assert any("Warning: NetBox HTTPRoute was not created" in line for line in callback.logs)
    assert ctx.steps["publish-netbox"].status == StepStatus.SUCCESS


def test_route_upgrade_failure_is_a_visible_nonfatal_warning(monkeypatch):
    provider, ctx, callback, _ = _ctx()
    monkeypatch.setattr(
        cluster,
        "publish_netbox_route",
        lambda *args, **kwargs: (_ for _ in ()).throw(CommandError(["helm"], 1, "boom")),
    )
    monkeypatch.setattr(cluster, "netbox_route_exists", lambda namespace: False)

    provider.post_helm_install(ctx)

    assert any("Could not publish the NetBox route" in line for line in callback.logs)
    assert ctx.steps["publish-netbox"].status == StepStatus.SUCCESS


# -- standalone context ------------------------------------------------------------


def test_standalone_seeding_creates_the_secrets_nvcm_will_keep():
    """`netbox up` runs before NVCM's deploy, which skips Secrets that exist. The
    ones created here therefore decide the token both sides share."""
    provider, ctx, callback, _ = _ctx()
    ctx.seed_secrets()

    k8s = ctx.k8s
    assert k8s.namespaces == ["nv-config-manager"]
    assert is_valid_api_token(k8s.secrets["dcim-token"]["token"])
    assert ctx.secrets_state["dcim_token"] == k8s.secrets["dcim-token"]["token"]
    nats = ctx.secrets_state["nats_password"]
    assert k8s.secrets["nats-sys"] == {"password": nats}
    assert k8s.secrets["nats-nv-config-manager"] == {"password": nats}
    assert len(callback.logs) == 3


def test_standalone_seeding_adopts_existing_secrets():
    token = generate_api_token()
    provider, ctx, callback, _ = _ctx(
        secrets={
            "dcim-token": {"token": token},
            "nats-nv-config-manager": {"password": "DeployedNatsPassword1"},
        }
    )
    ctx.seed_secrets()

    assert ctx.secrets_state["dcim_token"] == token
    assert ctx.secrets_state["nats_password"] == "DeployedNatsPassword1"
    # Only the missing one is created, with the adopted password.
    assert ctx.k8s.secrets["nats-sys"] == {"password": "DeployedNatsPassword1"}
    assert callback.logs == ["Created: nats-sys"]
