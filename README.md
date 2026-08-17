# NetBox DCIM Provider for NVIDIA Config Manager

The **NetBox** implementation of the DCIM provider API for
[NVIDIA Config Manager](https://github.com/nvidia/nv-config-manager) (NVCM),
NVIDIA's network configuration platform for AI/HPC fabrics. It lets NVCM use
NetBox as its source of truth.

It works with the companion plugin
[`netbox-nv-config-manager`](https://github.com/netboxlabs/netbox-nv-config-manager),
which adds the fabric/overlay models NVCM needs and publishes NetBox object
changes onto NVCM's event bus.

The sections below follow NVCM's own README, so the two read the same way.
Where a command exists in both, it has the same name and the same flags.

## Overview

NVCM's installer deploys NVCM. It does not deploy a DCIM, and its config wizard
cannot express everything an external one needs. This repository fills that gap
with `nvcm-netbox`, which:

- deploys NetBox, with the plugin and the fabric models, into the same cluster
- creates the API token NetBox and NVCM must share
- runs NVCM's installer with NetBox set as the DCIM provider
- connects NetBox's change events to NVCM's event bus
- checks that a change in NetBox reaches the render service

## Deploying

```bash
git clone -b zb/pluggable-dcim https://github.com/nvidia/nv-config-manager
git clone https://github.com/netboxlabs/netbox-nv-config-manager
git clone https://github.com/netboxlabs/nv-config-manager-dcim-netbox
cd nv-config-manager-dcim-netbox

# Where your NVCM checkout is. Both `make` and `nvcm-netbox` read it, so set it
# once; without it they assume ../nv-config-manager.
export NVCM_REPO=../nv-config-manager

make setup         # venv, the SDK from your NVCM checkout, and the CLI
make kind-up       # builds the images, then deploys NetBox and NVCM
```

`make setup` rather than `pip install -e .` because the `nv-config-manager-dcim`
SDK is not published; it is installed from `$NVCM_REPO/components/dcim`.

It puts `nvcm-netbox` in the repo's virtualenv rather than on your PATH, which is
why the commands below are written `.venv/bin/nvcm-netbox`. Run
`source .venv/bin/activate` once and you can drop the prefix.

Running `up` again is safe.

> **NetBox version.** The plugin needs a GraphQL feature that ships in NetBox
> 4.7, so the NetBox image is built from 4.7.0-beta1. There is no published image
> for it yet; that first build takes a while, later ones are cached. The pins are
> in [`build/versions.env`](build/versions.env).

### Step by Step

`up` is the whole sequence in one command. To drive it yourself, `nvcm-netbox`
takes the same commands and flags as NVCM's installer, so anything in
[NVIDIA's documentation](https://github.com/nvidia/nv-config-manager) works here
with `nvcm-netbox` in place of `nv-config-manager-installer`:

```bash
.venv/bin/nvcm-netbox netbox up                  # cluster, NetBox, API token, config
.venv/bin/nvcm-netbox init                       # optional: edit the config in NVCM's TUI
.venv/bin/nvcm-netbox validate  generated/nv-config-manager-install.yaml
.venv/bin/nvcm-netbox deploy    generated/nv-config-manager-install.yaml \
  --image-source local --build-images --load-kind \
  --kind-cluster nv-config-manager \
  --install-envoy-gateway --install-cnpg-operator --install-cert-manager
.venv/bin/nvcm-netbox verify
```

| Command | |
| --- | --- |
| `netbox up` | NetBox only. No counterpart in NVCM, because NVCM has no DCIM to deploy |
| `init` | NVCM's TUI, opened on the config |
| `validate` | NVCM's `validate` |
| `generate-values` | NVCM's `generate-values` |
| `deploy` | NVCM's `deploy`. Deploys NVCM only |
| `verify` | Publishes a synthetic change event and checks it reaches the render service |

`validate`, `generate-values` and `deploy` hand their arguments straight to
NVCM's installer, so every flag it accepts works unchanged.

Run `netbox up` first. NetBox issues the API token NVCM authenticates with, so
`deploy` on its own leaves NVCM unable to reach its DCIM.

`init` opens NVCM's TUI. The wizard has no field for the provider wheelhouse
image or the API token, so `nvcm-netbox` writes those before it opens and puts
them back afterwards. Everything else, `cluster.size`, SSO, vault, ZTP, is yours
to change there.

One caveat: these commands run inside your NVCM checkout, so a relative
`--output-dir` or `--chart-dir` resolves against *that* directory. Pass absolute
paths.

## Local Development Setup

```bash
make setup     # venv + SDK, templates, provider, dev tools
make check     # ruff + unit tests
make test      # unit tests only
```

Per-area suites match the functional modules: `make inventory`, `make ipam`,
`make ib`, `make render`, and so on (`make help` lists them all).

No cluster is needed for any of these. To develop against a live NetBox without
one, see
[host-NetBox development mode](docs/deployment.md#host-netbox-development-mode).

## Kubernetes Deployment

Prerequisites:

- Docker or a compatible container runtime, with at least 24 GB for `small`
- Kind for local clusters, or a reachable Kubernetes cluster
- Helm 3.x
- kubectl configured for the target cluster
- Python 3.11 or newer, managed through `uv`
- A checkout of [nv-config-manager](https://github.com/nvidia/nv-config-manager)
  on `zb/pluggable-dcim`. `main` does not carry the pluggable-DCIM framework
- A checkout of
  [netbox-nv-config-manager](https://github.com/netboxlabs/netbox-nv-config-manager),
  which the NetBox image is built from

### Resource Profiles

The same `cluster.size` profiles NVCM defines, selected with `--size`. They size
NVCM; NetBox, Postgres and Valkey add about 1.6 GiB of memory requests on top,
and the profile does not change that.

| Size | Environment | Suggested Capacity |
| ---- | ----------- | ------------------ |
| `small` | Local laptop or CI | 8 vCPU, 24 GB RAM |
| `medium` | Remote VM or staging | 16 vCPU, 64 GB RAM |
| `large` | Production | 96 vCPU, 256 GB RAM, 3+ nodes |

### Local Kind Deployment

```bash
make kind-up
```

The default `make kind-up` target creates the Kind cluster if needed, builds
local images, loads them into Kind, installs required operators, deploys NetBox,
and deploys using `deploy/configs/local-netbox.yaml`.

The cluster is created from NVCM's own `deploy/kind-config.yaml`, which maps the
Gateway's NodePorts onto host 443/80. A default `kind create cluster` does not,
and the Gateway is then unreachable from the host.

```bash
.venv/bin/nvcm-netbox up --no-create-cluster  # require an existing cluster
.venv/bin/nvcm-netbox up --no-kind-load       # a real cluster, images from a registry
.venv/bin/nvcm-netbox up --size medium        # larger resource profile
```

A fresh NetBox is empty, and an empty DCIM renders nothing. Populate or refresh
the mock topology:

```bash
make topology                      # air_trial, 7 devices
make topology BLUEPRINT=air_superpod   # 24 devices
```

The blueprints come from `development/mock_topology/context/` in your NVCM
checkout.

See [docs/deployment.md](docs/deployment.md) for the manual sequence and
troubleshooting.

### Local Hostnames

NetBox is published through NVCM's Gateway alongside NVCM's own services. Add
these to `/etc/hosts` to reach it from your workstation:

```
127.0.0.1 config-manager.local
127.0.0.1 netbox.config-manager.local
127.0.0.1 render.config-manager.local
127.0.0.1 ztp.config-manager.local
127.0.0.1 dhcp.config-manager.local
127.0.0.1 workflow.config-manager.local
127.0.0.1 config-store.config-manager.local
127.0.0.1 temporal.config-manager.local
```

Local endpoints:

- UI: https://config-manager.local
- NetBox: https://netbox.config-manager.local
- Workflow API: https://workflow.config-manager.local
- Config Store API: https://config-store.config-manager.local

The Gateway's certificate is self-signed, so a browser warns on first visit.
`make install-cert` in your NVCM checkout trusts it system-wide.

### NetBox Credentials

Generated at install and kept in the `netbox-superuser` secret. The username is
`admin`:

```bash
kubectl get secret netbox-superuser -n nv-config-manager -o jsonpath='{.data.password}' | base64 -d && echo
kubectl get secret netbox-superuser -n nv-config-manager -o jsonpath='{.data.api_token}' | base64 -d && echo
```

The API token is the one NVCM authenticates with, so both sides hold the same
value. Pass `--token` to `up` to choose it yourself.

## Makefile Commands

```bash
make setup             # venv + SDK, templates, provider, dev tools
make setup-air         # the above plus the DSX Air extra
make images            # build both local images
make image             # the provider wheelhouse only
make netbox-image      # NetBox with the plugin only
make kind-up           # create a Kind cluster and deploy NetBox + NVCM
make kind-down         # delete the Kind cluster
make airgapped         # build an offline bundle carrying NVCM + NetBox
make airgapped-images  # list the images that bundle needs
make check             # lint + unit tests
make lint              # ruff check + format check
make test              # unit tests
make clean             # remove venv and caches
```

`make help` lists these plus the per-area test suites.

## Air-Gapped Deployment

The bundle is built by `deploy/airgapped/create-airgapped.sh`, which calls
NVCM's own bundle script and merges NetBox into the result: one tarball carries
the Helm charts, images, operator pins and offline installer for both.

On a machine with network access:

```bash
make images
export NVCM_REPO=../nv-config-manager
make airgapped         # or: deploy/airgapped/create-airgapped.sh --version v0.1.0 --arch amd64
```

Every argument is forwarded to NVCM's script, so its flags work unchanged.

On the target, upload the images and charts, then install NetBox **before**
NVCM, since NetBox issues the API token:

```bash
tar -xzf nv-config-manager-netbox-airgapped-v0.1.0-amd64.tar.gz
cd nv-config-manager-netbox-airgapped-v0.1.0-amd64

./upload-to-registry.sh --registry registry.example.com/nv-config-manager \
  --chart-registry registry.example.com/nv-config-manager/charts \
  --username '<user>' --password-stdin

./install-netbox.sh --registry registry.example.com/nv-config-manager

./installer/nv-config-manager-installer deploy install.yaml \
  --chart-dir helm --image-source registry \
  --install-envoy-gateway --install-cert-manager --install-cnpg-operator
```

See [docs/airgapped.md](docs/airgapped.md) for the full workflow.

## Testing

No cluster required:

```bash
make test                     # unit tests
make lint                     # ruff check + format check
make check                    # both
.venv/bin/pytest tests/test_render.py
.venv/bin/pytest -m ipam      # one functional area
```

Per-area suites match the functional modules, and `make help` lists them:
`inventory`, `reference`, `ipam`, `hosts`, `dhcp`, `ztp`, `firmware`, `state`,
`secrets`, `render`, `spectrumx`, `ib`, `lifecycle`, `events`.

The render tests under `tests/integration/` compare rendered output against
NVCM's own templates. They need the SDK and templates from your checkout, which
`make setup` installs, but no running deployment:

```bash
.venv/bin/pytest tests/integration/ -v
```

Against a live deployment, the event loop is checked end to end by:

```bash
.venv/bin/nvcm-netbox verify --device-id 3
```

That publishes a synthetic change event and confirms the DCIM consumer
dispatches a render. It reports `1 queued, 0 failed` on a deployment with
topology loaded; without one, NetBox rejects the device and it says so.

The DSX Air tests are skipped unless the `air` extra is installed, since it
pulls NVCM's installer from your checkout:

```bash
make setup-air && .venv/bin/pytest tests/test_air.py
```

## Documentation

- [Deployment](docs/deployment.md): the installer, manual bring-up, troubleshooting
- [DSX Air simulations](docs/air-sim.md): running a simulated fabric with NetBox
- [Air-gapped installation](docs/airgapped.md): building and installing an
  offline bundle carrying both NVCM and NetBox
- [Event delivery](docs/event-delivery.md): how change events reach NVCM
- [Supported operations](docs/supported-operations.mdx): the full operation surface
  by area, and how each maps onto NetBox
- [Render data mapping](docs/netbox-data-mapping.mdx): where each `RenderData`
  field comes from

## License

Apache-2.0. See [LICENSE](LICENSE).
