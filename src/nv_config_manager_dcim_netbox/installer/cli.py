# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""`nvcm-netbox` -- NetBox-side helpers around the NVIDIA Config Manager installer.

Deployment itself is `nv-config-manager-installer init|validate|deploy` with
`dcim.provider: netbox`; this package's installer plugin runs inside it. The
commands here cover what that pipeline has no counterpart for.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import click
from nv_config_manager_dcim import DCIM_EVENT_CONTRACT_VERSION
from nv_config_manager_installer.deployer import DeployOptions, DeployStep, StepStatus
from nv_config_manager_installer.providers.errors import InstallerProviderError
from nv_config_manager_installer.providers.registry import resolve_provider
from nv_config_manager_installer.schema import NVConfigManagerInstallConfig

from nv_config_manager_dcim_netbox.installer import cluster
from nv_config_manager_dcim_netbox.installer.air import cli as air_cli
from nv_config_manager_dcim_netbox.installer.config import DEFAULT_PROFILE
from nv_config_manager_dcim_netbox.installer.settings import NetBoxInstallerSettings
from nv_config_manager_dcim_netbox.installer.shell import CommandError, require_tools
from nv_config_manager_dcim_netbox.installer.standalone import StandaloneDeployContext

DEFAULT_NAMESPACE = "nv-config-manager"

# The consumer pulls asynchronously; give it a few seconds to log the dispatch.
VERIFY_POLL_ATTEMPTS = 10
VERIFY_POLL_INTERVAL = 1.0

# Where to find the NVCM checkout that holds the mock topology blueprints.
NVCM_REPO_ENV = "NVCM_REPO"
DEFAULT_NVCM_REPO = Path("../nv-config-manager")


def _warn(message: str) -> None:
    click.secho(f"      {message}", fg="yellow")


class _StepPrinter:
    """Render provider step progress for the standalone commands."""

    def on_step_update(self, step: DeployStep) -> None:
        colour = {
            StepStatus.RUNNING: "cyan",
            StepStatus.SUCCESS: "green",
            StepStatus.FAILED: "red",
            StepStatus.SKIPPED: "yellow",
        }.get(step.status)
        click.secho(f"[{step.status.value:7}] {step.label}", fg=colour)
        if step.status == StepStatus.SKIPPED and step.output:
            click.echo(f"          {step.output[-1]}")
        if step.error:
            click.echo(f"          {step.error}")

    def on_log(self, message: str) -> None:
        click.echo(f"          {message}")

    def on_complete(self, success: bool, endpoints: list[str]) -> None:
        for endpoint in endpoints:
            click.echo(f"          {endpoint}")


def _nvcm_repo_candidate(nvcm_repo: Path | None) -> tuple[Path, str]:
    """Return the NVCM checkout path to try, and where that choice came from.

    Precedence: --nvcm-repo, then $NVCM_REPO, then the sibling default.
    """
    if nvcm_repo is not None:
        return nvcm_repo.expanduser().resolve(), "--nvcm-repo"
    if os.environ.get(NVCM_REPO_ENV):
        return Path(os.environ[NVCM_REPO_ENV]).expanduser().resolve(), f"${NVCM_REPO_ENV}"
    return DEFAULT_NVCM_REPO.expanduser().resolve(), "the default"


def _blueprint_root(nvcm_repo: Path | None) -> Path:
    """Return NVCM's mock topology context directory, or fail naming the path tried."""
    repo, source = _nvcm_repo_candidate(nvcm_repo)
    root = repo / "development" / "mock_topology" / "context"
    if not root.is_dir():
        raise click.ClickException(
            f"No mock topology blueprints at {root}\n(from {source})\n"
            f"Pass --nvcm-repo, or set {NVCM_REPO_ENV} to an nv-config-manager checkout."
        )
    return root


def _load_config(config_path: Path) -> NVConfigManagerInstallConfig:
    try:
        return NVConfigManagerInstallConfig.from_yaml(config_path)
    except Exception as exc:  # noqa: BLE001 - pydantic and YAML errors read the same here
        raise click.ClickException(f"Failed to load {config_path}: {exc}") from exc


def _netbox_settings(config_path: Path | None) -> NetBoxInstallerSettings:
    """Return the NetBox settings of an install config, or the defaults."""
    if config_path is None:
        return NetBoxInstallerSettings()
    config = _load_config(config_path)
    try:
        provider = resolve_provider(config)
    except InstallerProviderError as exc:
        raise click.ClickException(str(exc)) from exc
    settings = provider.settings(config)
    if not isinstance(settings, NetBoxInstallerSettings):
        raise click.ClickException(f"{config_path} does not select the netbox provider")
    return settings


class _CleanErrors(click.Group):
    """Report a failed helm/kubectl call as an error, not a Python traceback."""

    def invoke(self, ctx: click.Context):
        try:
            return super().invoke(ctx)
        except CommandError as exc:
            raise click.ClickException(str(exc)) from exc


@click.group(cls=_CleanErrors)
@click.version_option(package_name="nv-config-manager-dcim-netbox")
def main() -> None:
    """NetBox-side helpers for NVIDIA Config Manager deployments."""


main.add_command(air_cli.air)


def _nvcm_repo_option(fn):
    return click.option(
        "--nvcm-repo",
        type=click.Path(path_type=Path),
        default=None,
        help=f"Path to the nv-config-manager checkout (or set ${NVCM_REPO_ENV}).",
    )(fn)


@main.group("netbox")
def netbox_group() -> None:
    """Manage the NetBox side on its own."""


@contextlib.contextmanager
def _merged_blueprint(path: Path):
    """Yield the blueprint with the shared `common/` defaults merged in.

    NVCM's own context loader globs `common/*.yaml` into every context, so a
    context that defines no `global_defaults` of its own inherits them. A
    context's own file wins on a name clash.
    """
    common = path.parent / "common"
    if not common.is_dir():
        yield path
        return
    with tempfile.TemporaryDirectory(prefix="nvcm-blueprint-") as tmp:
        merged = Path(tmp) / path.name
        shutil.copytree(common, merged)
        shutil.copytree(path, merged, dirs_exist_ok=True)
        yield merged


@netbox_group.command("load-topology")
@click.option(
    "--blueprint",
    default="air_trial",
    show_default=True,
    help="Blueprint under NVCM's development/mock_topology/context/, or a path to one.",
)
@click.option("--namespace", default=DEFAULT_NAMESPACE, show_default=True)
@click.option(
    "--deployment-name",
    default=None,
    help="Fills the blueprint's {{ deployment_name }}. Defaults to the blueprint name.",
)
@_nvcm_repo_option
def load_topology(
    blueprint: str, namespace: str, deployment_name: str | None, nvcm_repo: Path | None
) -> None:
    """Load mock topology data into NetBox. NVCM's `make topology`.

    NetBox comes up empty, and an empty DCIM renders nothing, so this is what
    makes a fresh deployment demonstrable.
    """
    require_tools("kubectl")
    path = Path(blueprint)
    if not path.is_dir():
        root = _blueprint_root(nvcm_repo)
        path = root / blueprint
        if not path.is_dir():
            available = sorted(
                p.name for p in root.glob("*") if p.is_dir() and not p.name.startswith("_")
            )
            raise click.ClickException(
                f"No blueprint at {path}.\nAvailable: {', '.join(available) or 'none'}"
            )

    click.echo(f"Loading {path.name} into NetBox ...")
    with _merged_blueprint(path) as merged:
        output = cluster.load_topology(
            str(merged), namespace, deployment_name=deployment_name or path.name
        )
    click.echo(output.strip() or "done")
    click.secho(f"Loaded {path.name}.", fg="green")


@netbox_group.command("up")
@click.option(
    "--config",
    "-c",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=DEFAULT_PROFILE,
    show_default=True,
    help="Install config selecting dcim.provider: netbox.",
)
@click.option("--helm-timeout", default="10m", show_default=True)
@click.option("--recreate-secrets", is_flag=True, help="Replace the DCIM token and NATS Secrets.")
@click.option("--dry-run", is_flag=True, help="Resolve the provider and settings; touch nothing.")
def netbox_up(config_path: Path, helm_timeout: str, recreate_secrets: bool, dry_run: bool) -> None:
    """Deploy NetBox from an install config, without deploying NVCM.

    Runs the provider's pre-Helm steps (install, bootstrap) on their own. The
    DCIM token, NATS and database Secrets are created with the names NVCM's
    deploy uses, so a later `nv-config-manager-installer deploy` keeps them and
    NetBox and NVCM share one token and one database password. The CNPG
    operator must already be installed unless dcim.settings.database is bundled.
    """
    require_tools("kubectl", "helm")
    config = _load_config(config_path)
    try:
        provider = resolve_provider(config)
    except InstallerProviderError as exc:
        raise click.ClickException(str(exc)) from exc
    if not config.dcim.local:
        raise click.ClickException(
            f"{config_path} sets dcim.local: false, so there is no NetBox to deploy."
        )

    ctx = StandaloneDeployContext(
        config,
        DeployOptions(
            helm_timeout=helm_timeout, recreate_secrets=recreate_secrets, dry_run=dry_run
        ),
        _StepPrinter(),
        provider,
    )
    if not dry_run:
        ctx.seed_secrets()
    try:
        provider.pre_helm_install(ctx)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc

    click.secho("\nNetBox is up.", fg="green", bold=True)
    click.echo(
        f"  Token:   kubectl -n {config.cluster.namespace} get secret "
        f"{config.dcim.token_secret_name or provider.metadata.token_secret_name} "
        f"-o jsonpath='{{.data.{config.dcim.token_secret_key}}}' | base64 -d"
    )
    click.echo(f"  Deploy:  nv-config-manager-installer deploy {config_path} --project-root <nvcm>")


@main.command()
@click.option("--namespace", default=DEFAULT_NAMESPACE, show_default=True)
@click.option(
    "--device-id",
    default="1",
    show_default=True,
    help="NetBox device PK to reference in the synthetic event.",
)
@click.option(
    "--config",
    "-c",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Install config to read dcim.settings from. Defaults to the provider's defaults.",
)
def verify(namespace: str, device_id: str, config_path: Path | None) -> None:
    """Publish a synthetic change event and check the DCIM consumer handles it.

    Proves NetBox -> NATS -> NVCM consumer -> this provider's render handlers,
    which is the part of the stack that silently does nothing when the subject,
    stream or provider name disagree.
    """
    require_tools("kubectl")
    settings = _netbox_settings(config_path)
    nats_password = cluster.read_nats_password(namespace)
    if not nats_password:
        raise click.ClickException(
            f"No {cluster.NVCM_NATS_SECRET} secret in {namespace} -- NVCM does not look deployed."
        )

    # This command publishes its own envelope, so it never exercises NetBox's
    # publisher. Check that separately or a broken one looks like a pass.
    publisher = cluster.netbox_publisher_status(namespace)
    backend, importable = publisher.backend, publisher.importable
    # The subject has to match on all three sides -- this, NVCM's stream filter,
    # and the chart's nvcm.eventSubject -- or the broker drops every event with
    # nothing logged anywhere.
    if publisher.subject and publisher.subject != settings.event_subject:
        raise click.ClickException(
            f"NetBox publishes to {publisher.subject!r} but NVCM's stream filters on "
            f"{settings.event_subject!r}; every change event is dropped by the broker.\n"
            "Align nvcm.eventSubject in deploy/netbox with dcim.settings.event_subject in "
            "the install config."
        )
    # The stream is the other half of that agreement. The plugin's publisher
    # checks it at connect, but the failure lands in NetBox's log.
    if publisher.stream and publisher.stream != settings.event_stream:
        raise click.ClickException(
            f"NetBox publishes to the {publisher.stream!r} JetStream stream but NVCM "
            f"provisions {settings.event_stream!r}; every publish fails at connect.\n"
            "Align nvcm.eventStream in deploy/netbox with dcim.settings.event_stream in "
            "the install config."
        )
    # And the envelope contract. DCIMChangeEvent.from_dict exact-matches this, so
    # a plugin and an SDK built against different versions of it means every event
    # is rejected as invalid, with the publish itself reporting success.
    if publisher.contract_version and publisher.contract_version != DCIM_EVENT_CONTRACT_VERSION:
        raise click.ClickException(
            f"NetBox stamps events with contract_version {publisher.contract_version!r} but this "
            f"provider's SDK parses {DCIM_EVENT_CONTRACT_VERSION!r}; every event is rejected as "
            "invalid.\nUpgrade the plugin and nv-config-manager-dcim together."
        )
    if backend == "nats" and not importable:
        click.secho(
            "NetBox is configured to publish to NATS but cannot import nats-py, so real\n"
            "changes are lost at publish time. The check below bypasses that path and\n"
            "will still pass. Rebuild the NetBox image (it installs the [nats] extra).",
            fg="red",
        )
    elif backend == "log":
        _warn(
            "NetBox is on the `log` publisher, so real changes go to its log rather than\n"
            "      NVCM. That means the release was installed with no nvcm.natsPassword.\n"
            "      Switch it over with:\n"
            f"      helm upgrade {settings.release} {settings.chart} -n {namespace} "
            "--reuse-values --set nvcm.natsPassword=<pw>"
        )

    subject = settings.event_subject
    click.echo(f"Publishing a synthetic dcim.device event to {subject} ...")
    try:
        cluster.publish_test_event(namespace, subject, nats_password, device_id)
    except CommandError as exc:
        raise click.ClickException(f"Could not publish to NATS:\n{exc.output}") from exc

    # The consumer pulls asynchronously, so reading the log once right after
    # publishing races it and reports a working loop as broken.
    markers = ("batch queue operation", "queued", "not enabled")
    hit: list[str] = []
    for _ in range(VERIFY_POLL_ATTEMPTS):
        logs = cluster.dcim_consumer_logs(namespace)
        hit = [line for line in logs.splitlines() if any(m in line for m in markers)]
        if hit:
            break
        time.sleep(VERIFY_POLL_INTERVAL)

    if not hit:
        click.secho("No dispatch seen in the DCIM consumer logs.", fg="red")
        click.echo(
            "Check that dcim.settings.event_subject matches the plugin's nats_subject, and\n"
            "that the provider wheelhouse image installed (dcim.provider_packages)."
        )
        sys.exit(1)

    click.secho("Event dispatched through the NetBox provider:", fg="green")
    for line in hit[-3:]:
        click.echo(f"  {line.strip()}")

    if any("is not one of the available choices" in line for line in hit):
        click.echo(
            f"\nNote: NetBox has no device {device_id}, so the render was refused at the end of\n"
            "an otherwise complete path. Pass --device-id for one that exists to go further."
        )
    elif not any("queued" in line and "0 queued" not in line for line in hit):
        click.echo(
            "\nNote: '0 queued ... not enabled' still proves the full path. To get a real\n"
            "render, mark the device render-enabled via the plugin's DeviceConfigStatus."
        )


if __name__ == "__main__":  # pragma: no cover
    main()
