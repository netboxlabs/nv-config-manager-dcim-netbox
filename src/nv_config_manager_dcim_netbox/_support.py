# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Shared helpers for the NetBox DCIM provider."""

from __future__ import annotations

from typing import NoReturn

from nv_config_manager_dcim import DCIMOperationNotSupportedError

PROVIDER_NAME = "netbox"


def unsupported(operation: str) -> NoReturn:
    """Raise the SDK's not-supported error for an unimplemented operation.

    Every ``DCIMClient`` method in this provider currently delegates to this
    helper. Replace a method body with a real NetBox implementation to enable
    the operation.
    """
    raise DCIMOperationNotSupportedError(
        f'DCIM provider "{PROVIDER_NAME}" does not yet implement {operation}'
    )
