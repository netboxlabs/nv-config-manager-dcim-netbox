# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Full-surface coverage: every DCIMClient API is present and behaves as a stub.

Each operation is tagged with a functional-area marker so a single area can be
run in isolation (e.g. ``pytest -m dhcp`` / ``make dhcp``). Add real tests for
an area under the same marker as you implement it; move a method out of
``CATEGORIES`` into ``_IMPLEMENTED`` once it no longer raises the stub error.
"""

from __future__ import annotations

import inspect
from typing import get_protocol_members

import pytest
from nv_config_manager_dcim import (
    DCIMClient,
    DCIMInvalidDataError,
    DCIMOperationNotSupportedError,
    create_dcim_client,
)

from nv_config_manager_dcim_netbox import NetBoxProvider

_SETTINGS = {"server": "https://netbox.example", "token": "t"}

# Methods implemented by the transport (never stubbed): session lifecycle, the
# provider-owned identifier checks NVCM validates references with, and UI links.
_TRANSPORT = {
    "close",
    "__aenter__",
    "__aexit__",
    "is_valid_device_id",
    "is_valid_location_id",
    "get_device_ui_url",
}

# DCIMClient operations implemented against native NetBox (REST/GraphQL). These
# no longer raise DCIMOperationNotSupportedError, so they are excluded from the
# stub checks. Add a method here as you implement it.
_IMPLEMENTED = {
    # inventory
    "get_device_metadata",
    "get_device_serial",
    "get_device_selection_by_name",
    "list_devices",
    "get_device_interfaces",
    "get_network_device",
    "get_network_devices",
    "get_host_device",
    "get_host_devices",
    "get_managed_device_metadata",
    # ipam
    "get_device_vrfs",
    "assign_vrf_to_interface",
    "assign_vrf_to_device",
    "remove_unmapped_device_vrfs",
    "get_namespace_route_distinguishers",
    # reference (namespace zone = VRF grouped by tag)
    "list_namespace_tags",
    "get_location_metadata",
    # hosts
    "find_host_devices_by_mac",
    "update_dpu_device_inventory",
    "get_interface_hosts_by_mac",
    "get_connected_switch_port_by_remote_mac",
    "get_host_metadata_by_macs",
    "get_host_metadata_by_names",
    # reference
    "list_locations",
    "list_tenants",
    "list_roles",
    "list_overlays",
    "list_statuses",
    # ztp
    "get_ztp_device",
    "mark_ztp_device_provisioned",
    # secrets (config-context references, not a secret store)
    "get_device_password_mapping_users",
    "get_device_secret_versions",
    "get_device_password_secret_names",
    # render
    "get_render_data",
    "get_intended_interface_neighbors",
    # lifecycle state (device custom fields)
    "get_render_device_status",
    "get_render_enabled_device_ids",
    "get_backup_enabled_device_ids",
    "get_render_template_versions",
    "upsert_intended_configuration",
    "update_render_template_version",
    "get_configuration_backup_metadata",
    "record_configuration_backup",
    "has_recorded_config_drift",
    # spectrum-x
    "get_spectrum_x_vrfs",
    "provision_spectrum_x_vrf",
    "delete_spectrum_x_vrf",
    "delete_spectrum_x_overlay_if_unused",
    "reconcile_spectrum_x_overlay_assignments",
    # dhcp
    "get_dhcp_auto_subnets",
    "get_dhcp_static_data",
    "get_dhcp_site_options",
    "get_dhcp_contexts",
    # firmware
    "get_os_image_versions",
    "set_intended_os_image",
    "get_firmware_bundle",
    "set_device_firmware_intent",
    # infiniband
    "get_ib_switch_topology",
    "get_ib_interface_guid",
    "get_ib_interface_guids",
    "set_ib_interface_guid",
    "get_ib_interface_records",
    "find_ib_interfaces_by_guids",
    "ensure_ib_pkey_partition",
    "ensure_orphan_ib_pkey",
    "ensure_ib_pkey_assignments",
    "remove_ib_pkey_assignments",
    "get_ib_pkey_assignments",
    "sync_ib_pkey_assignments",
    "canonicalize_ib_host",
    "resolve_ib_host_site",
    "resolve_ib_pkey_context",
    "cleanup_ib_pkey_partition",
}
_NOT_STUBBED = _TRANSPORT | _IMPLEMENTED

# Functional area -> the DCIMClient operations it owns. Lists every operation
# (implemented or stubbed) so the per-area markers/Make targets stay stable;
# the stub checks below skip anything in _NOT_STUBBED.
CATEGORIES: dict[str, tuple[str, ...]] = {
    "inventory": (
        "get_device_metadata",
        "get_managed_device_metadata",
        "get_device_serial",
        "get_device_selection_by_name",
        "list_devices",
        "get_network_device",
        "get_network_devices",
        "get_host_device",
        "get_host_devices",
        "get_device_interfaces",
    ),
    "reference": (
        "list_locations",
        "get_location_metadata",
        "list_tenants",
        "list_roles",
        "list_statuses",
        "list_namespace_tags",
        "list_overlays",
    ),
    "ipam": (
        "get_device_vrfs",
        "assign_vrf_to_device",
        "assign_vrf_to_interface",
        "remove_unmapped_device_vrfs",
        "get_namespace_route_distinguishers",
    ),
    "hosts": (
        "find_host_devices_by_mac",
        "get_interface_hosts_by_mac",
        "get_host_metadata_by_macs",
        "get_host_metadata_by_names",
        "update_dpu_device_inventory",
        "get_connected_switch_port_by_remote_mac",
    ),
    "dhcp": (
        "get_dhcp_auto_subnets",
        "get_dhcp_static_data",
        "get_dhcp_site_options",
        "get_dhcp_contexts",
    ),
    "ztp": (
        "get_ztp_device",
        "mark_ztp_device_provisioned",
    ),
    "firmware": (
        "get_os_image_versions",
        "set_intended_os_image",
        "get_firmware_bundle",
        "set_device_firmware_intent",
    ),
    "state": (
        "get_render_device_status",
        "get_render_enabled_device_ids",
        "get_backup_enabled_device_ids",
        "get_render_template_versions",
        "upsert_intended_configuration",
        "update_render_template_version",
        "get_configuration_backup_metadata",
        "record_configuration_backup",
        "has_recorded_config_drift",
    ),
    "secrets": (
        "get_device_password_mapping_users",
        "get_device_secret_versions",
        "get_device_password_secret_names",
    ),
    "render": (
        "get_render_data",
        "get_intended_interface_neighbors",
    ),
    "spectrumx": (
        "get_spectrum_x_vrfs",
        "provision_spectrum_x_vrf",
        "delete_spectrum_x_vrf",
        "delete_spectrum_x_overlay_if_unused",
        "reconcile_spectrum_x_overlay_assignments",
    ),
    "ib": (
        "get_ib_switch_topology",
        "get_ib_interface_guid",
        "get_ib_interface_guids",
        "set_ib_interface_guid",
        "get_ib_interface_records",
        "find_ib_interfaces_by_guids",
        "ensure_ib_pkey_partition",
        "ensure_orphan_ib_pkey",
        "ensure_ib_pkey_assignments",
        "remove_ib_pkey_assignments",
        "get_ib_pkey_assignments",
        "sync_ib_pkey_assignments",
        "canonicalize_ib_host",
        "resolve_ib_host_site",
        "resolve_ib_pkey_context",
        "cleanup_ib_pkey_partition",
    ),
}

_PROTOCOL_METHODS = sorted(get_protocol_members(DCIMClient))
_STUB_METHODS = [name for name in _PROTOCOL_METHODS if name not in _NOT_STUBBED]
_STUB_CASES = [
    pytest.param(name, marks=getattr(pytest.mark, area), id=name)
    for area, names in CATEGORIES.items()
    for name in names
    if name not in _NOT_STUBBED
]

_SENTINEL = object()


def _client() -> DCIMClient:
    return create_dcim_client("netbox", _SETTINGS)


def _required_args(method: object) -> list[object]:
    """A placeholder value per required positional parameter.

    Stubs raise before touching their arguments, so only arity matters.
    """
    args: list[object] = []
    for name, param in inspect.signature(method).parameters.items():
        if name == "self":
            continue
        if param.default is not inspect.Parameter.empty:
            continue
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD, param.KEYWORD_ONLY):
            continue
        args.append(_SENTINEL)
    return args


def test_client_implements_entire_protocol() -> None:
    client = _client()
    missing = [name for name in _PROTOCOL_METHODS if not hasattr(client, name)]
    assert not missing, f"NetBoxDCIMClient is missing DCIMClient members: {missing}"
    assert isinstance(client, DCIMClient)


def test_categories_cover_every_operation() -> None:
    categorized = {name for names in CATEGORIES.values() for name in names}
    expected = set(_PROTOCOL_METHODS) - _TRANSPORT
    assert categorized == expected, (
        "CATEGORIES is out of sync with the DCIMClient protocol; "
        f"only-in-categories={categorized - expected}, "
        f"uncategorized={expected - categorized}"
    )


@pytest.mark.parametrize("name", sorted(_IMPLEMENTED))
def test_implemented_method_present(name: str) -> None:
    assert hasattr(_client(), name)


@pytest.mark.parametrize("name", _STUB_CASES)
async def test_every_stub_raises_unsupported(name: str) -> None:
    client = _client()
    method = getattr(client, name)
    try:
        with pytest.raises(DCIMOperationNotSupportedError):
            result = method(*_required_args(method))
            if inspect.isawaitable(result):
                await result
    finally:
        await client.close()


@pytest.mark.lifecycle
async def test_lifecycle_methods_are_implemented() -> None:
    provider = NetBoxProvider()
    provider.validate_settings(_SETTINGS)  # must not raise
    client = provider.create_client(_SETTINGS)
    try:
        assert client.get_device_ui_url("1").endswith("/dcim/devices/1/")
    finally:
        await client.close()


@pytest.mark.events
def test_normalize_event_validates_and_parses() -> None:
    provider = NetBoxProvider()
    with pytest.raises(DCIMInvalidDataError):
        provider.normalize_event({})
    event = provider.normalize_event(
        {
            "provider": "netbox",
            "operation": "update",
            "object_type": "dcim.device",
            "object_id": "5",
            "timestamp": "2026-07-27T00:00:00Z",
            "contract_version": "1.0",
        }
    )
    assert event.object_type == "dcim.device"
    assert event.operation == "update"
    assert event.object_id == "5"


@pytest.mark.events
def test_register_render_event_handlers_registers() -> None:
    class _Registry:
        def __init__(self):
            self.registered: dict[str, object] = {}

        def register_render_event_handler(self, object_type, handler):
            self.registered[object_type] = handler

    registry = _Registry()
    NetBoxProvider().register_render_event_handlers(registry)
    assert "dcim.device" in registry.registered
    assert "dcim.interface" in registry.registered


def test_return_annotations_match_the_sdk_protocol():
    """Every method must declare the SDK's own return type.

    NVCM consumes these by attribute, so a structurally similar substitute
    passes our tests and fails in a Temporal activity. `get_os_image_versions`
    returned `tuple[str, str, str]` where the contract is `OSImageVersions`, a
    NamedTuple: same three values, same order, equal under `==`, and no
    `.intended_firmware`. The workflow died with

        AttributeError: 'tuple' object has no attribute 'intended_firmware'

    naming neither the provider nor the method.
    """
    import inspect

    from nv_config_manager_dcim import api as sdk_api

    from nv_config_manager_dcim_netbox.provider import NetBoxDCIMClient

    mismatched = []
    for name, protocol_method in inspect.getmembers(sdk_api.DCIMClient, inspect.isfunction):
        if name.startswith("_"):
            continue
        ours = getattr(NetBoxDCIMClient, name, None)
        if ours is None:
            continue
        expected = str(inspect.signature(protocol_method).return_annotation)
        actual = str(inspect.signature(ours).return_annotation)
        if expected != actual:
            mismatched.append(f"{name}: SDK says {expected}, we say {actual}")

    assert not mismatched, "return types diverge from the DCIM contract:\n  " + "\n  ".join(
        mismatched
    )
