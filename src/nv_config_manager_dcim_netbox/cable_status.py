# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Cable-validation status persistence for NetBox."""

from __future__ import annotations

from collections.abc import Mapping

from nv_config_manager_dcim import (
    CableStatusUpdate,
    DCIMConflictError,
    DCIMInvalidDataError,
    DCIMNotFoundError,
)

_CABLES_PATH = "dcim/cables/"
_INTERFACES_PATH = "dcim/interfaces/"
_VALIDATION_STATUS_FIELD = "validation_status"


class NetBoxCableStatusMixin:
    """Store validation outcomes on NetBox cable custom fields."""

    async def update_cable_status(self, update: CableStatusUpdate) -> None:
        """Update the validation status of the cable attached to an interface."""
        interfaces = await self.rest_list(
            _INTERFACES_PATH,
            [("device_id", update.device_id), ("name", update.interface_name)],
        )
        if not interfaces:
            raise DCIMNotFoundError(
                f"No interface named {update.interface_name!r} found on device {update.device_id!r}"
            )
        if len(interfaces) > 1:
            raise DCIMConflictError(
                f"Multiple interfaces named {update.interface_name!r} found on device "
                f"{update.device_id!r}"
            )

        cable_reference = interfaces[0].get("cable")
        if not isinstance(cable_reference, Mapping) or not cable_reference.get("id"):
            raise DCIMNotFoundError(
                f"Interface {update.interface_name!r} on device {update.device_id!r} has no cable"
            )

        cable_id = str(cable_reference["id"])
        cable = await self.rest_request("GET", f"{_CABLES_PATH}{cable_id}/")
        if not isinstance(cable, Mapping):
            raise DCIMInvalidDataError("NetBox returned invalid cable data")

        custom_fields = cable.get("custom_fields")
        if custom_fields is None:
            custom_fields = {}
        if not isinstance(custom_fields, Mapping):
            raise DCIMInvalidDataError("NetBox returned invalid cable custom-field data")
        if custom_fields.get(_VALIDATION_STATUS_FIELD) == update.status.value:
            return

        await self.rest_request(
            "PATCH",
            f"{_CABLES_PATH}{cable_id}/",
            json={"custom_fields": {_VALIDATION_STATUS_FIELD: update.status.value}},
        )
