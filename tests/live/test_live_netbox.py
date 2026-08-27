# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""The provider against a real NetBox holding real topology data.

What this catches that the fixture-based suites cannot: upstream schema drift.
Every read here goes through NetBox's GraphQL and REST APIs, so a renamed field,
a moved filter or a changed custom-field shape fails here rather than in a
cluster. The 4.7 upgrade produced two of exactly that kind of break.

It deliberately does not re-test provider logic already covered with fixtures --
mapping rules, error translation, render shaping. It asserts that a real NetBox
answers, and that what comes back is populated rather than an empty shell.

The topology is the `air_trial` blueprint, the same one the kind deployment
loads. Assertions name only what that blueprint guarantees.
"""

from __future__ import annotations

import pytest
from nv_config_manager_dcim import RenderDataRequest

pytestmark = pytest.mark.live

# From development/mock_topology/context/air_trial/devices/.
_LEAF = "tan-leaf-02"


def test_selection_returns_the_addressable_blueprint_devices(trial_devices):
    expected = {
        "oob-mleaf-01",
        "tan-leaf-01",
        "tan-leaf-02",
        "tan-leaf-03",
        "tan-leaf-04",
        "tan-leaf-05",
    }
    assert expected <= set(trial_devices)


def test_selection_applies_has_primary_ip(trial_devices):
    # list_devices always sends has_primary_ip=true. The blueprint's
    # oob-mgmt-server has none, so it must not come back -- if it does, the
    # filter stopped being applied or NetBox renamed it.
    assert "oob-mgmt-server" not in trial_devices


def test_device_metadata_resolves_the_related_objects(live_client, run, trial_devices):
    # site/platform/role are separate GraphQL traversals; a null here is the
    # usual first sign that a relation was renamed upstream.
    meta = run(live_client.get_device_metadata(trial_devices[_LEAF].id))
    assert meta is not None
    assert meta.name == _LEAF
    assert meta.platform == "Cumulus Linux"
    assert meta.site
    assert meta.role


def test_render_data_is_populated_not_just_shaped(live_client, run, trial_devices):
    """RenderData validating is not enough -- an all-defaults one also validates."""
    data = run(live_client.get_render_data(RenderDataRequest(device_id=trial_devices[_LEAF].id)))

    assert data.device.identity.name == _LEAF
    assert data.device.interfaces, "no interfaces; the interface query returned nothing"

    # The management interface carries an address and its parent prefixes, which
    # only resolve if IPAM loaded and the prefix lookup still works.
    mgmt = [i for i in data.device.interfaces if i.management_only]
    assert mgmt, "no management interface"
    assert any(addr.address for i in mgmt for addr in i.addresses)

    # `role` comes from the nvcm_interface_role custom field. NetBox 4.7 changed
    # how selection custom fields serialize; this is a text field and must stay
    # a plain string.
    roles = [i.role for i in data.device.interfaces if i.role]
    assert roles, "no interface carries nvcm_interface_role"
    assert all(isinstance(r, str) for r in roles)

    # Location data comes from a second query rooted at the site.
    assert data.location.location


def test_config_context_reaches_the_render_path(live_client, run, trial_devices):
    # 4.7 added pre-rendered config context caching. The blueprint seeds DHCP and
    # ZTP context that the render path reads, so an empty merge shows up here.
    data = run(live_client.get_render_data(RenderDataRequest(device_id=trial_devices[_LEAF].id)))
    assert data.device.services or data.device.routing, (
        "neither services nor routing populated; config_context did not merge"
    )


def test_dhcp_reads_the_bootstrap_and_topology_data(live_client, run):
    subnets = run(live_client.get_dhcp_auto_subnets())
    static = run(live_client.get_dhcp_static_data())
    # The blueprint tags DHCP prefixes and gives the oob devices fixed addresses.
    assert subnets or static, "DHCP produced nothing from a loaded topology"


def test_ztp_device_resolves(live_client, run, trial_devices):
    ztp = run(live_client.get_ztp_device(trial_devices[_LEAF].id))
    assert ztp is not None


def test_location_metadata_query_is_valid_against_a_real_schema(live_client, run):
    """The aliased list query must satisfy NetBox's own filter types.

    A unit test mocking graphql_query cannot catch this: `id` on a list filter
    is an IDComparisonFilterLookup, and passing a bare ID! made NetBox reject
    the whole document, failing every site-scoped workflow.
    """
    site = run(live_client.list_locations(("site",)))
    assert site, "the trial blueprint defines a site"

    found = run(live_client.get_location_metadata(site[0].id))

    assert found is not None
    assert found.id == site[0].id
    assert found.name == site[0].name


def test_location_metadata_returns_none_for_an_unknown_id(live_client, run):
    assert run(live_client.get_location_metadata("999999")) is None
