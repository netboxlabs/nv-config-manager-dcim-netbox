# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Subprocess helpers for the deployment commands."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence


class CommandError(RuntimeError):
    """A subprocess exited non-zero."""

    def __init__(self, argv: Sequence[str], returncode: int, output: str = "") -> None:
        self.argv = list(argv)
        self.returncode = returncode
        self.output = output
        shown = " ".join(self.argv)
        super().__init__(f"`{shown}` exited {returncode}" + (f"\n{output}" if output else ""))


def require_tools(*names: str) -> None:
    """Raise unless every named executable is on PATH.

    Checked up front so a 20-minute deploy does not fail at the helm step because
    helm was never installed.
    """
    missing = [name for name in names if shutil.which(name) is None]
    if missing:
        raise RuntimeError(f"Required executable(s) not found on PATH: {', '.join(missing)}")


def run(argv: Sequence[str], *, stdin: str | None = None, quiet: bool = False) -> str:
    """Run *argv*, returning stdout. Raises CommandError on failure.

    ``stdin`` is for values that must not appear in the process table — the
    NetBox API token is piped to a shell rather than passed as an argument.
    """
    result = subprocess.run(  # noqa: S603 - argv is built from validated inputs
        list(argv),
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise CommandError(argv, result.returncode, (result.stdout + result.stderr).strip())
    if not quiet and result.stderr.strip():
        # helm and kubectl put progress on stderr even when they succeed.
        pass
    return result.stdout


def stream(argv: Sequence[str], *, cwd: str | None = None) -> None:
    """Run *argv* with its output attached to this terminal.

    Used for the NVCM deploy, which takes many minutes and whose progress the
    user needs to see as it happens.
    """
    result = subprocess.run(list(argv), cwd=cwd, check=False)  # noqa: S603
    if result.returncode != 0:
        raise CommandError(argv, result.returncode)
