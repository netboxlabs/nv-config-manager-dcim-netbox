# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Ad-hoc probe: call one DCIMClient method against a configured NetBox.

Override via env: SERVER, TOKEN, DEVICE_ID. While methods are stubbed this
prints a DCIMOperationNotSupportedError (no NetBox contact needed).
"""

from __future__ import annotations

import asyncio
import os

from nv_config_manager_dcim import create_dcim_client


async def main() -> None:
    client = create_dcim_client(
        "netbox",
        {
            "server": os.environ.get("SERVER", "http://localhost:8000"),
            "token": os.environ.get("TOKEN", "<token>"),
        },
    )
    device_id = os.environ.get("DEVICE_ID", "<device-id>")
    try:
        print(await client.get_device_metadata(device_id))
    except Exception as exc:  # noqa: BLE001 - probe reports whatever is raised
        print(f"{type(exc).__name__}: {exc}")
    finally:
        await client.close()


asyncio.run(main())
