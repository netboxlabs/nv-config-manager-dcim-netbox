# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""`nvcm-netbox` — bring up NVIDIA Config Manager with NetBox as its DCIM provider."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import click

from nv_config_manager_dcim_netbox.installer import cluster
from nv_config_manager_dcim_netbox.installer import config as cfg
from nv_config_manager_dcim_netbox.installer.air import cli as air_cli
from nv_config_manager_dcim_netbox.installer.shell import CommandError, require_tools, stream
from nv_config_manager_dcim_netbox.installer.tokens import generate_api_token, is_valid_api_token

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CHART = _REPO_ROOT / "deploy" / "netbox"
DEFAULT_NAMESPACE = "nv-config-manager"
DEFAULT_NETBOX_RELEASE = "netbox"
# Matches netbox.image.tag in deploy/netbox/values.yaml.
DEFAULT_NETBOX_TAG = "0.1.0"

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


def _nvcm_installer_argv(nvcm_repo: Path, *args: str) -> list[str]:
    """Build a `uv run` invocation of NVCM's installer CLI.

    We shell out rather than importing nv_config_manager_installer. Their
    Deployer.run() is a hardcoded call sequence in a very large module on a
    force-pushed branch; the CLI and the config-file schema are the stable
    surface, so those are the only things we depend on.
    """
    return [
        "uv",
        "run",
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
    )


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

    options = _config_options(
        base,
        api_token=token,
        namespace=namespace,
        size=size,
        provider_image=provider_image,
        public_url=public_url,
    )
    written = cfg.write_config(cfg.apply_options(base, options), output)
    click.echo(f"Wrote {written}")
    click.secho(f"API token: {token}", fg="yellow")
    click.echo("Keep it — NetBox and NVCM must both be given this same value.")


@main.command()
@click.option(
    "--nvcm-repo",
    type=click.Path(path_type=Path),
    default=None,
    help=f"Path to an nv-config-manager checkout. Defaults to ${NVCM_REPO_ENV}, "
    f"then {DEFAULT_NVCM_REPO}.",
)
@click.option("--namespace", default=DEFAULT_NAMESPACE, show_default=True)
@click.option(
    "--release",
    default=DEFAULT_NETBOX_RELEASE,
    show_default=True,
    help="Helm release name for NetBox.",
)
@click.option(
    "--chart",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=DEFAULT_CHART,
    show_default=True,
)
@click.option(
    "--size", type=click.Choice(["small", "medium", "large"]), default="small", show_default=True
)
@click.option("--token", default=None, help="NetBox v2 API token. Generated if omitted.")
@click.option("--netbox-image", default="nv-config-manager-netbox", show_default=True)
@click.option("--netbox-tag", default=None, help="NetBox image tag (default: the chart's).")
@click.option("--provider-image", default=None, help="Provider wheelhouse image ref.")
@click.option(
    "--config-out",
    type=click.Path(path_type=Path),
    default=Path("generated/nv-config-manager-install.yaml"),
    show_default=True,
)
@click.option("--skip-nvcm", is_flag=True, help="Deploy NetBox only; skip the NVCM installer.")
@click.option("--skip-verify", is_flag=True, help="Do not run the event-loop check at the end.")
@click.option("--kind-cluster", default="nv-config-manager", show_default=True)
@click.option(
    "--create-cluster/--no-create-cluster",
    default=True,
    show_default=True,
    help="Create the kind cluster if it does not exist, using NVCM's kind-config.yaml.",
)
@click.option(
    "--kind-load/--no-kind-load",
    default=True,
    show_default=True,
    help="Load the locally-built images into the kind node. Disable for a real cluster.",
)
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
    authenticates with, then NVCM, then NetBox is rolled once more to pick up the
    NATS password NVCM's deployer created.
    """
    require_tools("kubectl", "helm")
    total = 7 if skip_nvcm else 10
    step = 0

    repo = None if skip_nvcm else _resolve_nvcm_repo(nvcm_repo)
    if not skip_nvcm:
        require_tools("uv")

    token = token or generate_api_token()
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
        raise click.ClickException(
            f"kind cluster {kind_cluster!r} does not exist and --no-create-cluster was given."
        )
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

    step += 1
    _step(step, total, "Fetching chart dependencies")
    cluster.helm_dependency_build(chart)

    step += 1
    if kind_load:
        _step(step, total, f"Loading images into kind cluster {kind_cluster}")
        require_tools("kind")
        cluster.load_images_into_kind([netbox_ref, provider_ref], kind_cluster)
        _ok("images available to the node")
    else:
        _step(step, total, "Skipping kind image load (--no-kind-load)")

    step += 1
    _step(step, total, f"Deploying NetBox to namespace {namespace}")
    stuck = cluster.pending_release_status(release, namespace)
    if stuck:
        raise click.ClickException(
            f"The {release!r} release is stuck in {stuck}, usually because an earlier run was "
            f"interrupted mid-install. helm refuses to proceed until it is cleared:\n"
            f"    helm uninstall {release} -n {namespace}\n"
            "(or `helm rollback` if there is a completed revision to return to)."
        )
    # No NATS password yet: NVCM does not exist, so the publisher comes up on the
    # `log` backend. It is switched to NATS at the end.
    cluster.install_netbox(
        str(chart),
        release=release,
        namespace=namespace,
        api_token=token,
        image_repository=netbox_image,
        image_tag=netbox_tag or DEFAULT_NETBOX_TAG,
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
        profile, api_token=token, namespace=namespace, size=size, provider_image=provider_ref
    )
    config = cfg.apply_options(profile, options)
    written = cfg.write_config(config, config_out)
    _ok(f"{written}")

    if skip_nvcm:
        step += 1
        _step(step, total, "Skipping NVCM (--skip-nvcm)")
        manual = " ".join(
            _nvcm_installer_argv(_nvcm_repo_candidate(nvcm_repo)[0], "deploy", str(written))
        )
        click.echo(f"\nDeploy NVCM yourself with:\n  {manual}")
        return

    assert repo is not None
    step += 1
    _step(step, total, "Validating the config against NVCM's installer")
    stream(_nvcm_installer_argv(repo, "validate", str(written)))

    step += 1
    _step(step, total, "Deploying NVCM (this takes a while)")
    stream(
        _nvcm_installer_argv(
            repo,
            "deploy",
            str(written),
            "--image-source",
            "local",
            "--build-images",
            "--load-kind",
            "--kind-cluster",
            kind_cluster,
            "--install-envoy-gateway",
            "--install-cnpg-operator",
            "--install-cert-manager",
        )
    )

    step += 1
    _step(step, total, "Connecting NetBox to the NVCM event bus")
    nats_password = cluster.read_nats_password(namespace)
    if not nats_password:
        _warn(
            f"No {cluster.NVCM_NATS_SECRET} secret found; leaving the publisher on the "
            "`log` backend. Re-run once NVCM is healthy."
        )
    else:
        cluster.install_netbox(
            str(chart),
            release=release,
            namespace=namespace,
            api_token=token,
            image_repository=netbox_image,
            image_tag=netbox_tag or DEFAULT_NETBOX_TAG,
            nats_password=nats_password,
        )
        _ok("NetBox publishing to NATS")

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

    subject = cfg.event_subject(cfg.load_profile())
    click.echo(f"Publishing a synthetic dcim.device event to {subject} ...")
    try:
        cluster.publish_test_event(namespace, subject, nats_password, device_id)
    except CommandError as exc:
        raise click.ClickException(f"Could not publish to NATS:\n{exc.output}") from exc

    logs = cluster.dcim_consumer_logs(namespace)
    hit = [
        line
        for line in logs.splitlines()
        if any(m in line for m in ("batch queue operation", "queued", "not enabled"))
    ]
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
    if not any("queued" in line and "0 queued" not in line for line in hit):
        click.echo(
            "\nNote: '0 queued ... not enabled' still proves the full path. To get a real\n"
            "render, mark the device render-enabled via the plugin's DeviceConfigStatus."
        )


if __name__ == "__main__":  # pragma: no cover
    main()
