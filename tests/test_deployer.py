# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tests for the provider-specific extension of NVCM's deployer."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from nv_config_manager_installer.deployer import DeployOptions, DeployStep, StepStatus
from nv_config_manager_installer.schema import NVConfigManagerInstallConfig

from nv_config_manager_dcim_netbox.installer import cluster
from nv_config_manager_dcim_netbox.installer import config as provider_config
from nv_config_manager_dcim_netbox.installer.deployer import (
    NetBoxDeployer,
    NetBoxDeploymentSettings,
)
from nv_config_manager_dcim_netbox.installer.shell import CommandError
from nv_config_manager_dcim_netbox.installer.tokens import generate_api_token

pytestmark = pytest.mark.installer


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


def _deployer(tmp_path, *, dry_run=False):
    token = generate_api_token()
    raw = provider_config.apply_options(
        provider_config.load_profile(),
        provider_config.ConfigOptions(api_token=token, nats_password="nats-secret"),
    )
    config = NVConfigManagerInstallConfig.model_validate(raw)
    callback = RecordingCallback()
    deployer = NetBoxDeployer(
        config,
        DeployOptions(dry_run=dry_run, helm_timeout="7m"),
        NetBoxDeploymentSettings(
            chart=tmp_path / "chart",
            release="provider-netbox",
            image_repository="registry.example/netbox",
            image_tag="4.7-test",
            extra_set=("feature.enabled=true",),
        ),
        callback,
    )
    deployer._secrets_state = {"dcim_token": token, "nats_password": "nats-secret"}
    return deployer, callback, token


def test_provider_steps_surround_the_umbrella_helm_step():
    ids = [step.id for step in NetBoxDeployer.create_steps()]
    helm = ids.index("helm-install")
    assert ids[helm - 2 : helm] == ["install-netbox", "bootstrap-netbox"]
    assert ids[helm + 1] == "publish-netbox"


def test_pre_helm_hook_installs_netbox_with_common_secrets(monkeypatch, tmp_path):
    deployer, callback, token = _deployer(tmp_path)
    calls = []
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

    deployer._run_provider_pre_helm_install()

    assert calls[0] == ("deps", tmp_path / "chart")
    _, chart, kwargs = calls[1]
    assert chart == str(tmp_path / "chart")
    assert kwargs["api_token"] == token
    assert kwargs["nats_password"] == "nats-secret"
    assert kwargs["extra_set"] == ["feature.enabled=true"]
    assert kwargs["timeout"] == "7m"
    assert [call[0] for call in calls] == ["deps", "install", "wait", "init"]
    assert callback.updates[-1] == ("bootstrap-netbox", StepStatus.SUCCESS)


def test_post_helm_hook_publishes_route(monkeypatch, tmp_path):
    deployer, callback, _ = _deployer(tmp_path)
    calls = []
    monkeypatch.setattr(
        cluster,
        "publish_netbox_route",
        lambda chart, **kwargs: calls.append(("route", chart, kwargs)),
    )
    monkeypatch.setattr(cluster, "netbox_route_exists", lambda namespace: True)

    deployer._run_provider_post_helm_install()

    assert [call[0] for call in calls] == ["route"]
    assert callback.updates[-1] == ("publish-netbox", StepStatus.SUCCESS)


def test_missing_route_is_a_visible_nonfatal_warning(monkeypatch, tmp_path):
    deployer, callback, _ = _deployer(tmp_path)
    monkeypatch.setattr(cluster, "publish_netbox_route", lambda *args, **kwargs: None)
    monkeypatch.setattr(cluster, "netbox_route_exists", lambda namespace: False)

    deployer._run_provider_post_helm_install()

    assert any("Warning: NetBox HTTPRoute was not created" in line for line in callback.logs)
    assert deployer._get_step("publish-netbox").status == StepStatus.SUCCESS


def test_route_upgrade_failure_is_a_visible_nonfatal_warning(monkeypatch, tmp_path):
    deployer, callback, _ = _deployer(tmp_path)
    monkeypatch.setattr(
        cluster,
        "publish_netbox_route",
        lambda *args, **kwargs: (_ for _ in ()).throw(CommandError(["helm"], 1, "boom")),
    )
    monkeypatch.setattr(cluster, "netbox_route_exists", lambda namespace: False)

    deployer._run_provider_post_helm_install()

    assert any("Could not publish the NetBox route" in line for line in callback.logs)
    assert deployer._get_step("publish-netbox").status == StepStatus.SUCCESS


def test_dry_run_skips_all_provider_cluster_calls(monkeypatch, tmp_path):
    deployer, _, _ = _deployer(tmp_path, dry_run=True)

    def unexpected(*args, **kwargs):
        pytest.fail("provider cluster operation ran during a dry run")

    monkeypatch.setattr(cluster, "pending_release_status", unexpected)
    monkeypatch.setattr(cluster, "wait_for_netbox", unexpected)

    deployer._run_provider_pre_helm_install()
    deployer._run_provider_post_helm_install()

    assert deployer._get_step("install-netbox").status == StepStatus.SKIPPED
    assert deployer._get_step("bootstrap-netbox").status == StepStatus.SKIPPED
    assert deployer._get_step("publish-netbox").status == StepStatus.SKIPPED
