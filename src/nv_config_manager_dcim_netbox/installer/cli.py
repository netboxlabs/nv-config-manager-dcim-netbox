# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""`nvcm-netbox` — bring up NVIDIA Config Manager with NetBox as its DCIM provider."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import click
import yaml
from nv_config_manager_installer.deployer import DeployOptions, DeployStep, StepStatus
from nv_config_manager_installer.schema import ImageSource, NVConfigManagerInstallConfig

from nv_config_manager_dcim_netbox.installer import cluster
from nv_config_manager_dcim_netbox.installer import config as cfg
from nv_config_manager_dcim_netbox.installer.air import cli as air_cli
from nv_config_manager_dcim_netbox.installer.deployer import (
    NetBoxDeployer,
    NetBoxDeploymentSettings,
)
from nv_config_manager_dcim_netbox.installer.shell import CommandError, require_tools, stream
from nv_config_manager_dcim_netbox.installer.tokens import (
    generate_api_token,
    generate_nats_password,
    is_valid_api_token,
    is_valid_nats_password,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CHART = _REPO_ROOT / "deploy" / "netbox"
DEFAULT_NAMESPACE = "nv-config-manager"
DEFAULT_NETBOX_RELEASE = "netbox"
# Matches netbox.image.tag in deploy/netbox/values.yaml.
DEFAULT_NETBOX_TAG = "0.1.0"

# The consumer pulls asynchronously; give it a few seconds to log the dispatch.
VERIFY_POLL_ATTEMPTS = 10
VERIFY_POLL_INTERVAL = 1.0

# Where to find the NVCM checkout. The sibling default only holds if the repos
# were cloned side by side; set NVCM_REPO once instead of passing --nvcm-repo
# on every invocation.
NVCM_REPO_ENV = "NVCM_REPO"
DEFAULT_NVCM_REPO = Path("../nv-config-manager")


def _step(number: int, total: int, message: str) -> None:
    click.secho(f"[{number}/{total}] {message}", fg="cyan", bold=True)


def _ok(message: str) -> None:
    click.secho(f"      {message}", fg="green")


def _warn(message: str) -> None:
    click.secho(f"      {message}", fg="yellow")


class _ClickDeployCallback:
    """Render the inherited deployer's progress in the provider CLI."""

    def on_step_update(self, step: DeployStep) -> None:
        colour = {
            StepStatus.RUNNING: "cyan",
            StepStatus.SUCCESS: "green",
            StepStatus.FAILED: "red",
            StepStatus.SKIPPED: "yellow",
        }.get(step.status)
        click.secho(f"[{step.status.value:7}] {step.label}", fg=colour)
        if step.error:
            click.echo(f"          {step.error}")

    def on_log(self, message: str) -> None:
        click.echo(f"          {message}")

    def on_complete(self, success: bool, endpoints: list[str]) -> None:
        if success:
            for endpoint in endpoints:
                click.echo(f"          {endpoint}")


def _nvcm_installer_argv(nvcm_repo: Path, *args: str) -> list[str]:
    """Build a `uv run` invocation of NVCM's installer CLI.

    We shell out rather than importing nv_config_manager_installer. Their
    Deployer.run() is a hardcoded call sequence in a very large module on a
    force-pushed branch; the CLI and the config-file schema are the stable
    surface, so those are the only things we depend on.

    `--directory` runs it from the NVCM checkout, because their
    find_project_root() walks up from the working directory looking for
    `deploy/`, a `Makefile` or `.git`. Run from here it finds *this* repo, and
    the image builds then fail on contexts like `components/nats-ready` that
    only exist in theirs. Any path passed in *args must be absolute.
    """
    return [
        "uv",
        "run",
        "--directory",
        str(nvcm_repo),
        "--project",
        str(nvcm_repo / "installer"),
        "nv-config-manager-installer",
        *args,
    ]


def _config_options(
    base: dict,
    *,
    api_token: str,
    namespace: str,
    size: str,
    provider_image: str | None = None,
    public_url: str | None = None,
    nats_password: str | None = None,
) -> cfg.ConfigOptions:
    """Resolve the installer config options `config` and `up` both write.

    Shared so the two cannot drift: `up` had its own copy and omitted the
    public_url fallback, resetting a profile's value to the default port-forward.
    """
    dcim = base.get("dcim", {})
    defaults = cfg.ConfigOptions(api_token=api_token)
    return cfg.ConfigOptions(
        api_token=api_token,
        namespace=namespace,
        size=size,
        provider_image=provider_image
        or (dcim.get("provider_packages") or [{}])[0].get("image")
        or defaults.provider_image,
        netbox_public_url=public_url or dcim.get("public_url") or defaults.netbox_public_url,
        nats_password=nats_password or "",
    )


def _existing_nats_password(config: dict) -> str:
    """Return a NATS password already in the config, so re-runs keep it.

    Regenerating it would leave NVCM's NATS users and the deployed NetBox
    disagreeing until both are rolled.

    Note this has to be given the *generated* config, not the base profile — the
    profile never carries a natsPassword, so passing it here always reads "" and
    every run mints a new one.
    """
    k8s = (config.get("secrets") or {}).get("k8s") or {}
    nats_values = (k8s.get("nats") or {}).get("values") or {}
    if password := nats_values.get("password"):
        return password

    # Configs written before NVCM separated NATS from the built-in provider use
    # the legacy location. Keep reading it so an upgrade does not rotate a live
    # broker password unexpectedly.
    legacy_values = (k8s.get("nautobot") or {}).get("values") or {}
    return legacy_values.get("natsPassword") or ""


def _existing_api_token(config_path: Path) -> str:
    """Reuse the generated API token so an interrupted deployment can resume."""
    try:
        config = yaml.safe_load(config_path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return ""
    values = (((config.get("secrets") or {}).get("k8s") or {}).get("dcim") or {}).get("values")
    token = (values or {}).get("token") or ""
    return token if is_valid_api_token(token) else ""


def _nats_password(config: dict, namespace: str) -> str:
    """The password NetBox must publish with: deployed, then configured, then new.

    A deployed NATS outranks the config file. NVCM's deployer skips any secret
    that already exists (`deployer.py:_apply_secret`, `if exists and not
    recreate_secrets`), so on a redeploy the natsPassword in the install config
    is read, validated, and then quietly not applied -- nats-nv-config-manager
    keeps whatever it was first created with. Writing a different one into the
    NetBox release leaves the publisher authenticating against a password NATS
    rejects, and the only trace is `Authorization Violation` in the NetBox log
    while renders silently never happen.
    """
    deployed = cluster.read_nats_password(namespace)
    if deployed:
        if not is_valid_nats_password(deployed):
            raise click.ClickException(
                f"The deployed NATS password in {cluster.NVCM_NATS_SECRET} does not match "
                "NVCM's own rule ([A-Za-z][A-Za-z0-9]{15,}), so it cannot be written back "
                "into the install config. Redeploy NVCM with --recreate-secrets, or delete "
                "the secret and let this run mint a new one:\n"
                f"    kubectl delete secret -n {namespace} {cluster.NVCM_NATS_SECRET}"
            )
        return deployed
    return _existing_nats_password(config) or generate_nats_password()


def _nvcm_repo_candidate(nvcm_repo: Path | None) -> tuple[Path, str]:
    """Return the NVCM checkout path to try, and where that choice came from.

    Precedence: --nvcm-repo, then $NVCM_REPO, then the sibling default. The
    source is carried so errors can say which one produced the path.
    """
    if nvcm_repo is not None:
        return nvcm_repo.expanduser().resolve(), "--nvcm-repo"
    if os.environ.get(NVCM_REPO_ENV):
        return Path(os.environ[NVCM_REPO_ENV]).expanduser().resolve(), f"${NVCM_REPO_ENV}"
    return DEFAULT_NVCM_REPO.expanduser().resolve(), "the default"


def _resolve_nvcm_repo(nvcm_repo: Path | None) -> Path:
    """Locate and validate the NVCM checkout.

    Errors name the absolute path that was tried and where it came from; a
    relative default resolved against the wrong working directory is otherwise
    invisible.
    """
    resolved, source = _nvcm_repo_candidate(nvcm_repo)
    hint = (
        f"Pass --nvcm-repo, or set {NVCM_REPO_ENV} to the checkout's path.\n"
        "NVCM's Helm chart and images are not published anywhere, so a checkout "
        "is required."
    )
    if not resolved.exists():
        raise click.ClickException(f"No such directory: {resolved}\n(from {source})\n{hint}")
    if not (resolved / "installer" / "pyproject.toml").is_file():
        raise click.ClickException(
            f"{resolved} is not an nv-config-manager checkout: no installer/pyproject.toml.\n"
            f"(from {source})\n{hint}"
        )
    if not (resolved / "deploy" / "helm" / "Chart.yaml").is_file():
        raise click.ClickException(
            f"{resolved} has no deploy/helm/Chart.yaml, so the NetBox deploy cannot "
            f"reference the NVCM chart.\n(from {source})"
        )
    return resolved


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
    """Deploy NVIDIA Config Manager backed by NetBox."""


# Registered eagerly, but its NVCM imports happen inside the command, so the kind
# path works without the `air` extra installed.
main.add_command(air_cli.air)


# Commands that are theirs, forwarded verbatim. Their `deploy` alone has 18
# options and they live on a force-pushed branch, so the flags are passed through
# rather than re-declared here: nothing to keep in sync when they add one.
_PASSTHROUGH = {"ignore_unknown_options": True}


def _nvcm_repo_option(fn):
    return click.option(
        "--nvcm-repo",
        type=click.Path(path_type=Path),
        default=None,
        help=f"Path to the nv-config-manager checkout (or set ${NVCM_REPO_ENV}).",
    )(fn)


def _passthrough(subcommand: str, config_path: Path, extra: tuple[str, ...], repo: Path) -> None:
    """Run one of their subcommands against *config_path*.

    The path is resolved because the subprocess runs from their checkout, where a
    relative one would point at their tree instead of ours.
    """
    stream(_nvcm_installer_argv(repo, subcommand, str(config_path.resolve()), *extra))


@main.command(context_settings=_PASSTHROUGH)
@click.argument("config_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.argument("extra", nargs=-1, type=click.UNPROCESSED)
@_nvcm_repo_option
def validate(config_path: Path, extra: tuple[str, ...], nvcm_repo: Path | None) -> None:
    """Validate an install config. Same as NVCM's `validate`."""
    _passthrough("validate", config_path, extra, _resolve_nvcm_repo(nvcm_repo))


@main.command("generate-values", context_settings=_PASSTHROUGH)
@click.argument("config_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.argument("extra", nargs=-1, type=click.UNPROCESSED)
@_nvcm_repo_option
def generate_values(config_path: Path, extra: tuple[str, ...], nvcm_repo: Path | None) -> None:
    """Generate Helm values from a config. Same as NVCM's `generate-values`.

    Note that `--output-dir` is theirs, so a relative one resolves against the
    NVCM checkout. Pass an absolute path to write elsewhere.
    """
    _passthrough("generate-values", config_path, extra, _resolve_nvcm_repo(nvcm_repo))


@main.command(context_settings=_PASSTHROUGH)
@click.argument("config_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.argument("extra", nargs=-1, type=click.UNPROCESSED)
@_nvcm_repo_option
def deploy(config_path: Path, extra: tuple[str, ...], nvcm_repo: Path | None) -> None:
    """Deploy NVCM against an existing NetBox. Same as NVCM's `deploy`.

    This deploys NVCM only, exactly as theirs does. NetBox has to exist first,
    because it owns the API token NVCM authenticates with; `up` sequences both.
    """
    repo = _resolve_nvcm_repo(nvcm_repo)
    _warn_if_no_netbox(config_path)
    _passthrough("deploy", config_path, extra, repo)


def _warn_if_no_netbox(config_path: Path) -> None:
    """Warn when the config points at an in-cluster NetBox that is not there.

    A warning rather than an error: pointing at a NetBox outside the cluster is
    legitimate, and this cannot see one.
    """
    try:
        config = yaml.safe_load(config_path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return
    server = str((config.get("dcim") or {}).get("server") or "")
    namespace = str((config.get("cluster") or {}).get("namespace") or DEFAULT_NAMESPACE)
    # Only in-cluster service URLs are checkable, and only those are ours to set up.
    if not server.startswith(f"http://{cluster.NETBOX_SERVICE}"):
        return
    if cluster.netbox_service_exists(namespace):
        return
    _warn(
        f"No {cluster.NETBOX_SERVICE} service in {namespace}, but dcim.server is {server}.\n"
        "      NVCM will deploy and fail to reach its DCIM. Run `nvcm-netbox up` to "
        "bring NetBox up first."
    )


@main.command(context_settings=_PASSTHROUGH)
@click.option(
    "--config",
    "-c",
    "config_path",
    type=click.Path(path_type=Path),
    default=Path("generated/nv-config-manager-install.yaml"),
    show_default=True,
    help="Config to create or edit.",
)
@click.argument("extra", nargs=-1, type=click.UNPROCESSED)
@_nvcm_repo_option
def init(config_path: Path, extra: tuple[str, ...], nvcm_repo: Path | None) -> None:
    """Edit an install config in NVCM's TUI. Same as NVCM's `init`.

    Their wizard has fields for the DCIM provider and endpoint but none for the
    provider wheelhouse image or the API token, so those are written here before
    it opens and re-applied after it closes. Their schema carries both, so
    anything it does not show still survives the round trip.
    """
    repo = _resolve_nvcm_repo(nvcm_repo)
    resolved = config_path.resolve()

    if resolved.exists():
        config = yaml.safe_load(resolved.read_text()) or {}
        token = (((config.get("secrets") or {}).get("k8s") or {}).get("dcim") or {}).get(
            "values", {}
        ).get("token") or generate_api_token()
    else:
        config, token = cfg.load_profile(), generate_api_token()

    options = _config_options(
        config,
        api_token=token,
        namespace=DEFAULT_NAMESPACE,
        size="small",
        nats_password=_nats_password(config, DEFAULT_NAMESPACE),
    )
    cfg.write_config(cfg.apply_options(config, options), resolved)

    stream(_nvcm_installer_argv(repo, "init", "--config", str(resolved), *extra))

    # Re-apply, in case the wizard cleared something it has no field for.
    edited = yaml.safe_load(resolved.read_text()) or {}
    cfg.write_config(cfg.apply_dcim(edited, options), resolved)

    click.echo(f"\nWrote {resolved}")
    click.echo(
        "The API token in it has to be the one NetBox is installed with. "
        "`nvcm-netbox up` does that;\nif you install NetBox yourself, pass "
        "`--token` to match."
    )


@main.command("config")
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    default=Path("generated/nv-config-manager-install.yaml"),
    show_default=True,
    help="Where to write the installer config.",
)
@click.option("--token", default=None, help="NetBox v2 API token to use. Generated if omitted.")
@click.option("--namespace", default=DEFAULT_NAMESPACE, show_default=True)
@click.option(
    "--size",
    type=click.Choice(["small", "medium", "large"]),
    default="small",
    show_default=True,
    help="NVCM resource profile (cluster.size).",
)
@click.option(
    "--provider-image", default=None, help="Provider wheelhouse image (default: from the profile)."
)
@click.option(
    "--profile",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Base installer profile to start from.",
)
@click.option(
    "--public-url", default=None, help="Browser-facing NetBox URL for NVCM's device links."
)
def config_command(
    output: Path,
    token: str | None,
    namespace: str,
    size: str,
    provider_image: str | None,
    profile: Path | None,
    public_url: str | None,
) -> None:
    """Write an nv-config-manager-install.yaml for an external-NetBox deploy.

    Useful on its own if you would rather drive NVCM's installer (or its TUI)
    yourself. The generated file is exactly what `up` feeds them.
    """
    base = cfg.load_profile(profile)
    token = token or generate_api_token()
    if not is_valid_api_token(token):
        raise click.ClickException(
            "--token must be a NetBox v2 token: nbt_<12 chars>.<40 chars>. "
            "The plugin silently skips installing a malformed token."
        )

    # Any natsPassword this command wrote on a previous run lives in `output`, not
    # in the profile -- reading the profile for it always came up empty, so every
    # invocation minted a new password. With NVCM not yet deployed there is no
    # secret to adopt either, and the mismatch only shows up much later as
    # `Authorization Violation` in the NetBox log with renders silently not
    # happening. `up` avoids this by resolving the config it just wrote.
    previous: dict = {}
    if output.is_file():
        previous = yaml.safe_load(output.read_text()) or {}

    options = _config_options(
        base,
        api_token=token,
        namespace=namespace,
        size=size,
        provider_image=provider_image,
        public_url=public_url,
        nats_password=_nats_password(previous, namespace),
    )
    written = cfg.write_config(cfg.apply_options(base, options), output)
    click.echo(f"Wrote {written}")
    click.secho(f"API token: {token}", fg="yellow")
    click.echo("Keep it — NetBox and NVCM must both be given this same value.")


def _up_options(fn):
    """Options shared by `up` and `netbox up`, which run the same steps.

    Declared once so the two cannot drift; `netbox up` is `up` stopping after
    NetBox.
    """
    options = [
        click.option(
            "--nvcm-repo",
            type=click.Path(path_type=Path),
            default=None,
            help=f"Path to an nv-config-manager checkout. Defaults to ${NVCM_REPO_ENV}, "
            f"then {DEFAULT_NVCM_REPO}.",
        ),
        click.option("--namespace", default=DEFAULT_NAMESPACE, show_default=True),
        click.option(
            "--release",
            default=DEFAULT_NETBOX_RELEASE,
            show_default=True,
            help="Helm release name for NetBox.",
        ),
        click.option(
            "--chart",
            type=click.Path(exists=True, file_okay=False, path_type=Path),
            default=DEFAULT_CHART,
            show_default=True,
        ),
        click.option(
            "--size",
            type=click.Choice(["small", "medium", "large"]),
            default="small",
            show_default=True,
        ),
        click.option("--token", default=None, help="NetBox v2 API token. Generated if omitted."),
        click.option("--netbox-image", default="nv-config-manager-netbox", show_default=True),
        click.option("--netbox-tag", default=None, help="NetBox image tag (default: the chart's)."),
        click.option("--provider-image", default=None, help="Provider wheelhouse image ref."),
        click.option(
            "--config-out",
            type=click.Path(path_type=Path),
            default=Path("generated/nv-config-manager-install.yaml"),
            show_default=True,
        ),
        click.option("--kind-cluster", default="nv-config-manager", show_default=True),
        click.option(
            "--create-cluster/--no-create-cluster",
            default=True,
            show_default=True,
            help="Create the kind cluster if it does not exist, using NVCM's kind-config.yaml.",
        ),
        click.option(
            "--kind-load/--no-kind-load",
            default=True,
            show_default=True,
            help="Load the locally-built images into the kind node. Disable for a real cluster.",
        ),
    ]
    for option in reversed(options):
        fn = option(fn)
    return fn


@main.group("netbox")
def netbox_group() -> None:
    """Manage the NetBox side on its own."""


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
        repo = _resolve_nvcm_repo(nvcm_repo)
        path = repo / "development" / "mock_topology" / "context" / blueprint
        if not path.is_dir():
            available = sorted(
                p.name
                for p in (repo / "development" / "mock_topology" / "context").glob("*")
                if p.is_dir() and not p.name.startswith("_")
            )
            raise click.ClickException(
                f"No blueprint at {path}.\nAvailable: {', '.join(available) or 'none'}"
            )

    click.echo(f"Loading {path.name} into NetBox ...")
    output = cluster.load_topology(
        str(path), namespace, deployment_name=deployment_name or path.name
    )
    click.echo(output.strip() or "done")
    click.secho(f"Loaded {path.name}.", fg="green")


@netbox_group.command("up")
@_up_options
@click.pass_context
def netbox_up(ctx: click.Context, **kwargs) -> None:
    """Deploy NetBox and mint the API token, without touching NVCM.

    The first half of `up`, and the step NVCM's installer has no equivalent for.
    Run it before `deploy`: NVCM authenticates to NetBox with the token this
    creates, so NetBox has to exist first.
    """
    ctx.invoke(up, skip_nvcm=True, skip_verify=True, **kwargs)


@main.command()
@_up_options
@click.option("--skip-nvcm", is_flag=True, help="Deploy NetBox only; skip the NVCM installer.")
@click.option("--skip-verify", is_flag=True, help="Do not run the event-loop check at the end.")
def up(
    nvcm_repo: Path | None,
    namespace: str,
    release: str,
    chart: Path,
    size: str,
    token: str | None,
    netbox_image: str,
    netbox_tag: str | None,
    provider_image: str | None,
    config_out: Path,
    skip_nvcm: bool,
    skip_verify: bool,
    kind_cluster: str,
    create_cluster: bool,
    kind_load: bool,
) -> None:
    """Bring up NetBox + NVCM on the current cluster.

    Order matters: NetBox is deployed first so it owns the API token NVCM
    authenticates with, then NVCM. NetBox is installed with the NATS password
    already set, so it does not have to be rolled again to start publishing; the
    one thing that does need a second helm pass is its HTTPRoute, which cannot
    render until NVCM's deploy has put the Gateway API CRDs on the cluster.
    """
    require_tools("kubectl", "helm")
    total = 7 if skip_nvcm else 4
    step = 0

    repo = None if skip_nvcm else _resolve_nvcm_repo(nvcm_repo)

    token = token or _existing_api_token(config_out) or generate_api_token()
    if not is_valid_api_token(token):
        raise click.ClickException("--token must be a NetBox v2 token: nbt_<12>.<40>.")

    # Both refs resolved up front: the same values are loaded into the node and
    # written into NVCM's config, so they cannot disagree.
    profile = cfg.load_profile()
    provider_ref = _config_options(
        profile, api_token=token, namespace=namespace, size=size, provider_image=provider_image
    ).provider_image
    netbox_ref = f"{netbox_image}:{netbox_tag or DEFAULT_NETBOX_TAG}"

    step += 1
    _step(step, total, f"Checking kind cluster {kind_cluster}")
    if cluster.kind_cluster_exists(kind_cluster):
        _ok("already running")
    elif not create_cluster:
        # Not an error: `kind_cluster_exists` is false for any cluster kind did not
        # create (and whenever kind is not installed at all), so treating it as
        # fatal made --no-create-cluster refuse to deploy onto a real cluster --
        # which is exactly what --no-kind-load exists to support.
        _ok(f"no kind cluster {kind_cluster!r}; deploying to the current kubectl context")
    else:
        require_tools("kind")
        kind_config = _nvcm_repo_candidate(nvcm_repo)[0] / "deploy" / "kind-config.yaml"
        if not kind_config.is_file():
            raise click.ClickException(
                f"kind cluster {kind_cluster!r} does not exist, and NVCM's cluster config "
                f"is not at {kind_config}.\nCreate the cluster yourself, or point "
                f"--nvcm-repo / ${NVCM_REPO_ENV} at the checkout."
            )
        cluster.create_kind_cluster(kind_cluster, kind_config)
        _ok(f"created from {kind_config.name}")

    # Resolved after the cluster exists, so an already-deployed NATS can be read.
    # Supplying it in the install config means NetBox can be given the password at
    # install time, so the release does not have to be upgraded again afterwards
    # just to switch its publisher from `log` to NATS.
    nats_password = _nats_password(profile, namespace)

    if skip_nvcm:
        step += 1
        _step(step, total, "Fetching chart dependencies")
        cluster.helm_dependency_build(chart)

    step += 1
    if kind_load:
        _step(step, total, f"Loading images into kind cluster {kind_cluster}")
        require_tools("kind")
        cluster.check_image_arch([netbox_ref, provider_ref], kind_cluster)
        cluster.load_images_into_kind([netbox_ref, provider_ref], kind_cluster)
        _ok("images available to the node")
    else:
        _step(step, total, "Skipping kind image load (--no-kind-load)")

    if skip_nvcm:
        step += 1
        _step(step, total, f"Deploying NetBox to namespace {namespace}")
        stuck = cluster.pending_release_status(release, namespace)
        if stuck:
            raise click.ClickException(
                f"The {release!r} release is stuck in {stuck}, usually because an earlier run "
                f"was interrupted mid-install. Clear it with:\n"
                f"    helm uninstall {release} -n {namespace}\n"
                "(or `helm rollback` if there is a completed revision to return to)."
            )
        stale = cluster.orphaned_data_volumes(release, namespace)
        if stale:
            raise click.ClickException(
                "These volumes hold databases whose passwords are gone, so NetBox would not "
                f"be able to log in to them:\n    {' '.join(stale)}\n"
                "Delete them and run this again (the data on them is not recoverable):\n"
                f"    kubectl delete pvc -n {namespace} {' '.join(stale)}"
            )
        cluster.install_netbox(
            str(chart),
            release=release,
            namespace=namespace,
            api_token=token,
            image_repository=netbox_image,
            image_tag=netbox_tag or DEFAULT_NETBOX_TAG,
            nats_password=nats_password,
        )
        cluster.wait_for_netbox(namespace)
        _ok("NetBox rolled out")

        step += 1
        _step(step, total, "Initialising NetBox (token, custom fields, bootstrap data)")
        output = cluster.run_nvcm_init(namespace, token)
        _ok(output.strip().splitlines()[-1] if output.strip() else "nvcm_init complete")

    step += 1
    _step(step, total, "Generating the NVCM installer config")
    options = _config_options(
        profile,
        api_token=token,
        namespace=namespace,
        size=size,
        provider_image=provider_ref,
        nats_password=nats_password,
    )
    config = cfg.apply_options(profile, options)
    # Keep the generated file usable by later standalone installer commands.
    written = cfg.write_config(config, config_out).resolve()
    _ok(f"{written}")

    if skip_nvcm:
        step += 1
        _step(step, total, "NetBox is up; NVCM was not deployed")
        click.echo(
            f"\nNetBox holds the API token in {written}. Deploy NVCM against it with:\n"
            f"  nvcm-netbox deploy {written} \\\n"
            f"    --image-source local --build-images --load-kind "
            f"--kind-cluster {kind_cluster} \\\n"
            "    --install-envoy-gateway --install-cnpg-operator --install-cert-manager\n"
            "\nThen `nvcm-netbox verify` to check the change-event loop."
        )
        return

    assert repo is not None
    install_config = NVConfigManagerInstallConfig.model_validate(config)
    install_config.images.source = ImageSource.LOCAL
    deployer = NetBoxDeployer(
        install_config,
        DeployOptions(
            project_root=repo,
            chart_dir=str(repo / "deploy" / "helm"),
            build_images=True,
            load_kind=kind_load,
            kind_cluster=kind_cluster,
            install_envoy_gateway=True,
            install_cnpg_operator=True,
            install_cert_manager=True,
        ),
        NetBoxDeploymentSettings(
            chart=chart.resolve(),
            release=release,
            image_repository=netbox_image,
            image_tag=netbox_tag or DEFAULT_NETBOX_TAG,
        ),
        _ClickDeployCallback(),
    )
    if not deployer.run():
        raise click.ClickException("NVCM + NetBox deployment failed; see the step output above.")

    step += 1
    if skip_verify:
        _step(step, total, "Skipping verification (--skip-verify)")
    else:
        _step(step, total, "Verifying the change-event loop")
        ctx = click.get_current_context()
        ctx.invoke(verify, namespace=namespace, device_id="1")

    click.secho("\nNVCM + NetBox is up.", fg="green", bold=True)
    click.echo(
        f"  NetBox:  kubectl -n {namespace} port-forward svc/{cluster.NETBOX_SERVICE} 8080:80"
    )
    click.echo(f"  Token:   {token}")
    click.echo(f"  Config:  {written}")


@main.command()
@click.option("--namespace", default=DEFAULT_NAMESPACE, show_default=True)
@click.option(
    "--device-id",
    default="1",
    show_default=True,
    help="NetBox device PK to reference in the synthetic event.",
)
def verify(namespace: str, device_id: str) -> None:
    """Publish a synthetic change event and check the DCIM consumer handles it.

    Proves NetBox -> NATS -> NVCM consumer -> our provider's render handlers,
    which is the part of the stack that silently does nothing when the subject,
    stream or provider name disagree.
    """
    require_tools("kubectl")
    nats_password = cluster.read_nats_password(namespace)
    if not nats_password:
        raise click.ClickException(
            f"No {cluster.NVCM_NATS_SECRET} secret in {namespace} — NVCM does not look deployed."
        )

    # This command publishes its own envelope, so it never exercises NetBox's
    # publisher. Check that separately or a broken one looks like a pass.
    backend, importable = cluster.netbox_publisher_status(namespace)
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
            "      NVCM. That means the release was installed with no nvcm.natsPassword —\n"
            f"      `up` sets it on install. Switch it over with:\n"
            f"      helm upgrade {DEFAULT_NETBOX_RELEASE} {DEFAULT_CHART} -n {namespace} "
            "--reuse-values --set nvcm.natsPassword=<pw>"
        )

    subject = cfg.event_subject(cfg.load_profile())
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
            "Check that dcim.event_subject matches the plugin's nats_subject, and that\n"
            "the provider wheelhouse image installed (dcim.provider_packages)."
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
