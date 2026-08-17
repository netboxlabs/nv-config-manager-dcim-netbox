# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Wiring for the live suite.

Skipped unless ``NVCM_LIVE_SERVER`` and ``NVCM_LIVE_TOKEN`` point at a NetBox
holding the plugin's bootstrap data and a loaded topology. ``scripts/live-netbox.sh``
stands one up.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from nv_config_manager_dcim import create_dcim_client

SERVER = os.environ.get("NVCM_LIVE_SERVER")
TOKEN = os.environ.get("NVCM_LIVE_TOKEN")

pytestmark = pytest.mark.live


@pytest.fixture(scope="session")
def run():
    """Drive the provider's async surface from sync tests, on one loop.

    A per-call ``asyncio.run`` would close the client's connection pool between
    calls, so the session shares a loop.
    """
    loop = asyncio.new_event_loop()
    try:
        yield loop.run_until_complete
    finally:
        loop.close()


@pytest.fixture(scope="session")
def live_client(run):
    if not SERVER or not TOKEN:
        pytest.skip("set NVCM_LIVE_SERVER and NVCM_LIVE_TOKEN (see scripts/live-netbox.sh)")
    client = create_dcim_client("netbox", {"server": SERVER, "token": TOKEN})
    try:
        yield client
    finally:
        run(client.close())


@pytest.fixture(scope="session")
def trial_devices(live_client, run):
    """The blueprint's devices, keyed by name."""
    from nv_config_manager_dcim import DCIMDeviceSelectionFilter

    devices = run(live_client.list_devices(DCIMDeviceSelectionFilter()))
    if not devices:
        pytest.skip("no devices in NetBox; load a blueprint first")
    return {d.name: d for d in devices}
