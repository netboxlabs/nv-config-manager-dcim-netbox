# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""`nvcm-netbox air` -- access to a DSX Air simulation backed by NetBox.

Creating and deploying the simulation is `nv-config-manager-installer air-sim`
with `dcim_provider: netbox` in the sim config; this group holds only what the
stock wizard has no counterpart for.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import click
from nv_config_manager_installer.air_sim.constants import CONFIG_MANAGER_HOSTNAME, NVCM_BOX_USER
from nv_config_manager_installer.air_sim.sim_config import SimConfig
from nv_config_manager_installer.air_sim.sim_manager import AirSimulationManager

from nv_config_manager_dcim_netbox.installer.air import tunnel as tunnel_mod
from nv_config_manager_dcim_netbox.installer.settings import NetBoxAirSettings

# Where the sim config lives, matching NVCM's air-sim so their documentation
# applies unchanged: the same default path and the same environment variable.
SIM_CONFIG_ENV = "NVCM_AIR_CONFIG"
# Read by AirSimulationManager when the sim config carries no key.
NGC_API_KEY_ENV = "NGC_API_KEY"
DEFAULT_SIM_CONFIG = Path("~/.nvcm-air-sim.yaml")


def _default_sim_config() -> Path:
    return Path(os.environ.get(SIM_CONFIG_ENV) or DEFAULT_SIM_CONFIG).expanduser()


@click.group()
def air() -> None:
    """Reach a DSX Air simulation that uses NetBox as the DCIM provider."""


# The sim's own hostnames, which resolve only through the tunnel: they live in
# the box's /etc/hosts, where NVCM writes its services and the NetBox hooks add
# NetBox.
_NETBOX_URL_TEMPLATE = "https://{hostname}"


@air.command("tunnel")
@click.option(
    "--config",
    "-c",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=lambda: _default_sim_config(),
    show_default=str(DEFAULT_SIM_CONFIG),
    help=f"Sim config to read. Defaults to ${SIM_CONFIG_ENV}, then {DEFAULT_SIM_CONFIG}.",
)
@click.option(
    "--socks-port",
    default=None,
    type=int,
    help=f"Local SOCKS port. Defaults to {tunnel_mod.DEFAULT_SOCKS_PORT}, matching NVCM's.",
)
@click.option(
    "--print", "print_only", is_flag=True, help="Print the command instead of opening the tunnel."
)
@click.option(
    "--browser", is_flag=True, help="Also launch a Chromium-family browser through the tunnel."
)
@click.option("--url", default=None, help="URL to open with --browser. Defaults to NetBox's.")
def air_tunnel(
    config_path: Path,
    socks_port: int | None,
    print_only: bool,
    browser: bool,
    url: str | None,
) -> None:
    """Open a SOCKS tunnel to the running simulation.

    One tunnel reaches every `*.nvcm.air` service, NetBox included: those names
    resolve only on the sim box, and a SOCKS proxy resolves DNS there rather than
    here.

    AIR allocates the box's SSH host and port per simulation, so they cannot be
    written down; the sim config names the simulation, which AIR can be asked
    about, and holds `oob_ssh_password`. The password is handed to ssh down a
    pipe, so it never appears in a command line, a file, or this output.
    """
    cfg = SimConfig.from_yaml(config_path)
    if not cfg.simulation_name:
        raise click.ClickException(
            f"{config_path} has no simulation_name, so there is nothing to look up.\n"
            "Set it to the name the simulation was created with."
        )
    # $NGC_API_KEY counts: AirSimulationManager falls back to it and NVCM's
    # SimConfig never reads the environment.
    if not (cfg.ngc_api_key or os.environ.get(NGC_API_KEY_ENV)):
        raise click.ClickException(
            f"Looking the simulation up needs an NGC API key. Export ${NGC_API_KEY_ENV}, "
            f"or set ngc_api_key in {config_path}."
        )

    port = socks_port or tunnel_mod.DEFAULT_SOCKS_PORT
    manager = AirSimulationManager(
        ngc_api_key=cfg.ngc_api_key or None,
        org_id=cfg.org_id or None,
        use_internal=cfg.use_internal,
        ssh_password=cfg.oob_ssh_password,
    )
    try:
        endpoint = tunnel_mod.find_ssh_endpoint(manager, cfg.simulation_name, cfg.oob_server_name)
    except tunnel_mod.AmbiguousSim as exc:
        raise click.ClickException(
            f"{exc}\nDelete the ones you are not using, or rename this sim in {config_path}."
        ) from exc
    except tunnel_mod.SimNotFound as exc:
        raise click.ClickException(
            f"No running simulation for {exc}.\n"
            "AIR deletes simulations when they expire, so a sim from an earlier run may "
            "be gone; `nv-config-manager-installer air-sim deploy` creates a new one."
        ) from exc

    hostname = NetBoxAirSettings.model_validate(cfg.dcim_settings).netbox_hostname(
        CONFIG_MANAGER_HOSTNAME
    )
    target = url or _NETBOX_URL_TEMPLATE.format(hostname=hostname)
    if print_only:
        click.echo(tunnel_mod.printable_command(endpoint, port, NVCM_BOX_USER))
        click.echo(f"\nssh will prompt for the password; it is oob_ssh_password in {config_path}.")
        return

    click.secho(f"SOCKS tunnel to nvcm@{endpoint.host}:{endpoint.port}", fg="cyan")
    click.echo(f"  proxy:  socks5://localhost:{port}")
    click.echo(f"  NetBox: {target}")
    proc = tunnel_mod.open_tunnel(
        endpoint, cfg.oob_ssh_password, socks_port=port, user=NVCM_BOX_USER
    )

    if browser:
        argv = tunnel_mod.browser_argv(port, target)
        if argv is None:
            _warn_no_browser(port)
        else:
            # Not waited on: the browser outlives the tunnel command only if the
            # user leaves it open, and killing it here would close their tabs.
            subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    click.echo("Ctrl-C to close the tunnel.")
    try:
        raise SystemExit(proc.wait())
    except KeyboardInterrupt:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        click.echo("\nTunnel closed.")


def _warn_no_browser(socks_port: int) -> None:
    click.secho(
        "No Chromium-family browser found, so --browser did nothing. Point one at\n"
        f"  socks5://localhost:{socks_port}\n"
        "In Firefox also enable network.proxy.socks_remote_dns, or *.nvcm.air will "
        "not resolve.",
        fg="yellow",
    )
