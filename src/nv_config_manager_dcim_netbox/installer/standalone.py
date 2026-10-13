# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""A ProviderDeployContext for running provider hooks outside NVCM's deployer."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nv_config_manager_installer.deployer import (
    DeployCallback,
    DeployOptions,
    DeployStep,
    StepStatus,
)
from nv_config_manager_installer.k8s import K8sClient
from nv_config_manager_installer.providers.api import DCIMInstallerProvider
from nv_config_manager_installer.schema import NVConfigManagerInstallConfig
from nv_config_manager_installer.secrets import generate_secrets

from nv_config_manager_dcim_netbox.installer import cluster

# Core NATS Secrets NVCM's deploy creates from the shared password.
_NATS_SECRETS = ("nats-sys", "nats-nv-config-manager")


class StandaloneDeployContext:
    """Enough of the deployer surface to run `pre_helm_install` on its own.

    Secrets are created here with the same names NVCM's deploy uses, so a later
    `nv-config-manager-installer deploy` finds them in place and keeps them.
    """

    def __init__(
        self,
        config: NVConfigManagerInstallConfig,
        options: DeployOptions,
        callback: DeployCallback,
        provider: DCIMInstallerProvider,
        k8s: K8sClient | None = None,
    ) -> None:
        self.config = config
        self.options = options
        self.callback = callback
        self.provider = provider
        self.provider_state: dict[str, Any] = {}
        self._secrets_state: dict[str, str] = {}
        self._k8s = k8s
        contribution = provider.deploy_steps()
        self.steps = {
            step_id: DeployStep(step_id, label)
            for group in (*contribution.before.values(), *contribution.after.values())
            for step_id, label in group
        }

    # -- ProviderDeployContext -----------------------------------------------

    @property
    def k8s(self) -> K8sClient:
        if self._k8s is None:
            self._k8s = K8sClient()
        return self._k8s

    @property
    def secrets_state(self) -> dict[str, str]:
        return self._secrets_state

    @property
    def is_rerun(self) -> bool:
        return False

    @property
    def nats_user(self) -> str:
        nats = self.config.external_services.nats
        return nats.user if nats.enabled else "nv-config-manager"

    @property
    def nats_url(self) -> str:
        nats = self.config.external_services.nats
        if nats.enabled:
            return nats.server
        return f"nats://{self.config.cluster.release_name}-nats:4222"

    def start_step(self, step_id: str) -> DeployStep:
        step = self.steps[step_id]
        step.status = StepStatus.RUNNING
        self.callback.on_step_update(step)
        return step

    def finish_step(self, step: DeployStep, status: StepStatus | None = None) -> None:
        step.status = status or StepStatus.SUCCESS
        self.callback.on_step_update(step)

    def skip_step(self, step_id: str, reason: str = "") -> None:
        step = self.steps[step_id]
        step.status = StepStatus.SKIPPED
        if reason:
            step.output.append(reason)
        self.callback.on_step_update(step)

    def apply_secret(self, step: DeployStep, name: str, data: dict[str, str]) -> None:
        ns = self.config.cluster.namespace
        existed = self.k8s.secret_exists(name, ns)
        if existed and not self.options.recreate_secrets:
            message = f"Secret exists, skipping: {name}"
        else:
            self.k8s.apply_secret(name, ns, data)
            message = f"{'Recreated' if existed else 'Created'}: {name}"
        step.output.append(message)
        self.callback.on_log(message)

    def check_content_diff(
        self, paths: list[Path], pvc_name: str, namespace: str, label: str
    ) -> bool:
        return True

    def content_hash(self, paths: list[Path]) -> str:
        raise NotImplementedError("content hashing is only available inside Deployer.run()")

    # -- standalone preparation ----------------------------------------------

    def seed_secrets(self) -> None:
        """Generate the installer secret state and adopt or create the shared Secrets.

        The DCIM token, NATS password and database credentials already in the
        cluster win, as they do in NVCM's deploy. Missing ones are created from
        the generated state. `cluster-dcim-app` carries both keys NVCM writes
        (username, password), so CNPG and NetBox read it unchanged either way.
        """
        state = generate_secrets(self.config, self.provider)
        ns = self.config.cluster.namespace
        dcim = self.config.dcim
        token_secret = dcim.token_secret_name or self.provider.metadata.token_secret_name
        db = cluster.DB_NAME
        wanted: list[tuple[str, dict[str, str]]] = [
            (token_secret, {dcim.token_secret_key: "dcim_token"}),
            *((name, {"password": "nats_password"}) for name in _NATS_SECRETS),
            (cluster.DB_SECRET, {"username": f"{db}_db_user", "password": f"{db}_db_password"}),
        ]
        self.k8s.ensure_namespace(ns)
        for name, keys in wanted:
            existing = self.k8s.read_secret_data(name, ns)
            for key, state_key in keys.items():
                if value := existing.get(key):
                    state[state_key] = value
        for name, keys in wanted:
            existing = self.k8s.read_secret_data(name, ns)
            if not all(existing.get(key) for key in keys):
                self.k8s.apply_secret(name, ns, {key: state[sk] for key, sk in keys.items()})
                self.callback.on_log(f"Created: {name}")
        self._secrets_state = state
