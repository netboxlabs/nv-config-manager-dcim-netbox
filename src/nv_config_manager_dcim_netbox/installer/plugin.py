# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""NetBox provider for the NVIDIA Config Manager installer.

Registered in the ``nv_config_manager.installer.dcim`` entry-point group as
``netbox``. The stock ``nv-config-manager-installer`` resolves it from
``dcim.provider`` and runs the hooks below around its own pipeline.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar, NamedTuple
from urllib.parse import urlsplit

from nv_config_manager_installer.deployer import StepStatus
from nv_config_manager_installer.helm_values import cnpg_cluster_values
from nv_config_manager_installer.providers.api import (
    AirContribution,
    DCIMInstallerProvider,
    DeployStepsContribution,
    ImageContribution,
    InstallerProviderMetadata,
    ProviderDeployContext,
    SecretsContribution,
    SecretValue,
    TUIContribution,
)
from nv_config_manager_installer.schema import (
    NVConfigManagerInstallConfig,
    SecretsMethod,
    SPIFFEProvider,
)
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
from nv_config_manager_dcim_netbox.installer.settings import (
    GROUP_HEADER,
    GROUP_SEPARATOR,
    NetBoxInstallerSettings,
    NetBoxSSOSettings,
)
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
    "NetBoxSSOSettings",
]

TOKEN_SECRET_NAME = "dcim-token"
# In-cluster Service fixed by deploy/netbox (netbox.fullnameOverride).
LOCAL_SERVER = f"http://{cluster.NETBOX_SERVICE}"

# NVCM core-chart contract for a local DCIM (`dcim.local.*` / `cnpg.dcim.*`).
OIDC_PROXY_PORT = 9110
AUTH_BYPASS_PATHS = ["/api/", "/graphql/"]
CNPG_BACKUP_SECRET = "cnpg-backup-credentials"
SPIFFE_VOLUME = "spiffe-workload-api"
REMOTE_USER_BACKEND = "netbox.authentication.RemoteUserBackend"
# oauth2-proxy's sign-out path, served on the NetBox hostname by NVCM's proxy.
OIDC_SIGN_OUT_PATH = "/oauth2/sign_out"

STEPS = DeployStepsContribution(
    before={
        "helm-install": (
            ("install-netbox", "Install NetBox"),
            ("bootstrap-netbox", "Bootstrap NetBox"),
        )
    },
    after={"helm-install": (("verify-netbox-route", "Verify NetBox route"),)},
)

# Core NATS Secrets NVCM creates with the shared password; read back on re-runs.
NVCM_NATS_SECRET = cluster.NVCM_NATS_SECRET


class SharedSecrets(NamedTuple):
    """Values NetBox is installed with that NVCM's Secrets also hold."""

    token: str
    nats_password: str
    db_user: str


def netbox_settings(config: NVConfigManagerInstallConfig) -> NetBoxInstallerSettings:
    """Return the validated ``dcim.settings`` block."""
    return NetBoxInstallerSettings.model_validate(config.dcim.settings)


def store_settings(config: NVConfigManagerInstallConfig, settings: NetBoxInstallerSettings) -> None:
    """Write *settings* back into ``dcim.settings`` without default noise."""
    config.dcim.settings = settings.model_dump(mode="json", exclude_defaults=True)


def _nats_service(nats_url: str) -> str:
    """Return the NATS host the chart renders into ``nats://<host>:4222``."""
    return urlsplit(nats_url).hostname or nats_url


def external_db_host(config: NVConfigManagerInstallConfig) -> str:
    """Return the user-managed PostgreSQL host for the DCIM database, or ""."""
    pg = config.external_services.postgres
    return pg.dcim_host if pg.enabled and pg.dcim_host else ""


def uses_cnpg(config: NVConfigManagerInstallConfig, settings: NetBoxInstallerSettings) -> bool:
    """Return whether the NetBox chart renders a CNPG cluster for this config."""
    return settings.database == "cnpg" and not external_db_host(config)


def monitoring_enabled(config: NVConfigManagerInstallConfig) -> bool:
    mon = config.infrastructure.monitoring
    return mon.enabled or mon.observability_enabled


def admin_groups(config: NVConfigManagerInstallConfig) -> list[str]:
    """Return NVCM's admin roles without the `all` sentinel."""
    return [role for role in config.rbac.admin_roles if role != "all"]


def sso_values(config: NVConfigManagerInstallConfig, sso: NetBoxSSOSettings) -> dict[str, Any]:
    """Return the upstream chart's remoteAuth values for NVCM's gateway identity headers."""
    if not config.sso.enabled:
        return {}
    admins = admin_groups(config)
    return {
        "netbox.remoteAuth.enabled": True,
        "netbox.remoteAuth.backends": [REMOTE_USER_BACKEND],
        "netbox.remoteAuth.header": sso.username_header,
        "netbox.remoteAuth.userEmail": "HTTP_X_AUTH_REQUEST_EMAIL",
        "netbox.remoteAuth.autoCreateUser": True,
        "netbox.remoteAuth.autoCreateGroups": True,
        "netbox.remoteAuth.groupSyncEnabled": True,
        "netbox.remoteAuth.groupHeader": GROUP_HEADER,
        "netbox.remoteAuth.groupSeparator": GROUP_SEPARATOR,
        "netbox.remoteAuth.superuserGroups": (
            list(sso.superuser_groups) if sso.superuser_groups is not None else admins
        ),
        "netbox.remoteAuth.staffGroups": (
            list(sso.staff_groups) if sso.staff_groups is not None else admins
        ),
        "netbox.remoteAuth.defaultGroups": list(sso.default_groups),
        "netbox.remoteAuth.defaultPermissions": dict(sso.default_permissions),
        "netbox.logoutRedirectUrl": OIDC_SIGN_OUT_PATH,
    }


def spiffe_values(config: NVConfigManagerInstallConfig) -> dict[str, Any]:
    """Return extraVolumes/extraVolumeMounts for the SPIFFE workload API socket."""
    if not config.spiffe.enabled:
        return {}
    sp = config.spiffe
    volume: dict[str, Any] = {"name": SPIFFE_VOLUME}
    if sp.provider == SPIFFEProvider.TELEPORT:
        volume["hostPath"] = {"path": sp.socket_host_path, "type": "Directory"}
    else:
        volume["csi"] = {"driver": "csi.spiffe.io", "readOnly": True}
    mount = {"name": SPIFFE_VOLUME, "mountPath": sp.socket_mount_path, "readOnly": True}
    return {
        "netbox.extraVolumes": [volume],
        "netbox.extraVolumeMounts": [mount],
        "netbox.worker.extraVolumes": [volume],
        "netbox.worker.extraVolumeMounts": [mount],
    }


def database_values(
    config: NVConfigManagerInstallConfig, settings: NetBoxInstallerSettings, db_user: str
) -> dict[str, Any]:
    """Return the chart's database source: CNPG with backups, external, or bundled."""
    values: dict[str, Any] = {
        "nvcm.database.user": db_user,
        "netbox.externalDatabase.username": db_user,
    }
    if host := external_db_host(config):
        port = config.external_services.postgres.port
        values.update(
            {
                "nvcm.database.cnpg.enabled": False,
                "nvcm.database.external.host": host,
                "nvcm.database.external.port": port,
                "netbox.externalDatabase.host": host,
                "netbox.externalDatabase.port": port,
            }
        )
        return values
    if settings.database == "bundled":
        values.update({"nvcm.database.cnpg.enabled": False, "netbox.postgresql.enabled": True})
        return values
    backup = config.infrastructure.cnpg_s3_backup
    if backup.enabled:
        shared = cnpg_cluster_values(config, True)["backup"]
        values.update(
            {
                "nvcm.database.cnpg.backup.enabled": True,
                "nvcm.database.cnpg.backup.schedule": shared["schedule"],
                "nvcm.database.cnpg.backup.retentionPolicy": shared["retentionPolicy"],
                "nvcm.database.cnpg.backup.destinationType": "s3",
                "nvcm.database.cnpg.backup.s3.endpoint": backup.endpoint,
                "nvcm.database.cnpg.backup.s3.bucket": backup.bucket,
                "nvcm.database.cnpg.backup.s3.path": backup.path,
                "nvcm.database.cnpg.backup.credentialsSecret": CNPG_BACKUP_SECRET,
            }
        )
    return values


def monitoring_values(config: NVConfigManagerInstallConfig) -> dict[str, Any]:
    if not monitoring_enabled(config):
        return {}
    return {"nvcm.monitoring.enabled": True, "netbox.metrics.enabled": True}


def split_overrides(values: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """Split dotted-key values into ``--set`` (strings) and ``--set-json`` (the rest)."""
    plain: list[str] = []
    as_json: list[str] = []
    for key, value in values.items():
        if isinstance(value, str):
            plain.append(f"{key}={cluster.helm_set_value(value)}")
        else:
            as_json.append(f"{key}={json.dumps(value)}")
    return plain, as_json


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
    # NVCM creates `cluster-dcim-app` (username/password) before install-netbox;
    # CNPG bootstraps the database from it and NetBox connects with it.
    secrets: ClassVar[SecretsContribution] = SecretsContribution(databases=(cluster.DB_NAME,))
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
        if config.dcim.local and config.secrets.method == SecretsMethod.ESO:
            # The install needs the token and NATS password NVCM generates;
            # under ESO they live only in Vault.
            errors.append(
                "a local NetBox requires secrets.method kubernetes; with ESO, run NetBox "
                "externally (dcim.local false) and set dcim.server"
            )
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
        local = config.dcim.local
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
            # NVCM's chart renders the browser, auth-bypass and svc-* HTTPRoutes,
            # the OIDC-proxy upstream and the blackbox Probe from this block.
            "local": {
                "enabled": local,
                "backendName": cluster.NETBOX_SERVICE,
                "backendPort": 80,
                "gateway": {
                    "hostname": settings.hostname,
                    "svcHostname": "",
                    "proxyPort": OIDC_PROXY_PORT,
                    "authBypass": {"enabled": True, "paths": list(AUTH_BYPASS_PATHS)},
                },
                "probe": {"path": settings.readiness_path},
            },
        }
        if local:
            dcim["server"] = LOCAL_SERVER
        # NVCM's ExternalSecret for cluster-dcim-app and its CNPG PodMonitor
        # follow cnpg.dcim; the Cluster itself is rendered by the NetBox chart.
        cnpg_dcim = cnpg_cluster_values(config, local and uses_cnpg(config, settings))
        cnpg_dcim["clusterName"] = cluster.CNPG_CLUSTER
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
            "cnpg": {"dcim": cnpg_dcim},
            "nautobotNats": {"accounts": {"nautobot": {"enabled": False}}},
        }

    # -- deploy --------------------------------------------------------------

    def deploy_steps(self) -> DeployStepsContribution:
        return STEPS

    def _shared_secrets(self, ctx: ProviderDeployContext) -> SharedSecrets:
        """Return the API token, NATS password and database user NetBox is installed with.

        NVCM skips Secrets that already exist unless --recreate-secrets, so on a
        re-run the cluster's values win over the freshly generated state.
        """
        state = ctx.secrets_state
        token = state.get("dcim_token", "")
        nats_password = state.get("nats_password", "")
        db_user = state.get(f"{cluster.DB_NAME}_db_user", "") or cluster.DB_NAME
        if not ctx.options.recreate_secrets:
            dcim = ctx.config.dcim
            ns = ctx.config.cluster.namespace
            secret_name = dcim.token_secret_name or TOKEN_SECRET_NAME
            token = ctx.k8s.read_secret_data(secret_name, ns).get(dcim.token_secret_key) or token
            nats_password = (
                ctx.k8s.read_secret_data(NVCM_NATS_SECRET, ns).get("password") or nats_password
            )
            db_user = ctx.k8s.read_secret_data(cluster.DB_SECRET, ns).get("username") or db_user
        if not token or not nats_password:
            raise RuntimeError(
                "The NetBox install needs the DCIM token and NATS password from the "
                "installer secret state; secrets.method must be kubernetes for a local NetBox."
            )
        return SharedSecrets(token, nats_password, db_user)

    def _chart_overrides(
        self, ctx: ProviderDeployContext, settings: NetBoxInstallerSettings, db_user: str
    ) -> tuple[list[str], list[str]]:
        """Return ``--set`` and ``--set-json`` overrides that bind the chart to this deployment."""
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
        values: dict[str, Any] = {}
        values.update(database_values(ctx.config, settings, db_user))
        values.update(sso_values(ctx.config, settings.sso))
        values.update(spiffe_values(ctx.config))
        values.update(monitoring_values(ctx.config))
        plain, as_json = split_overrides(values)
        overrides.extend(plain)
        overrides.extend(settings.helm_set)
        return overrides, as_json

    def _check_database(
        self, ctx: ProviderDeployContext, settings: NetBoxInstallerSettings
    ) -> None:
        """Fail early when the chosen PostgreSQL source cannot be installed."""
        if settings.database == "bundled":
            return
        ns = ctx.config.cluster.namespace
        if not ctx.k8s.secret_exists(cluster.DB_SECRET, ns):
            raise RuntimeError(
                f"The {cluster.DB_SECRET} Secret is missing in {ns}; NVCM's deploy creates it "
                "from the provider's database contribution and `nvcm-netbox netbox up` seeds it."
            )
        if uses_cnpg(ctx.config, settings) and not cluster.cnpg_operator_installed():
            raise RuntimeError(
                "The CloudNativePG operator is not installed, so the NetBox chart cannot "
                "create its database. Deploy with --install-cnpg-operator, install the "
                "operator first, or set dcim.settings.database: bundled."
            )

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
        shared = self._shared_secrets(ctx)

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

        self._check_database(ctx, settings)

        if not ctx.config.cluster.airgapped:
            cluster.helm_dependency_build(settings.chart)
        extra_set, extra_set_json = self._chart_overrides(ctx, settings, shared.db_user)
        cluster.install_netbox(
            str(settings.chart),
            release=settings.release,
            namespace=ns,
            api_token=shared.token,
            image_repository=settings.image_repository,
            image_tag=settings.image_tag,
            nats_password=shared.nats_password,
            workflow_url=cluster.workflow_url(
                ctx.config.cluster.hostname, ctx.config.infrastructure.tls
            ),
            extra_set=extra_set,
            extra_set_json=extra_set_json,
            timeout=ctx.options.helm_timeout,
        )
        ctx.finish_step(step)

        bootstrap = ctx.start_step("bootstrap-netbox")
        cluster.wait_for_netbox(ns, timeout=ctx.options.helm_timeout)
        output = cluster.run_nvcm_init(ns, shared.token)
        if output.strip():
            bootstrap.output.extend(output.strip().splitlines())
        ctx.finish_step(bootstrap)

    def post_helm_install(self, ctx: ProviderDeployContext) -> None:
        if not ctx.config.dcim.local:
            ctx.skip_step("verify-netbox-route", "External NetBox in use")
            return

        verify = ctx.start_step("verify-netbox-route")
        if ctx.options.dry_run:
            ctx.finish_step(verify, StepStatus.SKIPPED)
            return

        ns = ctx.config.cluster.namespace
        if not cluster.netbox_route_exists(ns):
            warning = (
                "NetBox HTTPRoute was not created; NVCM's chart renders it from dcim.local, "
                f"so check that its release carries that block and that the Gateway exists in {ns}."
                " Use a port-forward meanwhile."
            )
            verify.output.append(warning)
            ctx.callback.on_log(f"Warning: {warning}")
        ctx.finish_step(verify)

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
