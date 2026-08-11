# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""`nvcm-netbox` — bring up NVIDIA Config Manager with NetBox as its DCIM provider."""

from __future__ import annotations

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


def _resolve_nvcm_repo(nvcm_repo: Path) -> Path:
    resolved = nvcm_repo.expanduser().resolve()
    if not (resolved / "installer" / "pyproject.toml").is_file():
        raise click.ClickException(
            f"{resolved} does not look like an nv-config-manager checkout "
            "(no installer/pyproject.toml).\n"
            "NVCM's Helm chart and images are not published anywhere, so a checkout "
            "is required. Pass --nvcm-repo."
        )
    if not (resolved / "deploy" / "helm" / "Chart.yaml").is_file():
        raise click.ClickException(f"{resolved} has no deploy/helm chart.")
    return resolved


@click.group()
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

    defaults = cfg.ConfigOptions(api_token=token)
    options = cfg.ConfigOptions(
        api_token=token,
        namespace=namespace,
        size=size,
        provider_image=provider_image
        or (base.get("dcim", {}).get("provider_packages") or [{}])[0].get("image")
        or defaults.provider_image,
        netbox_public_url=public_url
        or base.get("dcim", {}).get("public_url")
        or defaults.netbox_public_url,
    )
    written = cfg.write_config(cfg.apply_options(base, options), output)
    click.echo(f"Wrote {written}")
    click.secho(f"API token: {token}", fg="yellow")
    click.echo("Keep it — NetBox and NVCM must both be given this same value.")


@main.command()
@click.option(
    "--nvcm-repo",
    type=click.Path(path_type=Path),
    default=Path("../nv-config-manager"),
    show_default=True,
    help="Path to an nv-config-manager checkout.",
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
def up(
    nvcm_repo: Path,
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
) -> None:
    """Bring up NetBox + NVCM on the current cluster.

    Order matters: NetBox is deployed first so it owns the API token NVCM
    authenticates with, then NVCM, then NetBox is rolled once more to pick up the
    NATS password NVCM's deployer created.
    """
    require_tools("kubectl", "helm")
    total = 5 if skip_nvcm else 8
    step = 0

    repo = None if skip_nvcm else _resolve_nvcm_repo(nvcm_repo)
    if not skip_nvcm:
        require_tools("uv")

    token = token or generate_api_token()
    if not is_valid_api_token(token):
        raise click.ClickException("--token must be a NetBox v2 token: nbt_<12>.<40>.")

    step += 1
    _step(step, total, "Fetching chart dependencies")
    cluster.helm_dependency_build(chart)

    step += 1
    _step(step, total, f"Deploying NetBox to namespace {namespace}")
    # No NATS password yet: NVCM does not exist, so the publisher comes up on the
    # `log` backend. It is switched to NATS at the end.
    cluster.install_netbox(
        str(chart),
        release=release,
        namespace=namespace,
        api_token=token,
        image_repository=netbox_image,
        image_tag=netbox_tag or "",
    )
    cluster.wait_for_netbox(namespace)
    _ok("NetBox rolled out")

    step += 1
    _step(step, total, "Initialising NetBox (token, custom fields, bootstrap data)")
    output = cluster.run_nvcm_init(namespace, token)
    _ok(output.strip().splitlines()[-1] if output.strip() else "nvcm_init complete")

    step += 1
    _step(step, total, "Generating the NVCM installer config")
    base = cfg.load_profile()
    options = cfg.ConfigOptions(
        api_token=token,
        namespace=namespace,
        size=size,
        provider_image=provider_image
        or (base.get("dcim", {}).get("provider_packages") or [{}])[0].get("image")
        or cfg.ConfigOptions(api_token=token).provider_image,
    )
    config = cfg.apply_options(base, options)
    written = cfg.write_config(config, config_out)
    _ok(f"{written}")

    if skip_nvcm:
        step += 1
        _step(step, total, "Skipping NVCM (--skip-nvcm)")
        manual = " ".join(_nvcm_installer_argv(Path(nvcm_repo), "deploy", str(written)))
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
            image_tag=netbox_tag or "",
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
