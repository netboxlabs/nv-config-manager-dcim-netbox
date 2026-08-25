# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""`nvcm-netbox air` — DSX Air simulations backed by NetBox."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import click

from nv_config_manager_dcim_netbox.installer import config as base_config
from nv_config_manager_dcim_netbox.installer.air import AirSupportUnavailable

# Safe at module scope: tunnel.py takes the AIR client as an argument rather than
# importing it, so `air --help` still works without the `air` extra installed.
from nv_config_manager_dcim_netbox.installer.air import tunnel as tunnel_mod


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


# Where the sim config lives, matching NVCM's air-sim so their documentation
# applies unchanged: the same default path and the same environment variable.
SIM_CONFIG_ENV = "NVCM_AIR_CONFIG"
# Read by AirSimulationManager when the sim config carries no key.
NGC_API_KEY_ENV = "NGC_API_KEY"
DEFAULT_SIM_CONFIG = Path("~/.nvcm-air-sim.yaml")


def _default_sim_config() -> Path:
    import os

    return Path(os.environ.get(SIM_CONFIG_ENV) or DEFAULT_SIM_CONFIG).expanduser()


# Keys a sim needs chosen rather than defaulted, in the order they are written.
_REQUIRED_KEYS = (
    "mock_blueprint",
    "deployment_name",
    "simulation_name",
    "size",
    "oob_ssh_password",
)

# Deliberately not written out. SimConfig defaults it from $NGC_API_KEY, which is
# how NVCM's documentation tells you to supply it; writing `ngc_api_key: ''` here
# would override that default with an empty string.
_ENV_ONLY_NOTE = "# ngc_api_key comes from $NGC_API_KEY. Add it here only to override."

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

    # Generated once, here, rather than left to SimConfig's default_factory, which
    # mints a new one on every run. cloud-init sets the box's `nvcm` password from
    # this, so an unrecorded value cannot be used to SSH in and cannot match a box
    # that was already provisioned: re-running then fails on rsync with
    # "Permission denied (publickey,password)".
    from nv_config_manager_installer.air_sim.sim_config import generate_oob_ssh_password

    chosen = {
        "mock_blueprint": blueprint,
        "deployment_name": name,
        "simulation_name": f"nv-config-manager-netbox-{blueprint.replace('_', '-')}",
        "size": value("size") or "small",
        "oob_ssh_password": generate_oob_ssh_password(),
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
        "# explicitly and enables the bundled Nautobot population job.",
        "",
    ]
    for key in _REQUIRED_KEYS:
        got = chosen[key]
        rendered = "''" if got == "" else got
        lines.append(f"{key}: {rendered}")
        if key == "oob_ssh_password":
            lines[-1] += "   # ssh nvcm@<box> with this"

    lines += ["", _ENV_ONLY_NOTE, "", "# --- optional, shown at their current defaults ---"]
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
    default=lambda: _default_sim_config(),
    show_default=str(DEFAULT_SIM_CONFIG),
    help=(
        f"Where to write the sim config. Defaults to ${SIM_CONFIG_ENV}, then {DEFAULT_SIM_CONFIG}."
    ),
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
    default=lambda: _default_sim_config(),
    show_default=str(DEFAULT_SIM_CONFIG),
    help=(
        "Sim config YAML (NVCM's air-sim format plus this provider's dcim_* keys). "
        f"Defaults to ${SIM_CONFIG_ENV}, then {DEFAULT_SIM_CONFIG}."
    ),
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
        from nv_config_manager_dcim_netbox.installer.air.config import NetBoxSimConfig
        from nv_config_manager_dcim_netbox.installer.air.orchestrator import (
            NetBoxSimOrchestrator,
        )
    except AirSupportUnavailable as exc:
        raise click.ClickException(str(exc)) from exc
    except ImportError as exc:
        raise click.ClickException(str(AirSupportUnavailable(exc))) from exc

    cfg = NetBoxSimConfig.from_yaml(config_path)
    if cfg.config_manager_ref == "main":
        raise click.ClickException(
            f"{config_path} sets config_manager_ref: main, which has no pluggable-DCIM "
            f"framework, so the sim would clone a tree this provider cannot run on.\n"
            f"Set it to {base_config.NVCM_BRANCH}, or remove the key to take that default."
        )
    if token:
        cfg.dcim_token = token
    if blueprint is not None:
        cfg.load_blueprint = blueprint

    orchestrator = NetBoxSimOrchestrator(cfg, ConsoleCallback(), chart=chart)
    click.secho(f"NetBox API token: {orchestrator.api_token}", fg="yellow")

    orchestrator.run()


# The sim's own hostnames, which resolve only through the tunnel: they live in
# the box's /etc/hosts, where NVCM writes its services and `air up` adds NetBox.
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

    Takes no arguments because it needs none. AIR allocates the box's SSH host
    and port per simulation, so they cannot be written down -- but the sim config
    already names the simulation, which AIR can be asked about, and already holds
    `oob_ssh_password`. The password is handed to ssh down a pipe, so it never
    appears in a command line, a file, or this output.
    """
    try:
        from nv_config_manager_installer.air_sim.constants import NVCM_BOX_USER
        from nv_config_manager_installer.air_sim.sim_manager import AirSimulationManager

        from nv_config_manager_dcim_netbox.installer.air.config import NetBoxSimConfig
    except AirSupportUnavailable as exc:
        raise click.ClickException(str(exc)) from exc
    except ImportError as exc:
        raise click.ClickException(str(AirSupportUnavailable(exc))) from exc

    cfg = NetBoxSimConfig.from_yaml(config_path)
    if not cfg.simulation_name:
        raise click.ClickException(
            f"{config_path} has no simulation_name, so there is nothing to look up.\n"
            "Set it to the name `air up` created the simulation with."
        )
    # $NGC_API_KEY counts: AirSimulationManager falls back to it
    # (`ngc_api_key or os.environ.get("NGC_API_KEY")`) and NVCM's SimConfig never
    # reads the environment, so gating on the config value alone rejected the
    # very thing this message tells you to do -- and `air up`, which lets the
    # manager do the resolving, worked all along.
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
            "be gone; `nvcm-netbox air up` creates a new one."
        ) from exc

    target = url or _NETBOX_URL_TEMPLATE.format(hostname=cfg.netbox_hostname())
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
