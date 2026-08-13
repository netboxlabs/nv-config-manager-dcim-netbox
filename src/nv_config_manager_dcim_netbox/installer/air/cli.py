# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""`nvcm-netbox air` — DSX Air simulations backed by NetBox."""

from __future__ import annotations

from pathlib import Path

import click

from nv_config_manager_dcim_netbox.installer.air import AirSupportUnavailable


class ConsoleCallback:
    """Minimal OrchestratorCallback that just reports to the terminal."""

    def on_step(self, step_id, status, message: str = "") -> None:
        name = getattr(status, "name", str(status))
        colour = {"RUNNING": "cyan", "SUCCESS": "green", "FAILED": "red"}.get(name, "yellow")
        click.secho(
            f"[{name.lower():8}] {step_id}" + (f" — {message}" if message else ""), fg=colour
        )

    def on_log(self, line: str) -> None:
        click.echo(f"  {line}")

    def on_ssh_ready(self, host: str, port: int) -> None:
        click.secho(f"SSH ready: nvcm@{host}:{port}", fg="green")

    def on_deploy_started(self, host: str, port: int) -> None:
        click.secho("Deploying NVCM (this takes a while)...", fg="cyan", bold=True)

    def on_complete(self, success: bool, host: str = "", port: int = 0) -> None:
        if success:
            click.secho(f"\nSimulation ready: nvcm@{host}:{port}", fg="green", bold=True)
        else:
            click.secho("\nSimulation failed.", fg="red", bold=True)


# Keys a sim needs chosen rather than defaulted, in the order they are written.
_REQUIRED_KEYS = ("mock_blueprint", "deployment_name", "simulation_name", "size", "ngc_api_key")

# The NetBox keys worth showing, commented, so their current values are visible
# without reading the source. Rendered from the dataclass, never hand-written, so
# they cannot drift from the defaults they document.
_SHOWN_DEFAULTS = (
    "netbox_image",
    "netbox_image_tag",
    "dcim_provider_package_image",
    "dcim_server",
    "dcim_token",
    "load_blueprint",
)


def _render_example(blueprint: str, name: str) -> str:
    """Build an example sim config from NetBoxSimConfig's own defaults.

    Generated rather than shipped as a file: every value here is a default that
    changes with the package (image tags) or with NVCM's branching
    (config_manager_ref), and a checked-in copy goes stale silently.
    """
    import dataclasses

    from nv_config_manager_dcim_netbox.installer.air.config import NetBoxSimConfig

    defaults = {f.name: f for f in dataclasses.fields(NetBoxSimConfig)}

    def value(key: str):
        got = defaults[key].default
        return "" if got is dataclasses.MISSING else got

    chosen = {
        "mock_blueprint": blueprint,
        "deployment_name": name,
        "simulation_name": f"nv-config-manager-netbox-{blueprint.replace('_', '-')}",
        "size": value("size") or "small",
        "ngc_api_key": "",
    }

    lines = [
        "# DSX Air simulation with NetBox as the DCIM provider.",
        "#",
        "#   nvcm-netbox air up -c this-file.yaml",
        "#",
        "# Written by `nvcm-netbox air config`. It is NVCM's air-sim YAML; only the",
        "# keys that must be chosen are set, because everything NetBox needs already",
        "# defaults correctly. Do not replace it with NVCM's own",
        "# development/air_sim/configs/air_trial.yaml: that sets config_manager_ref",
        "# explicitly, and run_mock_topology_job, which `air up` forces off anyway.",
        "",
    ]
    for key in _REQUIRED_KEYS:
        got = chosen[key]
        rendered = "''" if got == "" else got
        lines.append(f"{key}: {rendered}")
        if key == "ngc_api_key":
            lines[-1] += "                 # or export NGC_API_KEY"

    lines += ["", "# --- optional, shown at their current defaults ---"]
    for key in _SHOWN_DEFAULTS:
        got = value(key)
        rendered = (
            "''" if got == "" else ("true" if got is True else "false" if got is False else got)
        )
        lines.append(f"# {key}: {rendered}")
    lines.append("")
    return "\n".join(lines)


@click.group()
def air() -> None:
    """Create a DSX Air simulation using NetBox as the DCIM provider."""


@air.command("config")
@click.option(
    "--output",
    "-o",
    "output",
    type=click.Path(path_type=Path),
    default=Path("air-trial.yaml"),
    show_default=True,
    help="Where to write the sim config.",
)
@click.option(
    "--blueprint",
    type=click.Choice(["air_trial", "air_superpod"]),
    default="air_trial",
    show_default=True,
    help="Topology to simulate. air_trial is 7 devices, air_superpod is 24.",
)
@click.option("--name", default="demo", show_default=True, help="Deployment name.")
@click.option("--force", is_flag=True, help="Overwrite an existing file.")
def air_config(output: Path, blueprint: str, name: str, force: bool) -> None:
    """Write a sim config to edit and pass to `air up`.

    The repo ignores the usual working-copy names, because this file takes an
    NGC key and can take a NetBox token.
    """
    try:
        body = _render_example(blueprint, name)
    except AirSupportUnavailable as exc:
        raise click.ClickException(str(exc)) from exc

    if output.exists() and not force:
        raise click.ClickException(f"{output} exists. Pass --force to overwrite.")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(body)
    click.echo(f"Wrote {output}")
    click.echo(f"Set ngc_api_key (or $NGC_API_KEY), then: nvcm-netbox air up -c {output}")


@air.command("up")
@click.option(
    "--config",
    "-c",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Sim config YAML (NVCM's air-sim format plus this provider's dcim_* keys).",
)
@click.option("--token", default=None, help="NetBox v2 API token. Generated if omitted.")
@click.option(
    "--chart",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="NetBox chart directory (defaults to deploy/netbox).",
)
@click.option(
    "--blueprint/--no-blueprint",
    default=None,
    help="Load the sim's topology into NetBox. On by default.",
)
def air_up(
    config_path: Path, token: str | None, chart: Path | None, blueprint: bool | None
) -> None:
    """Create the simulation and deploy NVCM with NetBox onto it.

    Deploys NetBox into the sim's kind cluster, loads the topology into it, and
    points NVCM's install config at it.
    """
    try:
        from nv_config_manager_installer.air_sim.orchestrator import SimOrchestrator

        from nv_config_manager_dcim_netbox.installer.air.callback import NetBoxAirCallback
        from nv_config_manager_dcim_netbox.installer.air.config import NetBoxSimConfig
    except AirSupportUnavailable as exc:
        raise click.ClickException(str(exc)) from exc
    except ImportError as exc:
        raise click.ClickException(str(AirSupportUnavailable(exc))) from exc

    cfg = NetBoxSimConfig.from_yaml(config_path)
    if token:
        cfg.dcim_token = token
    if blueprint is not None:
        cfg.load_blueprint = blueprint

    # The bundled DCIM's topology job would populate a Nautobot that is not being
    # deployed; nvcm_load_dcim puts the same topology into NetBox instead.
    cfg.run_mock_topology_job = False

    callback = NetBoxAirCallback(ConsoleCallback(), cfg, chart=chart)
    click.secho(f"NetBox API token: {callback.api_token}", fg="yellow")

    SimOrchestrator(cfg, callback).run()
