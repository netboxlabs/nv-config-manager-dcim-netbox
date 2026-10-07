# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""NetBox provider for the NVIDIA Config Manager installer.

Registered in the ``nv_config_manager.installer.dcim`` entry-point group as
``netbox``. The stock ``nv-config-manager-installer`` resolves it from
``dcim.provider`` and runs the hooks below around its own pipeline.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlsplit

from nv_config_manager_installer.deployer import StepStatus
from nv_config_manager_installer.providers.api import (
    AirContribution,
    DCIMInstallerProvider,
    DeployStepsContribution,
    ImageContribution,
    InstallerProviderMetadata,
    ProviderDeployContext,
    SecretValue,
    TUIContribution,
)
from nv_config_manager_installer.schema import NVConfigManagerInstallConfig
from nv_config_manager_installer.tui.screens.external_services import ProviderDCIMPanel
from textual.app import ComposeResult
from textual.widgets import Input, Label

from nv_config_manager_dcim_netbox.installer import cluster
from nv_config_manager_dcim_netbox.installer.air_hooks import (
    NETBOX_LAUNCH_STATUS,
    NetBoxAirHooks,
    NetBoxAirSettings,
    NetBoxPopulationPanel,
)
from nv_config_manager_dcim_netbox.installer.config import (
    DEFAULT_CHART,
    EVENT_STREAM,
    EVENT_SUBJECT,
    IMAGE_TAG,
    NETBOX_IMAGE,
    PROVIDER_IMAGE,
    PROVIDER_NAME,
)
from nv_config_manager_dcim_netbox.installer.settings import NetBoxInstallerSettings
from nv_config_manager_dcim_netbox.installer.shell import CommandError
from nv_config_manager_dcim_netbox.installer.tokens import generate_api_token, is_valid_api_token

__all__ = [
    "DEFAULT_CHART",
    "EVENT_STREAM",
    "EVENT_SUBJECT",
    "IMAGE_TAG",
    "LOCAL_SERVER",
    "NETBOX_IMAGE",
    "PROVIDER_IMAGE",
    "NetBoxAirSettings",
    "NetBoxDCIMPanel",
    "NetBoxInstallerProvider",
    "NetBoxInstallerSettings",
]

TOKEN_SECRET_NAME = "dcim-token"
# In-cluster Service fixed by deploy/netbox (netbox.fullnameOverride).
LOCAL_SERVER = f"http://{cluster.NETBOX_SERVICE}"

STEPS = DeployStepsContribution(
    before={
        "helm-install": (
            ("install-netbox", "Install NetBox"),
            ("bootstrap-netbox", "Bootstrap NetBox"),
        )
    },
    after={"helm-install": (("publish-netbox", "Publish NetBox route"),)},
)

# Core NATS Secrets NVCM creates with the shared password; read back on re-runs.
NVCM_NATS_SECRET = cluster.NVCM_NATS_SECRET


def netbox_settings(config: NVConfigManagerInstallConfig) -> NetBoxInstallerSettings:
    """Return the validated ``dcim.settings`` block."""
    return NetBoxInstallerSettings.model_validate(config.dcim.settings)


def store_settings(config: NVConfigManagerInstallConfig, settings: NetBoxInstallerSettings) -> None:
    """Write *settings* back into ``dcim.settings`` without default noise."""
    config.dcim.settings = settings.model_dump(mode="json", exclude_defaults=True)


def _nats_service(nats_url: str) -> str:
    """Return the NATS host the chart renders into ``nats://<host>:4222``."""
    return urlsplit(nats_url).hostname or nats_url


class NetBoxDCIMPanel(ProviderDCIMPanel):
    """NetBox image tag, hostname and superuser name under the DCIM controls."""

    def __init__(self, config: NVConfigManagerInstallConfig, **kwargs: object) -> None:
        super().__init__(config, **kwargs)
        self._settings = netbox_settings(config)

    def compose(self) -> ComposeResult:
        yield Label("NetBox", classes="subsection-label")
        yield Label("NetBox image tag", classes="field-label")
        yield Input(value=self._settings.image_tag, placeholder=IMAGE_TAG, id="netbox-image-tag")
        yield Label("NetBox hostname (empty: netbox.<cluster hostname>)", classes="field-label")
        yield Input(
            value=self._settings.hostname,
            placeholder="netbox.config-manager.local",
            id="netbox-hostname",
        )
        yield Label("NetBox superuser name", classes="field-label")
        yield Input(
            value=self._settings.superuser_name, placeholder="admin", id="netbox-superuser-name"
        )

    def write_to_config(self, config: NVConfigManagerInstallConfig) -> None:
        settings = self._settings.model_copy(
            update={
                "image_tag": self.query_one("#netbox-image-tag", Input).value.strip() or IMAGE_TAG,
                "hostname": self.query_one("#netbox-hostname", Input).value.strip(),
                "superuser_name": self.query_one("#netbox-superuser-name", Input).value.strip()
                or "admin",
            }
        )
        self._settings = settings
        store_settings(config, settings)

    def sync_from_config(self, config: NVConfigManagerInstallConfig) -> None:
        super().sync_from_config(config)
        self._settings = netbox_settings(config)
        self.query_one("#netbox-image-tag", Input).value = self._settings.image_tag
        self.query_one("#netbox-hostname", Input).value = self._settings.hostname
        self.query_one("#netbox-superuser-name", Input).value = self._settings.superuser_name


class NetBoxInstallerProvider(DCIMInstallerProvider):
    """Deploy NetBox and wire it to NVCM through the common installer."""

    metadata: ClassVar[InstallerProviderMetadata] = InstallerProviderMetadata(
        name=PROVIDER_NAME,
        display_name="NetBox",
        token_secret_name=TOKEN_SECRET_NAME,
        event_stream=EVENT_STREAM,
        event_subject=EVENT_SUBJECT,
        server_placeholder="https://netbox.example.com",
    )
    settings_model: ClassVar[type[NetBoxInstallerSettings]] = NetBoxInstallerSettings
    # The NetBox and provider images are built by `make images` from this
    # repository and loaded into the cluster by the Makefile. NVCM's deployer
    # builds relative to --project-root (the NVCM checkout) and passes none of
    # the build contexts these Dockerfiles need, so they are not declared here.
    # See NOTES.md.
    images: ClassVar[ImageContribution] = ImageContribution()
    tui: ClassVar[TUIContribution] = TUIContribution(dcim_panel=NetBoxDCIMPanel)
    air: ClassVar[AirContribution | None] = AirContribution(
        launch_status=NETBOX_LAUNCH_STATUS,
        hooks=NetBoxAirHooks(),
        settings_model=NetBoxAirSettings,
        population_panel=NetBoxPopulationPanel,
    )

    # -- configuration -------------------------------------------------------

    def validate_config(self, config: NVConfigManagerInstallConfig) -> list[str]:
        errors: list[str] = []
        if not config.dcim.local and not config.dcim.server:
            errors.append("dcim.server is required when dcim.local is false")
        token = config.secrets.k8s.group("dcim").values.get("token", "")
        if token and not is_valid_api_token(token):
            errors.append(
                "secrets.k8s.dcim.values.token must be a NetBox v2 token "
                "(nbt_<12 chars>.<40 chars>); NetBox silently skips a malformed one"
            )
        return errors

    # -- secrets -------------------------------------------------------------

    def generate_secret_state(
        self,
        config: NVConfigManagerInstallConfig,
        state: dict[str, str],
        value_of: SecretValue,
    ) -> None:
        # One v2 token for both sides: nvcm_init installs it into NetBox and
        # NVCM authenticates with it. NVCM's own fallback is not a v2 token.
        if not state.get("dcim_token"):
            state["dcim_token"] = generate_api_token()

    # -- helm ----------------------------------------------------------------

    def helm_values(
        self,
        config: NVConfigManagerInstallConfig,
        secrets_state: Mapping[str, str],
        *,
        is_local: bool,
        local_tags: Mapping[str, str] | None,
    ) -> dict[str, Any]:
        settings = netbox_settings(config)
        packages = [
            {"name": package.name, "image": package.image, "pullPolicy": package.pull_policy}
            for package in config.dcim.provider_packages
        ] or [
            {"name": PROVIDER_NAME, "image": settings.provider_image, "pullPolicy": "IfNotPresent"}
        ]
        dcim: dict[str, Any] = {
            "displayName": "NetBox",
            "readiness": {"path": settings.readiness_path},
            "events": {"stream": settings.event_stream, "subject": settings.event_subject},
            "providerPackages": {"enabled": True, "images": packages},
        }
        if config.dcim.local:
            dcim["server"] = LOCAL_SERVER
        return {
            "dcim": dcim,
            "externalServices": {
                "nats": {
                    "streams": {
                        "dcim": {
                            "name": settings.event_stream,
                            "subject": settings.event_subject,
                            "subjects": [settings.event_subject],
                            "consumer": {"name": settings.consumer_name},
                        }
                    }
                }
            },
            "nautobotNats": {"accounts": {"nautobot": {"enabled": False}}},
        }

    # -- deploy --------------------------------------------------------------

    def deploy_steps(self) -> DeployStepsContribution:
        return STEPS

    def _shared_secrets(self, ctx: ProviderDeployContext) -> tuple[str, str]:
        """Return the API token and NATS password NetBox must be installed with.

        NVCM skips Secrets that already exist unless --recreate-secrets, so on a
        re-run the cluster's values win over the freshly generated state.
        """
        state = ctx.secrets_state
        token = state.get("dcim_token", "")
        nats_password = state.get("nats_password", "")
        if not ctx.options.recreate_secrets:
            dcim = ctx.config.dcim
            ns = ctx.config.cluster.namespace
            secret_name = dcim.token_secret_name or TOKEN_SECRET_NAME
            token = ctx.k8s.read_secret_data(secret_name, ns).get(dcim.token_secret_key) or token
            nats_password = (
                ctx.k8s.read_secret_data(NVCM_NATS_SECRET, ns).get("password") or nats_password
            )
        if not token or not nats_password:
            raise RuntimeError(
                "The NetBox install needs the DCIM token and NATS password from the "
                "installer secret state; secrets.method must be kubernetes for a local NetBox."
            )
        return token, nats_password

    def _chart_overrides(
        self, ctx: ProviderDeployContext, settings: NetBoxInstallerSettings
    ) -> list[str]:
        """Return ``--set`` overrides that bind the chart to this deployment."""
        esc = cluster.helm_set_value
        overrides = [
            f"nvcm.natsService={esc(_nats_service(ctx.nats_url))}",
            f"nvcm.natsUser={esc(ctx.nats_user)}",
            f"nvcm.eventStream={esc(settings.event_stream)}",
            f"nvcm.eventSubject={esc(settings.event_subject)}",
            f"netbox.superuser.name={esc(settings.superuser_name)}",
        ]
        if settings.superuser_password:
            overrides.append(f"netbox.superuser.password={esc(settings.superuser_password)}")
        if hostname := ctx.config.cluster.hostname:
            overrides.append(f"nvcm.gateway.baseHostname={esc(hostname)}")
        if settings.hostname:
            overrides.append(f"nvcm.gateway.hostname={esc(settings.hostname)}")
        overrides.extend(settings.helm_set)
        return overrides

    def pre_helm_install(self, ctx: ProviderDeployContext) -> None:
        if not ctx.config.dcim.local:
            ctx.skip_step("install-netbox", "External NetBox in use")
            ctx.skip_step("bootstrap-netbox", "External NetBox in use")
            return

        step = ctx.start_step("install-netbox")
        if ctx.options.dry_run:
            ctx.finish_step(step, StepStatus.SKIPPED)
            ctx.skip_step("bootstrap-netbox", "Dry run")
            return

        settings = netbox_settings(ctx.config)
        ns = ctx.config.cluster.namespace
        token, nats_password = self._shared_secrets(ctx)

        stuck = cluster.pending_release_status(settings.release, ns)
        if stuck:
            raise RuntimeError(
                f"The {settings.release!r} release is stuck in {stuck}. Clear it with "
                f"`helm uninstall {settings.release} -n {ns}` "
                "or roll it back to a completed revision."
            )

        stale = cluster.orphaned_data_volumes(settings.release, ns)
        if stale:
            names = " ".join(stale)
            raise RuntimeError(
                "NetBox datastore volumes remain but their credentials are gone: "
                f"{names}. Delete them before retrying with "
                f"`kubectl delete pvc -n {ns} {names}`."
            )

        if not ctx.config.cluster.airgapped:
            cluster.helm_dependency_build(settings.chart)
        cluster.install_netbox(
            str(settings.chart),
            release=settings.release,
            namespace=ns,
            api_token=token,
            image_repository=settings.image_repository,
            image_tag=settings.image_tag,
            nats_password=nats_password,
            workflow_url=cluster.workflow_url(
                ctx.config.cluster.hostname, ctx.config.infrastructure.tls
            ),
            extra_set=self._chart_overrides(ctx, settings),
            timeout=ctx.options.helm_timeout,
        )
        ctx.finish_step(step)

        bootstrap = ctx.start_step("bootstrap-netbox")
        cluster.wait_for_netbox(ns, timeout=ctx.options.helm_timeout)
        output = cluster.run_nvcm_init(ns, token)
        if output.strip():
            bootstrap.output.extend(output.strip().splitlines())
        ctx.finish_step(bootstrap)

    def post_helm_install(self, ctx: ProviderDeployContext) -> None:
        if not ctx.config.dcim.local:
            ctx.skip_step("publish-netbox", "External NetBox in use")
            return

        publish = ctx.start_step("publish-netbox")
        if ctx.options.dry_run:
            ctx.finish_step(publish, StepStatus.SKIPPED)
            return

        settings = netbox_settings(ctx.config)
        ns = ctx.config.cluster.namespace
        try:
            cluster.publish_netbox_route(
                str(settings.chart),
                release=settings.release,
                namespace=ns,
                timeout=ctx.options.helm_timeout,
            )
        except CommandError as exc:
            warning = (
                "Could not publish the NetBox route; retry the NetBox Helm upgrade with "
                f"--reuse-values. {exc.output}"
            )
            publish.output.append(warning)
            ctx.callback.on_log(f"Warning: {warning}")
        if not cluster.netbox_route_exists(ns):
            warning = (
                "NetBox HTTPRoute was not created; use a port-forward and verify that "
                f"NVCM's Gateway exists in {ns}."
            )
            publish.output.append(warning)
            ctx.callback.on_log(f"Warning: {warning}")
        ctx.finish_step(publish)

    def endpoints(
        self, config: NVConfigManagerInstallConfig, hostname: str, proto: str
    ) -> list[str]:
        if config.dcim.local:
            settings = netbox_settings(config)
            return [f"NetBox:           {proto}://{settings.netbox_hostname(hostname)}"]
        if config.dcim.server:
            return [f"NetBox (ext):     {config.dcim.server}"]
        return []

    def bootstrap_secret_hints(self, project_root: Path | None) -> list[tuple[str, str, str]]:
        return []
