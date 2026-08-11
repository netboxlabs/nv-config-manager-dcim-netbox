# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""NetBox support for NVCM's DSX Air simulations.

NVCM's `SimOrchestrator` creates the simulation and deploys NVCM; this package
deploys NetBox into the sim's kind cluster and loads the simulated fabric into
it.

Needs the `air` extra (see `make setup-air`).
"""

from __future__ import annotations


class AirSupportUnavailable(ImportError, RuntimeError):
    """Raised when the `air` extra is not installed.

    Also an ``ImportError``: this is an import failure, and it lets the air tests
    skip themselves with ``pytest.importorskip`` on an install without the extra.
    """

    def __init__(self, cause: Exception | None = None) -> None:
        super().__init__(
            "DSX Air support needs nv-config-manager-installer, which is not "
            "published. Install it from an NVCM checkout:\n"
            "    make setup-air NVCM=../nv-config-manager\n"
            "or:\n"
            "    uv pip install -e ../nv-config-manager/installer"
            + (f"\n\nImport failed with: {cause}" if cause else "")
        )
