# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Device-id UUID codec: NetBox integer PKs <-> UUIDs for UUID-keyed NVCM
services (e.g. the config-store). Encoding is applied at output boundaries and
decoding is applied to every device-id method argument via the client decorator.
"""

from __future__ import annotations

from nv_config_manager_dcim import RenderDataRequest

from nv_config_manager_dcim_netbox.client import decode_device_id, encode_device_id
from tests._fake import fake_client as _client

_STATUS = "plugins/nv-config-manager/device-config-statuses/"


def test_encode_decode_round_trip():
    for pk in ("1", "53", "999999"):
        uid = encode_device_id(pk)
        assert uid != pk and "-" in uid and len(uid) == 36  # a real UUID string
        assert decode_device_id(uid) == pk


def test_decode_passes_through_non_uuid():
    # A raw NetBox id (or anything non-UUID) is returned unchanged — decoding is safe
    # to apply defensively/idempotently.
    assert decode_device_id("53") == "53"
    assert decode_device_id("not-a-uuid") == "not-a-uuid"


def test_get_render_enabled_device_ids_are_uuid_encoded():
    client = _client(rest_list={_STATUS: [{"device": 5}, {"device": 7}]})
    ids = client_run(client.get_render_enabled_device_ids(None))
    assert {decode_device_id(i) for i in ids} == {"5", "7"}
    assert all(decode_device_id(i) != i for i in ids)  # actually encoded, not raw


def test_decorator_decodes_uuid_arg_before_netbox_query():
    # get_render_data receives a UUID (as NVCM would pass it) and must query NetBox
    # by the decoded integer PK. We capture the id the transport actually used.
    seen = {}

    class _CaptureClient(_client_cls()):
        async def graphql_query(self, query, variables):
            seen["id"] = variables.get("id")
            raise _StopQuery

    client = _CaptureClient("https://nb", "t")
    try:
        client_run(client.get_render_data(RenderDataRequest(device_id=encode_device_id("53"))))
    except _StopQuery:
        pass
    assert seen["id"] == "53"  # decoded back to the NetBox PK


class _StopQuery(Exception):
    pass


def _client_cls():
    from nv_config_manager_dcim_netbox.provider import NetBoxDCIMClient

    return NetBoxDCIMClient


def client_run(coro):
    import asyncio

    return asyncio.run(coro)
