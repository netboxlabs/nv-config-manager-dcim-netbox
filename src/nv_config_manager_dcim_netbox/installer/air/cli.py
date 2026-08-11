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


@click.group()
def air() -> None:
    """Create a DSX Air simulation using NetBox as the DCIM provider."""


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
