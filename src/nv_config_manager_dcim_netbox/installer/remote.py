# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Where a deployment command runs.

The kind and DSX Air paths run the same helm and kubectl steps; only the place
differs. Kind runs them against the current kubecontext, Air runs them on the
sim's NVCM box over SSH. `cluster.py` builds the argv either way and hands it to
one of these.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Protocol

from nv_config_manager_dcim_netbox.installer.shell import run, stream


class Runner(Protocol):
    """Executes a command somewhere and moves files to wherever that is."""

    def run(self, argv: list[str], *, stdin: str | None = None) -> str: ...
    def stream(self, argv: list[str]) -> None: ...
    def upload(self, local: Path, remote: str) -> None: ...


class LocalRunner:
    """Runs against the current kubecontext on this machine."""

    def run(self, argv: list[str], *, stdin: str | None = None) -> str:
        return run(argv, stdin=stdin)

    def stream(self, argv: list[str]) -> None:
        stream(argv)

    def upload(self, local: Path, remote: str) -> None:
        raise NotImplementedError("Local runs read files in place; nothing to upload.")


class SshRunner:
    """Runs on the sim's NVCM box, through NVCM's own AIR SSH plumbing.

    Wraps ``AirSimulationManager`` rather than shelling out to ssh directly: it
    already owns the sim's host key handling, credentials and upload path.
    """

    def __init__(self, manager, host: str, port: int) -> None:
        self._manager = manager
        self._host = host
        self._port = port

    def _remote(self, argv: list[str]) -> list[str]:
        return self._manager._ssh_cmd(self._host, self._port, shlex.join(argv))

    def run(self, argv: list[str], *, stdin: str | None = None) -> str:
        return run(self._remote(argv), stdin=stdin)

    def stream(self, argv: list[str]) -> None:
        # No progress streaming over SSH; the caller logs before and after.
        run(self._remote(argv))

    def upload(self, local: Path, remote: str) -> None:
        if not self._manager.upload_to_server(self._host, self._port, str(local), remote):
            raise RuntimeError(f"Failed to upload {local} to {remote}")
