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

## Overview

NVCM's installer is pluggable: a DCIM provider package registers an installer
provider, and the stock `nv-config-manager-installer` composes its steps, Helm
values and wizard screens into the common pipeline. This repository registers
the `netbox` installer provider, which:

- deploys NetBox, with the plugin and the fabric models, into the same cluster,
  before NVCM's own Helm release
- generates the NetBox v2 API token both sides share and installs it with
  `nvcm_init`
- wires NetBox's change events to NVCM's event bus (stream, subject, consumer)
- publishes NetBox through NVCM's Gateway
- supplies the NetBox stages of NVCM's DSX Air simulation wizard

`nvcm-netbox` adds the few NetBox-only commands around that: `verify` (does a
change in NetBox reach the render service), `netbox load-topology`,
`netbox up` (NetBox without NVCM) and `air tunnel`.

## Deploying

```bash
git clone https://github.com/nvidia/nv-config-manager
git clone https://github.com/netboxlabs/netbox-nv-config-manager
git clone https://github.com/netboxlabs/nv-config-manager-dcim-netbox
cd nv-config-manager-dcim-netbox

# Where your NVCM checkout is. `make` reads it; without it ../nv-config-manager.
export NVCM_REPO=../nv-config-manager

make setup         # venv with NVCM's installer, the SDK and this package's installer extra
make kind-up       # builds the images, then deploys NetBox and NVCM
```

`make setup` rather than `pip install -e .` because the `nv-config-manager-dcim`
SDK and `nv-config-manager-installer` are not published; both come from
`$NVCM_REPO`. The installer discovers the `netbox` provider through an entry
point, so the `installer` extra and `nv-config-manager-installer` must be in the
same environment. Check with `.venv/bin/nv-config-manager-installer providers
list`, which must list `netbox`.

The tools land in the repo's virtualenv rather than on your PATH, which is why
the commands below are written `.venv/bin/...`. Run `source .venv/bin/activate`
once and you can drop the prefix.

Running `make kind-up` again is safe.

> **NetBox version.** The plugin needs a GraphQL feature that ships in NetBox
> 4.7, and requires a released 4.7 -- not one of the 4.7.0 pre-releases. The
> image layers the plugin onto netbox-docker's published NetBox 4.7 image; the
> pins are in [`build/versions.env`](build/versions.env).

### Step by Step

`make kind-up` is the whole sequence in one command. To drive it yourself, use
NVCM's installer with `dcim.provider: netbox` in the config, so anything in
[NVIDIA's documentation](https://github.com/nvidia/nv-config-manager) applies
unchanged:

```bash
make images                                                  # NetBox + provider wheelhouse
kind load docker-image nv-config-manager-netbox:0.1.0 nv-config-manager-dcim-netbox:0.1.0 \
  --name nv-config-manager
.venv/bin/nv-config-manager-installer init -c my.yaml         # optional: edit a copy of the profile
.venv/bin/nv-config-manager-installer validate my.yaml
.venv/bin/nv-config-manager-installer deploy my.yaml --project-root $NVCM_REPO \
  --image-source local --build-images --load-kind --kind-cluster nv-config-manager \
  --install-envoy-gateway --install-cnpg-operator --install-cert-manager
.venv/bin/nvcm-netbox verify -c my.yaml
```

Start from [`deploy/configs/local-netbox.yaml`](deploy/configs/local-netbox.yaml),
which is a complete install config selecting NetBox. In the wizard the External
Services screen offers the `netbox` provider and its fields (image tag,
hostname, superuser name).

| Command | |
| --- | --- |
| `nv-config-manager-installer deploy` | NVCM's deploy; with `dcim.local: true` the `netbox` provider installs and bootstraps NetBox first and publishes its route after |
| `nvcm-netbox netbox up` | NetBox only, for a cluster NVCM is deployed to afterwards |
| `nvcm-netbox netbox load-topology` | Load a mock topology blueprint into NetBox |
| `nvcm-netbox verify` | Publishes a synthetic change event and checks it reaches the render service |

`--project-root` is where NVCM's Helm chart and image build contexts live. The
NetBox images come from this repository (`make images`) and are loaded into the
cluster separately; the installer builds only NVCM's.

## Local Development Setup

For provider-only development, install the DCIM SDK directly from the NVCM Git
repository; a separate checkout is not required:

```bash
uv venv --python 3.13
uv pip install --python .venv/bin/python \
  "nv-config-manager-dcim @ git+https://github.com/dsx-ai-factory/nv-config-manager.git@main#subdirectory=packages/dcim" \
  -e .
```

Replace `main` with a release tag or commit SHA for a reproducible install. A
sibling NVCM checkout is still useful when changing both repositories, and is
required for the full local deployment because it supplies the installer,
templates, Helm chart, and image build context:

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
- A checkout of
  [netbox-nv-config-manager](https://github.com/netboxlabs/netbox-nv-config-manager),
  which the NetBox image is built from

### Resource Profiles

The same `cluster.size` profiles NVCM defines, set in the install config. They
size NVCM; NetBox, Postgres and Valkey add about 1.6 GiB of memory requests on
top, and the profile does not change that.

| Size | Environment | Suggested Capacity |
| ---- | ----------- | ------------------ |
| `small` | Local laptop or CI | 8 vCPU, 24 GB RAM |
| `medium` | Remote VM or staging | 16 vCPU, 64 GB RAM |
| `large` | Production | 96 vCPU, 256 GB RAM, 3+ nodes |

### Local Kind Deployment

```bash
make kind-up
```

The default `make kind-up` target builds the NetBox and provider images, creates
the Kind cluster if needed, loads the images into it, and runs
`nv-config-manager-installer deploy` with `deploy/configs/local-netbox.yaml`,
which installs the operators, NetBox and NVCM.

The cluster is created from NVCM's own `deploy/kind-config.yaml`, which maps the
Gateway's NodePorts onto host 443/80. A default `kind create cluster` does not,
and the Gateway is then unreachable from the host.

```bash
make kind-up INSTALL_CONFIG=my.yaml           # a tuned copy of the profile
make kind-up KIND_CLUSTER=other               # another cluster name
make netbox-up                                # NetBox only
```

For a real cluster, run the installer directly without `--load-kind` and point
`dcim.provider_packages[].image` and `dcim.settings.image_repository` at a
registry.

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
value; NVCM reads it from the `dcim-token` Secret. Set
`secrets.k8s.dcim.values.token` in the install config to choose it yourself.

A login is required to reach anything: the chart sets `netbox.loginRequired: true`
rather than taking the upstream default of unauthenticated read-only access, which
NetBox 5.0 removes anyway.

## Makefile Commands

```bash
make setup             # venv + NVCM installer, SDK, templates, provider, dev tools
make setup-air         # compatibility alias for setup
make images            # build both local images
make image             # the provider wheelhouse only
make netbox-image      # NetBox with the plugin only
make check-image       # assert the provider image's offline /plugin-wheels contract
make kind-up           # create a Kind cluster and deploy NetBox + NVCM (INSTALL_CONFIG=)
make netbox-up         # deploy NetBox only, from INSTALL_CONFIG
make kind-down         # delete the Kind cluster
make topology          # load mock topology data into NetBox (BLUEPRINT=air_trial)
make tunnel            # SOCKS tunnel to a DSX Air sim (PRINT=1, BROWSER=1)
make airgapped         # build an offline bundle carrying NVCM + NetBox
make airgapped-images  # list the images that bundle needs
make check             # lint + version-check + unit tests
make lint              # ruff check + format check
make version-check     # chart tag == package version, NetBox pin within the plugin's range
make test              # unit tests
make clean             # remove venv and caches
```

`make setup` and `make setup-air` create the virtualenv, so they only run on a
clean checkout — `uv venv` refuses an existing `.venv`. Delete it first to
re-run them.

`make help` lists these plus the per-area test suites.

## Air-Gapped Deployment

The bundle is built by `deploy/airgapped/create-airgapped.sh`, which calls
NVCM's own bundle script and merges NetBox into the result: one tarball carries
the Helm charts, images, operator pins and offline installer for both.

On a machine with network access:

```bash
make images            # PLATFORM=linux/amd64 when the target is x86_64
export NVCM_REPO=../nv-config-manager
make airgapped         # or: deploy/airgapped/create-airgapped.sh --version v0.1.0 --arch amd64
```

Every argument is forwarded to NVCM's script, so its flags work unchanged. One
exception to their defaults: a bundle is built for one architecture, the build
host's unless `--arch` says otherwise, because the two images this adds are built
by `make images` one architecture at a time.

On the target, upload the images and charts, then install NetBox **before**
NVCM, since NetBox issues the API token:

```bash
tar -xzf nv-config-manager-netbox-airgapped-v0.1.0-amd64.tar.gz
cd nv-config-manager-netbox-airgapped-v0.1.0-amd64

./upload-to-registry.sh --registry registry.example.com/nv-config-manager \
  --chart-registry registry.example.com/nv-config-manager/charts \
  --username '<user>' --password-stdin

# --registry-username/--registry-password-stdin, or --pull-secret NAME, when
# the registry needs credentials to pull from.
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

The provider also runs NVCM's shared live-cluster integration suite through its
NetBox lifecycle adapter. The common assertions remain in the NVCM repository;
only provider-owned inspection of device flags, intended configuration, status,
and backup records lives here:

```bash
make test-integration INTEGRATION_ARGS=--use-port-forward
```

Use `INTEGRATION_TESTS` to select a module or node while developing, for example:

```bash
make test-integration \
  INTEGRATION_TESTS=../nv-config-manager/src/tests/integration/test_render.py \
  INTEGRATION_ARGS=--use-port-forward
```

Run `make topology` after `make kind-up` before the shared suite. The local
installer profile includes the matching reference template plugin so render,
ZTP, backup, and diagnostics assertions exercise the same mock fabric.
Nautobot-only event and RBAC modules are marked and skipped automatically during
a NetBox run.

The installer plugin and DSX Air tests need `nv-config-manager-installer` from
your checkout, which `make setup` installs; without it they skip:

```bash
.venv/bin/pytest tests/test_plugin.py tests/test_air.py
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
