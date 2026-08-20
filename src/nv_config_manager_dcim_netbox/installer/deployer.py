# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""NetBox-specific extension of NVIDIA Config Manager's deployment pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from nv_config_manager_installer.deployer import (
    DeployCallback,
    Deployer,
    DeployOptions,
    DeployStep,
    StepStatus,
)
from nv_config_manager_installer.schema import NVConfigManagerInstallConfig

from nv_config_manager_dcim_netbox.installer import cluster
from nv_config_manager_dcim_netbox.installer.shell import CommandError


@dataclass(frozen=True)
class NetBoxDeploymentSettings:
    """Provider-owned inputs that do not belong in NVCM's common schema."""

    chart: Path
    release: str = "netbox"
    image_repository: str = "nv-config-manager-netbox"
    image_tag: str | None = None
    extra_set: tuple[str, ...] = field(default_factory=tuple)


class NetBoxDeployer(Deployer):
    """Deploy NVCM and NetBox through the common installer lifecycle."""

    def __init__(
        self,
        config: NVConfigManagerInstallConfig,
        options: DeployOptions,
        netbox: NetBoxDeploymentSettings,
        callback: DeployCallback | None = None,
    ) -> None:
        self.netbox = netbox
        super().__init__(config, options, callback)

    @classmethod
    def create_steps(cls) -> list[DeployStep]:
        """Add provider steps around the common umbrella Helm installation."""
        steps = super().create_steps()
        helm_index = next(i for i, step in enumerate(steps) if step.id == "helm-install")
        steps.insert(helm_index, DeployStep("install-netbox", "Install NetBox"))
        steps.insert(helm_index + 2, DeployStep("bootstrap-netbox", "Bootstrap NetBox"))
        steps.insert(helm_index + 3, DeployStep("publish-netbox", "Publish NetBox route"))
        return steps

    def _run_provider_pre_helm_install(self) -> None:
        step = self._start_step("install-netbox")
        if self.options.dry_run:
            self._finish_step(step, StepStatus.SKIPPED)
            return

        stuck = cluster.pending_release_status(self.netbox.release, self.config.cluster.namespace)
        if stuck:
            raise RuntimeError(
                f"The {self.netbox.release!r} release is stuck in {stuck}. Clear it with "
                f"`helm uninstall {self.netbox.release} -n {self.config.cluster.namespace}` "
                "or roll it back to a completed revision."
            )

        stale = cluster.orphaned_data_volumes(self.netbox.release, self.config.cluster.namespace)
        if stale:
            names = " ".join(stale)
            raise RuntimeError(
                "NetBox datastore volumes remain but their credentials are gone: "
                f"{names}. Delete them before retrying with "
                f"`kubectl delete pvc -n {self.config.cluster.namespace} {names}`."
            )

        if not self.config.cluster.airgapped:
            cluster.helm_dependency_build(self.netbox.chart)
        cluster.install_netbox(
            str(self.netbox.chart),
            release=self.netbox.release,
            namespace=self.config.cluster.namespace,
            api_token=self._secrets_state["dcim_token"],
            image_repository=self.netbox.image_repository,
            image_tag=self.netbox.image_tag,
            nats_password=self._secrets_state["nats_password"],
            extra_set=list(self.netbox.extra_set),
            timeout=self.options.helm_timeout,
        )
        self._finish_step(step)

    def _run_provider_post_helm_install(self) -> None:
        namespace = self.config.cluster.namespace

        bootstrap = self._start_step("bootstrap-netbox")
        if self.options.dry_run:
            self._finish_step(bootstrap, StepStatus.SKIPPED)
        else:
            cluster.wait_for_netbox(namespace, timeout=self.options.helm_timeout)
            output = cluster.run_nvcm_init(namespace, self._secrets_state["dcim_token"])
            if output.strip():
                bootstrap.output.extend(output.strip().splitlines())
            self._finish_step(bootstrap)

        publish = self._start_step("publish-netbox")
        if self.options.dry_run:
            self._finish_step(publish, StepStatus.SKIPPED)
            return

        try:
            cluster.publish_netbox_route(
                str(self.netbox.chart),
                release=self.netbox.release,
                namespace=namespace,
                timeout=self.options.helm_timeout,
            )
        except CommandError as exc:
            warning = (
                "Could not publish the NetBox route; retry the NetBox Helm upgrade with "
                f"--reuse-values. {exc.output}"
            )
            publish.output.append(warning)
            self.callback.on_log(f"Warning: {warning}")
        if not cluster.netbox_route_exists(namespace):
            warning = (
                "NetBox HTTPRoute was not created; use a port-forward and verify that "
                f"NVCM's Gateway exists in {namespace}."
            )
            publish.output.append(warning)
            self.callback.on_log(f"Warning: {warning}")
        self._finish_step(publish)
