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
import time
from pathlib import Path
from typing import Protocol

from nv_config_manager_dcim_netbox.installer.shell import CommandError, run, stream

# ssh reserves 255 for its own failures: it cannot connect, or authentication
# fails. Any other code is the remote command's, which must never be retried.
_SSH_FAILURE = 255
_SSH_ATTEMPTS = 4
_SSH_BACKOFF = 5.0


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


class ScriptRunner:
    """Records commands as shell lines instead of running them.

    NVCM deploys by running one command on the box and streaming it back, so a
    whole deploy costs a single SSH session. Driving the box step by step instead
    means an authenticated connection per command, and any one of them can catch
    the box's sshd mid-blip and lose the run.

    Passing this where an SshRunner would go renders the same steps `cluster.py`
    builds into a script, which is then uploaded and run in one session. The
    commands cannot drift from the ones the kind path executes, because they come
    from the same functions.
    """

    def __init__(self) -> None:
        self.lines: list[str] = []

    def run(self, argv: list[str], *, stdin: str | None = None) -> str:
        command = shlex.join(argv)
        if stdin is None:
            self.lines.append(command)
        else:
            # Quoted delimiter: the payload is data, never expanded by the shell.
            self.lines.append(f"{command} <<'NVCM_STDIN'\n{stdin.rstrip()}\nNVCM_STDIN")
        return ""

    def stream(self, argv: list[str]) -> None:
        self.run(argv)

    def upload(self, local: Path, remote: str) -> None:
        raise NotImplementedError("Uploads happen before the script runs, not inside it.")

    def script(self, *, marker: str, redact: list[str] | None = None) -> str:
        """Render the recorded commands as a bash script.

        `set -e` so the first failure stops the run, and the marker is echoed only
        on success, which is how NVCM detects a completed deploy over SSH.

        Tracing is on so a failure can be tied to the command that caused it, but
        the output is streamed back and may be captured, so any command carrying a
        value in *redact* runs with tracing off.
        """
        secrets = [s for s in (redact or []) if s]
        rendered = []
        for line in self.lines:
            if any(secret in line for secret in secrets):
                rendered.append("{ set +x; } 2>/dev/null\n" + line + "\nset -x")
            else:
                rendered.append(line)
        body = "\n\n".join(rendered)
        return (
            f"#!/usr/bin/env bash\nset -euo pipefail\nset -x\n\n{body}\n\nset +x\necho {marker}\n"
        )


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

    def _run_retrying(self, argv: list[str], stdin: str | None = None) -> str:
        """Run over SSH, retrying only when SSH itself failed.

        A freshly booted sim box drops or refuses the occasional connection while
        its sshd settles, and NVCM's own bring-up authenticates many times before
        we get here. One blip should not lose a deploy that is already twenty
        minutes in.

        Only exit 255 is retried, which ssh reserves for connection and
        authentication failures. Every other code came from the remote command
        itself, and re-running it could repeat a side effect.
        """
        remote = self._remote(argv)
        for attempt in range(1, _SSH_ATTEMPTS + 1):
            try:
                return run(remote, stdin=stdin)
            except CommandError as exc:
                if exc.returncode != _SSH_FAILURE or attempt == _SSH_ATTEMPTS:
                    raise
                time.sleep(_SSH_BACKOFF * attempt)
        raise AssertionError("unreachable")

    def run(self, argv: list[str], *, stdin: str | None = None) -> str:
        return self._run_retrying(argv, stdin)

    def stream(self, argv: list[str]) -> None:
        # No progress streaming over SSH; the caller logs before and after.
        self._run_retrying(argv)

    def upload(self, local: Path, remote: str) -> None:
        if not self._manager.upload_to_server(self._host, self._port, str(local), remote):
            raise RuntimeError(f"Failed to upload {local} to {remote}")
