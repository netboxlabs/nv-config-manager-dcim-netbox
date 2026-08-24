# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""SOCKS tunnel to a running DSX Air sim, resolved from the sim config.

The sim's SSH endpoint is allocated by AIR per simulation, so it is different
every run and cannot be written down. Everything needed to rebuild it is already
on disk, though: the sim config carries ``simulation_name`` (which AIR can be
asked about) and ``oob_ssh_password``. So this takes no arguments -- and, because
it spawns ssh itself, the password never reaches a command line, a file, or the
terminal.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import warnings
from dataclasses import dataclass

# The port NVCM's own proxy helper uses, so their documentation and ours agree.
DEFAULT_SOCKS_PORT = 8080

# Kept in step with nv_config_manager_installer.air_sim.proxy._SSH_OPTS. A sim
# box is rebuilt on every run and gets a recycled host key, so the known-hosts
# check would fail on a name that was a different machine yesterday.
SSH_OPTS = (
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    "UserKnownHostsFile=/dev/null",
    "-o",
    "PreferredAuthentications=password",
)


class SimNotFound(RuntimeError):
    """No running simulation matched the configured name."""


class AmbiguousSim(RuntimeError):
    """More than one simulation matched the configured name."""


@dataclass(frozen=True)
class SshEndpoint:
    """Where a sim's OOB server answers SSH, as AIR currently publishes it."""

    host: str
    port: int


def find_ssh_endpoint(manager, simulation_name: str, server_name: str) -> SshEndpoint:
    """Resolve *simulation_name* to the SSH endpoint of its OOB server.

    Read-only: unlike NVCM's ``setup_nvcm_server``, this never creates the SSH
    service. `air up` already created it, and a tunnel command that mutates the
    sim it was only asked to connect to would be a surprise.

    Raises :class:`SimNotFound` / :class:`AmbiguousSim` rather than picking one,
    because names are not unique in an AIR org and connecting to the wrong sim
    looks like a deployment that silently did nothing.
    """
    client = manager.client
    # The AIR SDK warns about response fields its models cannot parse ("Consider
    # updating your SDK"). Nothing read here touches them, and the warning is
    # attributed to this file, which makes upstream drift look like a bug of ours.
    warnings.filterwarnings("ignore", message=".*Consider updating your SDK.*", append=True)

    # Filtered here rather than passed to list(): the endpoint takes arbitrary
    # kwargs as query parameters, so an unsupported one would be ignored server
    # side and quietly return every simulation in the org.
    matches = [s for s in client.simulations.list() if s.name == simulation_name]
    if not matches:
        raise SimNotFound(simulation_name)
    if len(matches) > 1:
        raise AmbiguousSim(
            f"{len(matches)} simulations are called {simulation_name!r}: "
            + ", ".join(str(s.id) for s in matches)
        )
    simulation = matches[0]

    for node in client.nodes.list(simulation=simulation.id):
        if node.name != server_name:
            continue
        interfaces = {iface.id: iface for iface in client.interfaces.list(node=node)}
        for service in client.services.list(simulation=simulation.id):
            interface = getattr(service, "interface", None)
            if interface is None or interface.id not in interfaces:
                continue
            if getattr(service, "node_port", None) == 22:
                return SshEndpoint(service.worker_fqdn, int(service.worker_port))
        raise SimNotFound(
            f"{simulation_name} has no SSH service on {server_name}; "
            "it may still be booting, or `air up` did not finish"
        )
    raise SimNotFound(f"{simulation_name} has no node called {server_name}")


def tunnel_argv(endpoint: SshEndpoint, socks_port: int, user: str, password_fd: int | None) -> list:
    """Build the ssh argv for a SOCKS tunnel.

    ``password_fd`` selects how the password is supplied. With an fd, sshpass
    reads it from there (``-d``) -- never from argv, which any same-user ``ps``
    can read, and which is what NVCM's ``ProxyInfo.start_tunnel`` does with
    ``-p`` (sshpass's own help calls that "security unwise"). Without an fd,
    plain ssh runs and prompts, which is the fallback when sshpass is absent.

    ``-N`` because the tunnel forwards only; there is no remote command to run.
    """
    ssh = ["ssh", *SSH_OPTS, "-D", str(socks_port), "-N", "-p", str(endpoint.port)]
    ssh.append(f"{user}@{endpoint.host}")
    if password_fd is None:
        return ssh
    return ["sshpass", "-d", str(password_fd), *ssh]


def printable_command(endpoint: SshEndpoint, socks_port: int, user: str) -> str:
    """The tunnel as a command to paste, with no secret in it.

    The prompting form on purpose: `PreferredAuthentications=password` makes ssh
    ask, so this can be shared or pasted into a ticket without leaking the box's
    password.
    """
    return " ".join(tunnel_argv(endpoint, socks_port, user, password_fd=None))


def browser_argv(socks_port: int, url: str) -> list[str] | None:
    """A Chromium-family command pointed through the tunnel, or ``None``.

    ``socks5://`` rather than ``socks5h://``: Chrome resolves DNS through a
    socks5 proxy already, which is required here because ``*.nvcm.air`` exists
    only in the sim box's ``/etc/hosts``.

    A throwaway profile dir, so ``--ignore-certificate-errors`` (the Gateway's
    certificate is issued by an internal CA) applies to this window only and not
    to the browser the user reads mail in.
    """
    for candidate in (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        "google-chrome",
        "chromium",
        "chromium-browser",
    ):
        binary = candidate if os.path.isabs(candidate) else shutil.which(candidate)
        if binary and os.path.exists(binary):
            return [
                binary,
                f"--proxy-server=socks5://localhost:{socks_port}",
                "--user-data-dir=/tmp/chrome-nvcm-proxy",
                "--ignore-certificate-errors",
                url,
            ]
    return None


def open_tunnel(
    endpoint: SshEndpoint, password: str, *, socks_port: int, user: str
) -> subprocess.Popen:
    """Start the SOCKS tunnel and return the process.

    The password goes down an anonymous pipe whose read end is the only thing
    the child is told about, so it exists in this process's memory and the
    child's -- never in argv, the environment, or a file. The write end is closed
    immediately after, which is also what makes sshpass stop reading.
    """
    if shutil.which("sshpass") is None:
        # ssh prompts on the terminal instead. Nothing to hide, nothing to leak.
        return subprocess.Popen(tunnel_argv(endpoint, socks_port, user, password_fd=None))

    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, password.encode() + b"\n")
    finally:
        os.close(write_fd)
    argv = tunnel_argv(endpoint, socks_port, user, password_fd=read_fd)
    try:
        return subprocess.Popen(argv, pass_fds=(read_fd,))
    finally:
        os.close(read_fd)
