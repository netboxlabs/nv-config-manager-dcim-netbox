# AGENTS.md, nv-config-manager-dcim-netbox

Source of truth for AI agents and new engineers working in this repository.
`CLAUDE.md` is a shim that pulls this file in.

## Repository Overview

`nv-config-manager-dcim-netbox` is the **NetBox implementation of the DCIM
provider API** for [NVIDIA Config Manager](https://github.com/dsx-ai-factory/nv-config-manager)
(NVCM). NVCM loads it through the `nv_config_manager.dcim` entry point and
talks to NetBox through it for everything DCIM: inventory, IPAM, DHCP, ZTP,
firmware, lifecycle state, render data, Spectrum-X and InfiniBand fabric
operations, and the interpretation of change events.

It is one half of the integration. The other half is the NetBox plugin
[`netbox-nv-config-manager`](https://github.com/netboxlabs/netbox-nv-config-manager),
which adds the fabric/overlay models and config-state records this provider
reads and writes, and publishes NetBox change events onto NVCM's NATS bus. The
two ship together and share an event envelope contract (`contract_version`,
exact-matched); upgrade them as a pair.

This repo also carries the **deployment tooling** that puts the two next to
NVCM: the `nvcm-netbox` CLI, a Helm chart for NetBox, the NetBox and provider
images, an air-gapped bundle builder, and a DSX Air simulation driver.

Owned by NetBox Labs. Apache-2.0.

## Tech Stack

- Python **3.13+**. The floor is the SDK's (`nv-config-manager-dcim`); this
  package runs inside NVCM's application pods, not NetBox's, so it is not tied
  to NetBox's Python range.
- `uv` for environments, `hatchling` for builds. The SDK and NVCM's installer are
  **not published to an index**: `make setup` installs them from a sibling
  NVCM checkout (`$NVCM_REPO`, default `../nv-config-manager`).
- `aiohttp` async transport: GraphQL for reads, REST for writes.
- `pytest` with `asyncio_mode = "auto"` and per-area markers (see Testing).
- `ruff` **0.15.11**, line length 100, rules `E W F I B UP`. The pin is repeated
  in `.pre-commit-config.yaml` and `.github/workflows/ci.yml`; keep all three
  equal.
- Helm 3, kind, kubectl and Docker for the deployment paths. None are needed
  for the unit suite.

## Repository Map

```text
.
├── src/nv_config_manager_dcim_netbox/
│   ├── __init__.py             # exports NetBoxProvider, NetBoxDCIMClient
│   ├── provider.py             # NetBoxProvider (entry point), SUPPORTED_API_VERSIONS, NetBoxDCIMClient (mixin stack)
│   ├── client.py               # BaseNetBoxClient: settings, auth, GraphQL/REST transport, error mapping
│   ├── _mappers.py             # NetBox payload -> SDK model helpers shared by the areas
│   ├── fabric_common.py        # FabricCommonMixin: shared fabric lookups, path constants
│   ├── inventory.py            # devices, interfaces, cables, locations          (marker: inventory)
│   ├── reference.py            # roles, platforms, statuses, selection lists    (reference)
│   ├── ipam.py                 # VRFs, namespaces-as-tags, route distinguishers (ipam)
│   ├── hosts.py                # host / MAC correlation                         (hosts)
│   ├── dhcp.py                 # DHCP contexts and auto-subnets                 (dhcp)
│   ├── ztp.py                  # ZTP                                            (ztp)
│   ├── firmware.py             # OS image / firmware targets                    (firmware)
│   ├── lifecycle_state.py      # DeviceConfigStatus / Intended / Backup records (state)
│   ├── secrets.py              # secret *references* from config context        (secrets)
│   ├── render.py               # get_render_data -> typed RenderData            (render)
│   ├── spectrumx.py            # Spectrum-X overlay/VRF provisioning            (spectrumx)
│   ├── infiniband.py           # IB P-Key lifecycle                             (ib)
│   ├── cable_status.py         # cable validation-status custom field
│   ├── events.py               # change-event -> device-render fan-out (_HANDLERS)  (events)
│   ├── integration_adapter.py  # inspection adapter for NVCM's shared live-cluster suite
│   └── installer/              # the `nvcm-netbox` CLI (`installer` extra)
│       ├── cli.py              # click entry point; wraps NVCM's installer commands
│       ├── cluster.py          # kind + helm orchestration
│       ├── config.py           # NVCM install-config generation / patching
│       ├── deployer.py         # deploy sequencing
│       ├── remote.py, shell.py, tokens.py
│       └── air/                # DSX Air simulation driver (`air` extra)
├── tests/
│   ├── _fake.py                # fake_client(): routable stubbed transport for unit tests
│   ├── test_<area>.py          # unit suites, one or more per area, marked by area
│   ├── test_parity_*.py        # behaviour pinned to the Nautobot reference provider
│   ├── test_coverage.py        # walks every DCIMClient protocol member: nothing may be a stub
│   ├── test_registry.py        # SUPPORTED_API_VERSIONS covers the installed SDK
│   ├── test_installer.py, test_deployer.py, test_chart.py, test_air.py   # deployment tooling; chart tests need helm
│   ├── integration/            # render through NVCM's real templates (needs the templates package, no cluster)
│   └── live/                   # against a real NetBox (NVCM_LIVE_SERVER / NVCM_LIVE_TOKEN)
├── build/
│   ├── versions.env            # pins: NETBOX_BASE_IMAGE, NETBOX_VERSION, NETBOX_BGP_VERSION, NETBOX_PLUGIN_REF
│   ├── netbox.Dockerfile       # netbox-docker image + plugin wheel + netbox-bgp
│   ├── check-netbox-version.py # NETBOX_VERSION within the plugin's min/max_version
│   ├── check-provider-image.sh # /plugin-wheels offline-install contract
│   └── assert-plugins-load.py, netbox-field-choices.py
├── deploy/
│   ├── netbox/                 # Helm chart `nvcm-netbox`: upstream netbox chart + our values/templates
│   │   ├── values.yaml         # `nvcm.*` is ours; `netbox.*` is passed to the upstream chart
│   │   └── templates/          # publisher-config-secret, netbox-settings-configmap, httproute, datastore-credentials
│   ├── airgapped/              # create-airgapped.sh, install-netbox.sh, netbox-images.sh
│   └── configs/                # NVCM install configs for local kind and DSX Air
├── docs/                       # deployment, airgapped, air-sim, event-delivery, supported-operations, netbox-data-mapping
├── scripts/                    # live-netbox.sh (disposable NetBox for tests/live), probe.py, ufm-mock.sh
├── Dockerfile                  # provider wheelhouse image: /plugin-wheels for NVCM's offline init container
├── Makefile                    # every dev, test, image and deploy entry point (`make help`)
├── .github/workflows/ci.yml    # lint, netbox-pin, provider-image, test, live-netbox
├── CHANGELOG.md, SECURITY.md, LICENSE
└── pyproject.toml              # metadata, extras, entry point, ruff, pytest markers
```

Gitignored working files you may find locally and must not rely on in code or
docs: `NOTES.md`, `TESTING.md`, `parity/`, `ship-kit/`, `netbox-provider.toml`,
the `air-*.yaml` working configs, and `.build/`.

## Architecture

### The provider contract

`NetBoxProvider` (`provider.py`) is what NVCM discovers. It exposes
`metadata` (name `netbox`, must equal the entry-point name), `validate_settings`,
`create_client`, `register_render_event_handlers` and `normalize_event`.

`SUPPORTED_API_VERSIONS` is a **literal tuple**, deliberately not the SDK's own
constant, so an SDK major bump is refused at discovery instead of failing inside
a render. Bump it only after re-checking the protocol surface
(`tests/test_coverage.py`), the render models and the cache schema version.

### One mixin per functional area

`NetBoxDCIMClient` stacks one mixin per area on `BaseNetBoxClient`. Every
`DCIMClient` member is implemented against native NetBox; there are **no**
`DCIMOperationNotSupportedError` stubs, and `tests/test_coverage.py` enforces
that. Where NetBox genuinely cannot do something the reference Nautobot provider
does, the method does the nearest correct thing and the difference is recorded
in `docs/supported-operations.mdx` ("NetBox-specific semantics").

### Identifiers, settings and auth

- Device and location ids are **native NetBox integer PKs** in both directions.
  They are per-model, so a bare id cannot be told apart across models; validate
  the shape with `client.is_netbox_pk`, not the kind.
- Settings: `server`, `token`, optional `public_url`, `verify` (bool or CA
  bundle path) and `timeout` (seconds). Not `endpoint`.
- Tokens: a value starting `nbt_` is sent as `Bearer` (NetBox v2); anything else
  as the v1 `Token` scheme. Deployments use v2 and need `API_TOKEN_PEPPERS` on
  the NetBox side.

### Errors

`client.py` maps transport failures to the SDK's `DCIMConnectivityError`,
non-JSON and GraphQL errors to `DCIMInvalidDataError`, and a GraphQL
missing-object error to `DCIMNotFoundError`. In the event path
(`events.py`), a transient failure is re-raised as `NetBoxUnavailableError`,
which is deliberately **not** a `DCIMError`: the render consumer acks every
`DCIMError` as processed-and-failed, and only a non-DCIM exception makes it nak
for redelivery.

### Change events

The plugin's `EVENTS_PIPELINE` bridge emits the provider-neutral envelope;
`normalize_event` validates it with `DCIMChangeEvent.from_dict`. `events._HANDLERS`
maps each `object_type` to a handler returning the device renders that change
should trigger (device and interface changes render that device; cable changes
render both ends; config-context, VLAN, VRF, overlay-assignment and BGP changes
fan out to every affected device). `docs/event-delivery.md` is the full story.

### NetBox-side mapping decisions

Recorded in `docs/supported-operations.mdx` and `docs/netbox-data-mapping.mdx`.
The ones that bite most often:

- Nautobot's Location tree is four NetBox models (Region, SiteGroup, Site,
  Location); Site and Location ids collide, and a Nautobot "site" is often a
  NetBox Location.
- IPAM namespaces do not exist in NetBox; a namespace is a VRF tag
  (`namespace_tag`, default `spectrumx`).
- Statuses NetBox lacks live in the plugin's `nvcm_status` custom field or in
  `FIELD_CHOICES` extensions the NetBox deployment must load.
- Secrets are **references** only, read from config context; the provider holds
  no secret store.

### Deployment tooling

`nvcm-netbox` wraps NVCM's installer: `netbox up` deploys the chart and mints the
API token NVCM needs; `init`, `validate`, `generate-values` and `deploy` hand
through to NVCM's commands; `verify` publishes a synthetic change event and
checks it reaches the render consumer. The chart (`deploy/netbox/`) wraps the
upstream `netbox` chart; everything under `nvcm.*` in `values.yaml` is ours.
Pins for the NetBox base image, NetBox version, netbox-bgp and the plugin ref are
in `build/versions.env` and nowhere else.

## Commands

Run from the repo root. `make help` lists everything.

| Command | What it does |
|---|---|
| `make setup` | Create `.venv`, install the SDK, templates and installer from `$NVCM_REPO`, this package with the `installer` extra, and dev tools. Refuses an existing `.venv`; `make clean` first |
| `make test` | Unit suite (`.venv/bin/pytest -q`) |
| `make <area>` | One area's suite via its marker: `inventory reference ipam hosts dhcp ztp firmware state secrets render spectrumx ib lifecycle events` |
| `make lint` | `ruff check` + `ruff format --check` on `src/` and `tests/` |
| `make version-check` | Chart image tag == `pyproject` version; `NETBOX_VERSION` within the plugin's range (skipped without a plugin checkout at `PLUGIN=`) |
| `make check` | lint + version-check + test |
| `.venv/bin/pytest tests/integration/ -v` | Render through NVCM's real templates (no cluster) |
| `scripts/live-netbox.sh <dir> <blueprint>` then `NVCM_LIVE_SERVER=... NVCM_LIVE_TOKEN=... .venv/bin/pytest tests/live` | Live suite against a disposable NetBox |
| `make test-integration INTEGRATION_ARGS=--use-port-forward` | NVCM's shared cluster suite through our adapter (needs a running deployment) |
| `make images` / `make image` / `make netbox-image` | Build both images, the provider wheelhouse, or NetBox + plugin |
| `make check-image` | Assert the wheelhouse's offline `/plugin-wheels` contract |
| `make kind-up` / `make kind-down` | Deploy NetBox + NVCM on kind / tear it down |
| `make topology BLUEPRINT=air_trial` | Load a mock topology into the deployed NetBox |
| `make airgapped` | Build the offline bundle |
| `.venv/bin/nvcm-netbox --help` | The CLI |

## Development

1. Clone `dsx-ai-factory/nv-config-manager` and `netboxlabs/netbox-nv-config-manager`
   as siblings, or `export NVCM_REPO=...` and pass `PLUGIN=...` to `make`.
2. `make setup`. For provider-only work without an NVCM checkout, the README's
   "Local Development Setup" installs the SDK straight from git.
3. `make check` before pushing. CI runs the same ruff pin.
4. Every source and test file starts with the two SPDX header lines; copy them
   from any existing file.
5. For changes that touch NetBox data shapes, run the live suite: unit tests use
   canned payloads (`tests/_fake.py`) and cannot see a renamed GraphQL field or
   a changed custom-field shape.

The plugin and this provider move together. A change to the plugin's models,
events or custom fields usually needs a matching change here and in
`docs/supported-operations.mdx`, and vice versa.

## Testing

- **Unit** (`tests/test_*.py`): `pytest` with `fake_client()` from `tests/_fake.py`,
  which routes canned GraphQL/REST payloads by path and records writes. Mark each
  test module with its area marker so `make <area>` finds it. Add tests in the
  same commit as the change.
- **Parity** (`tests/test_parity_*.py`): behaviour the reference Nautobot
  provider has that NetBox must match. Changing one is a contract decision;
  record it in `docs/supported-operations.mdx`.
- **Coverage of the protocol** (`tests/test_coverage.py`): every `DCIMClient`
  member must be implemented. A new SDK method fails here first.
- **Integration** (`tests/integration/`): render data through the real
  `nv_config_manager_templates` renderer; auto-skipped without that package.
- **Live** (`tests/live/`): skipped unless `NVCM_LIVE_SERVER` and
  `NVCM_LIVE_TOKEN` are set. `scripts/live-netbox.sh` stands one up with the
  plugin, bootstrap data and a blueprint. CI runs this only when
  `PLUGIN_REPO_TOKEN` can check out the plugin repo.
- **Deployment tooling** (`test_installer.py`, `test_chart.py`, `test_deployer.py`,
  `test_air.py`): chart tests shell out to `helm template` and skip without helm
  or the vendored chart under `deploy/netbox/charts/` (`helm dependency build`).
  `test_air.py` needs the `air` extra.

Do not mock NetBox at the ORM level; there is none here. Mock the transport
through `fake_client`, or go live.

### Reporting test results

Include the exact command, the working directory, pass/fail counts, and the
full output of any failure. Do not claim a test passed without running it.

## CI/CD

`.github/workflows/ci.yml`, on pull requests and pushes to `main`:

- `lint`: ruff check and format check at the pinned version.
- `netbox-pin`: `NETBOX_BASE_IMAGE` tag matches `NETBOX_VERSION`, is a release
  (never a pre-release), and exists on ghcr.io.
- `provider-image`: builds the wheelhouse image and asserts the offline
  install contract.
- `test`: unit suite against the SDK cloned from NVCM `main`.
- `live-gate` / `live-netbox`: the live suite on a PyPI NetBox with the plugin,
  only when `PLUGIN_REPO_TOKEN` is set; otherwise visibly skipped.

CodeQL runs through GitHub's default setup. There is **no release workflow**
yet; the images are built locally with `make images` and side-loaded.

## Common Tasks

### Implement or change a DCIMClient operation

1. Find the area mixin in `src/nv_config_manager_dcim_netbox/<area>.py`. Shared
   lookups go in `fabric_common.py`; payload shaping in `_mappers.py`.
2. Reads via GraphQL, writes via REST, both through `BaseNetBoxClient`.
3. If NetBox cannot express the operation the way Nautobot does, implement the
   nearest correct behaviour and document the difference under
   "NetBox-specific semantics" in `docs/supported-operations.mdx`.
4. Unit test with `fake_client` under the area's marker; add a live test if the
   NetBox payload shape is new.

### Add or change a render event handler

1. Add the `object_type` to `events._HANDLERS` with a handler that returns the
   affected device renders. Transient NetBox failures must surface as
   `NetBoxUnavailableError`.
2. Test it in `tests/test_events.py`; the plugin's bridge must also emit the
   object type (`netbox_nv_config_manager/events_pipeline.py`).
3. Update `docs/event-delivery.md`.

### Bump the SDK API version

1. Diff the `DCIMClient` protocol and render models against the new SDK.
2. Make `tests/test_coverage.py` pass, then add the version to
   `SUPPORTED_API_VERSIONS` in `provider.py`.

### Bump NetBox, netbox-bgp or the plugin

1. Edit `build/versions.env` only. `NETBOX_BASE_IMAGE` must be a released
   `vX.Y.Z` netbox-docker tag; `NETBOX_VERSION` must equal it; `NETBOX_BGP_VERSION`
   must satisfy the plugin's `netbox-bgp` floor.
2. `make version-check` with a plugin checkout, then `make netbox-image`.
3. The `netbox-pin` CI job re-checks the tag.

### Change the NetBox chart

1. Our settings live under `nvcm.*` in `deploy/netbox/values.yaml` and in our
   templates; upstream chart values go under `netbox.*`.
2. Anything secret goes through `extraConfig` as a Secret
   (`templates/publisher-config-secret.yaml`), never through values, which land
   in a ConfigMap. Non-secret NetBox settings go in
   `templates/netbox-settings-configmap.yaml`.
3. Read new `nvcm.*` values defensively (`hasKey`, `default dict`): `helm upgrade
   --reuse-values` replays old values where the key does not exist.
4. Assert the rendered output in `tests/test_installer.py` or `tests/test_chart.py`.

### Cut a release

1. Bump `version` in `pyproject.toml`; `make version-check` will demand the
   chart's `netbox.image.tag` match.
2. Update `CHANGELOG.md`.
3. Tag `vX.Y.Z`. `make images` and `make airgapped` produce the artifacts;
   publishing is manual today.

## Conventions and Patterns

- **SPDX headers** on every Python file.
- **One mixin per area, no stubs.** New protocol members are implemented, not
  raised as unsupported.
- **GraphQL reads, REST writes.** Multi-value filters are passed as lists of
  `(key, value)` tuples; values are strings.
- **NetBox drops unknown query parameters instead of rejecting them**, so a
  filter on a custom field the bootstrap has not created matches everything.
  Re-check `cf_*` filter results in Python.
- **`has_primary_ip=true` in `list_devices` is deliberate**; the reference
  provider hardcodes it.
- **Pins live in `build/versions.env`**; do not repeat them in docs, the Makefile
  or CI.
- **Three ruff pins must agree**: `pyproject.toml` dev group, `.pre-commit-config.yaml`,
  `.github/workflows/ci.yml`.
- **The wheelhouse must be self-contained.** `Dockerfile` collects every
  transitive dependency into `/plugin-wheels` because NVCM installs it with
  `pip --no-index`. New runtime dependencies go in `[project].dependencies`;
  deployment-only ones in the `installer` extra, which the wheelhouse does not
  carry.
- **Parity over preference.** Where the Nautobot reference provider defines a
  behaviour, match it unless NetBox cannot; then document.

## Troubleshooting

- **`tests/test_air.py` fails at collection with an `AttributeError` in
  `nv_config_manager_installer`.** The installer in `.venv` is stale against
  your NVCM checkout. `make clean && make setup`.
- **`make setup` fails on an existing `.venv`.** By design; `make clean` first.
- **Events publish but nothing renders.** The plugin's `nats_subject` /
  `nats_stream` and NVCM's `dcim.event_subject` / `dcim.event_stream` must agree,
  and the plugin publishes `provider: netbox`, which must equal NVCM's
  `[dcim] provider`. `nvcm-netbox verify` checks the path.
- **`verify` says `0 queued ... not enabled`.** The event arrived; the device is
  not render-enabled. Set `render_enabled` on its `DeviceConfigStatus`.
- **Images fail to pull with 429.** Docker Hub rate limit; `make images
  NVCM_NETBOX_BASE_MIRROR=mirror.gcr.io/library`.
- **`exec format error` on a DSX Air sim.** Built on Apple Silicon without
  `PLATFORM=linux/amd64`.
- **NVCM renamed its tree in September 2026** (`components/dcim` is now
  `packages/dcim`, templates are `packages/templates`, the Nautobot provider is
  `plugins/dcim/nautobot-2x`). Older notes and scripts may still use the old
  paths.

## References

- README: [`README.md`](./README.md), including the DSX Air and air-gapped flows.
- Docs: [`docs/`](./docs/), in particular `deployment.md`,
  `supported-operations.mdx`, `netbox-data-mapping.mdx`, `event-delivery.md`.
- Companion plugin: <https://github.com/netboxlabs/netbox-nv-config-manager>
  (its `AGENTS.md` covers the NetBox side).
- NVIDIA Config Manager: <https://github.com/dsx-ai-factory/nv-config-manager>;
  SDK in `packages/dcim`, templates in `packages/templates`, installer in
  `installer/`, reference Nautobot provider in `plugins/dcim/nautobot-2x`.
- Linear project: P-CAP-446 "NVCM support for NetBox".
- Security policy: [`SECURITY.md`](./SECURITY.md).
