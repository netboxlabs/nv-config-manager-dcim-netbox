# NetBox DCIM Provider

This package is the **NetBox** implementation of the NVIDIA Config Manager
(NVCM) DCIM provider API. It translates NetBox's native REST and GraphQL APIs,
data model, and change events into the provider-neutral contracts defined by the
standalone `nv-config-manager-dcim` SDK.

It follows the standard NV Config Manager provider layout: depend only on the
SDK, expose one provider entry point, and keep NetBox-specific transport and
schemas inside this package. NVCM services never import this package directly —
they discover it through the `nv_config_manager.dcim` entry-point group and
interact with it only through the SDK's `DCIMClient` protocol.

## Status

**Implemented.** All `DCIMClient` operations are implemented against NetBox
(REST + GraphQL), grouped by functional area (one module per area — see
[Layout](#layout)). The provider factory, settings validation, discovery, and
the aiohttp transport are functional. The unit suite passes (108 tests).

Change-event integration is live: `register_render_event_handlers` and
`normalize_event` are wired, so automatic render-on-change works via the
companion NetBox plugin's `EVENTS_PIPELINE` bridge
([`netbox_nv_config_manager`](https://github.com/netboxlabs/netbox-nv-config-manager)),
which publishes object changes as the DCIM change envelope this provider consumes.

Per-device lifecycle state (managed flags, intended/backup config metadata) is
backed by the plugin's first-class models, so the companion plugin is required
for the full feature set.

## Layout

```text
src/nv_config_manager_dcim_netbox/
├── __init__.py          # exports NetBoxProvider, NetBoxDCIMClient
├── provider.py          # NetBoxProvider factory + NetBoxDCIMClient composition
├── client.py            # BaseNetBoxClient: aiohttp transport, settings, close, ui-url
├── _support.py          # unsupported() helper (raises DCIMOperationNotSupportedError)
├── inventory.py         # Core inventory / DCIM
├── reference.py         # Reference / selection lists
├── ipam.py              # IPAM / VRF
├── hosts.py             # Host / MAC correlation
├── dhcp.py              # DHCP
├── ztp.py               # ZTP
├── firmware.py          # Firmware / OS image
├── lifecycle_state.py   # NVCM lifecycle state (managed / intended / backup)
├── secrets.py           # Secrets / passwords
├── render.py            # Render data assembly
├── spectrumx.py         # Spectrum-X overlays
├── infiniband.py        # InfiniBand
└── events.py            # Change events → render dispatch
```

`NetBoxDCIMClient` composes the per-area mixins over `BaseNetBoxClient`, so each
functional area can be implemented in its own module and reviewed independently.

## Install (development)

The SDK is not yet published to an index; install it from a sibling checkout of
`nv-config-manager` on the `zb/pluggable-dcim` branch:

```bash
# in a sibling directory
git clone https://github.com/NVIDIA/nv-config-manager.git
git -C nv-config-manager checkout zb/pluggable-dcim

# in this repo
uv pip install ../nv-config-manager/components/dcim   # the SDK
uv pip install -e .                                   # this provider
uv pip install pytest pytest-asyncio ruff
```

## Configure

The provider is selected by name and handed a plain settings mapping (NVCM
assembles this from its `[dcim]` / `[dcim.netbox]` configuration; keep the token
in the deployment's secret mechanism):

```toml
# for template-cli local testing
[provider]
name = "netbox"

[provider.settings]
server = "http://localhost:8000"
token  = "<netbox-api-token>"
# public_url = "http://localhost:8000"   # optional; used for UI links
# verify     = true                        # bool or path to a CA bundle
```

## Required NetBox permissions

The provider authenticates with a **v2 API token** (`Authorization: Bearer
nbt_<key>.<secret>`; a non-`nbt_` token falls back to the v1 `Token` scheme). The
token's user needs:

- **Read** across the objects the render/inventory/reference paths consume:
  `dcim` (devices, interfaces, sites, locations, regions, device-types,
  platforms, roles, cables), `ipam` (prefixes, vlans, vrfs, ip-addresses,
  mac-addresses), `tenancy` (tenants), `extras` (tags, config-contexts), the
  plugin models under `plugins/nv-config-manager/` (overlays, vxlans,
  overlay-assignments, device-config-statuses, intended-configs, backup-configs),
  and `netbox-bgp` sessions.
- **Write** where the provider mutates NetBox: the plugin lifecycle models
  (device/intended/backup config — upsert), overlays + vxlans + VRFs (Spectrum-X
  provisioning), interfaces + mac-addresses (DPU inventory), and devices (serial
  updates, interface VRF assignment).
- The provider also issues **OPTIONS** requests to read field choices (statuses).

For a lab, a superuser token is simplest; for production, grant read+write scoped
to the `dcim` / `ipam` / `tenancy` / `extras` apps, the plugin, and `netbox-bgp`.

## Event delivery

Change events originate in NetBox and are **pushed** to the render service — the
provider never polls. The companion plugin's `EVENTS_PIPELINE` bridge normalizes
each NetBox change into the provider-neutral `DCIMChangeEvent` envelope and
publishes it (to a logger, or to a NATS subject set by the plugin's
`nats_subject`, which must match NVCM's `dcim_change_subject`). The render
service consumes the envelope, calls `NetBoxProvider.normalize_event` to validate
it, and dispatches it to the handler registered for its `object_type`.

The provider registers render-event handlers for these native object types
(`events._HANDLERS`); each returns the device renders a change should trigger:

| `object_type` | Affected-device fan-out |
| --- | --- |
| `dcim.device` | the device itself |
| `dcim.interface` | the interface's parent device |
| `netbox_bgp.bgpsession` | the session's device |
| `netbox_nv_config_manager.overlayassignment` | the assigned device, or the assigned interface's device (resolved via REST) |

Object types whose fan-out needs cross-object queries (prefix / vrf / ipaddress /
cable / config-context) are intentionally not registered yet.

## Test the basics without NVCM core

```bash
# 1. Confirm discovery
uv run python -c "from nv_config_manager_dcim import discover_dcim_providers; print(discover_dcim_providers())"

# 2. Run the unit tests
uv run pytest

# 3. Exercise render against a live NetBox via the templates CLI
#    (install ../nv-config-manager/components/network-templates alongside)
template-cli cache-query --provider-config netbox-provider.toml --device-id <id> \
    --output-render-data-file rd.json
```

## Working on an operation

Each operation lives in its functional-area module (see [Layout](#layout)).
Reads use `self.graphql_query(...)`; writes use `self.rest_request("PATCH", ...)`.
Return the SDK Pydantic models — never raw NetBox responses. Map backend failures
to the SDK error taxonomy (see `client.py`). Where NetBox genuinely cannot satisfy
an operation, it raises `DCIMOperationNotSupportedError` with a clear explanation.

See NVCM's *Contribute a DCIM Provider* guide for the full authoring contract.

## License

Apache-2.0. See [LICENSE](LICENSE).
