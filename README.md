# NetBox DCIM Provider for NVIDIA Config Manager

The **NetBox** implementation of the DCIM provider API for
[NVIDIA Config Manager](https://github.com/nvidia/nv-config-manager) (NVCM),
NVIDIA's network configuration platform for AI/HPC fabrics. It lets NVCM use
NetBox as its source of truth.

It works with the companion plugin
[`netbox-nv-config-manager`](https://github.com/netboxlabs/netbox-nv-config-manager),
which adds the fabric/overlay models NVCM needs and publishes NetBox object
changes onto NVCM's event bus.

## Deploying

`nvcm-netbox` stands up a working environment in one command. NVCM's own
installer only deploys NVCM, so this fills in the rest: it deploys NetBox into
the cluster, creates the API token both sides need, runs NVCM's installer,
connects NetBox to NVCM's event bus, then checks that a change in NetBox reaches
the render service.

You need Docker (≥24 GB for the `small` profile), `kind`, `kubectl`, `helm` and
`uv`, plus a local clone of the NVCM repo.

```bash
git clone -b zb/pluggable-dcim https://github.com/nvidia/nv-config-manager
git clone https://github.com/netboxlabs/netbox-nv-config-manager
git clone https://github.com/netboxlabs/nv-config-manager-dcim-netbox
cd nv-config-manager-dcim-netbox

# Where your NVCM checkout is. Both `make` and `nvcm-netbox` read it, so set it
# once; without it they assume ../nv-config-manager.
export NVCM_REPO=../nv-config-manager

make setup         # venv, the SDK from your NVCM checkout, and the CLI
make images        # provider wheelhouse + NetBox with the plugin
make kind-up       # or: .venv/bin/nvcm-netbox up
```

`make setup` rather than `pip install -e .` because the
`nv-config-manager-dcim` SDK is not published; it is installed from
`$NVCM_REPO/components/dcim`.

`up` creates the kind cluster if it does not already exist, using NVCM's
`deploy/kind-config.yaml` (which maps the Gateway's NodePorts onto host 443/80;
a default `kind create cluster` does not). Pass `--no-create-cluster` to require
an existing one, and `--no-kind-load` when targeting a real cluster.

Running `up` again is safe.

```bash
.venv/bin/nvcm-netbox verify           # re-run the event-loop check
.venv/bin/nvcm-netbox up --size medium # larger NVCM resource profile
.venv/bin/nvcm-netbox up --skip-nvcm   # NetBox only
make kind-down                         # delete the cluster
kubectl -n nv-config-manager port-forward svc/netbox 8080:80
```

> **NetBox version.** The plugin needs a GraphQL feature that ships in NetBox
> 4.7, which isn't released yet, so `make images` builds NetBox from a pinned
> commit on the `feature` branch. The pins are in
> [`build/versions.env`](build/versions.env). Once the 4.7 beta is out we can drop
> that step and pull a published image instead.

See [docs/deployment.md](docs/deployment.md) for the manual sequence, the
host-NetBox development mode, and troubleshooting.

## Step by step

`up` is the whole sequence in one command. To drive it yourself, `nvcm-netbox`
takes the same commands and flags as NVCM's own installer, so anything in
[NVIDIA's documentation](https://github.com/nvidia/nv-config-manager) works here
with `nvcm-netbox` in place of `nv-config-manager-installer`:

```bash
nvcm-netbox up --skip-nvcm             # cluster, NetBox, API token, config
nvcm-netbox init                       # optional: edit the config in NVCM's TUI
nvcm-netbox validate  generated/nv-config-manager-install.yaml
nvcm-netbox deploy    generated/nv-config-manager-install.yaml \
  --image-source local --build-images --load-kind \
  --kind-cluster nv-config-manager \
  --install-envoy-gateway --install-cnpg-operator --install-cert-manager
nvcm-netbox verify
```

`validate`, `generate-values` and `deploy` hand their arguments straight to
NVCM's installer, so every flag it accepts works unchanged. `deploy` deploys
NVCM only, exactly as theirs does, which is why NetBox has to be up first: NetBox
issues the API token NVCM authenticates with.

`init` opens NVCM's TUI on the config. The wizard has no field for the provider
wheelhouse image or the API token, so `nvcm-netbox` writes those before it opens
and puts them back afterwards. Everything else, `cluster.size`, SSO, vault, ZTP,
is yours to change there.

One caveat: those commands run inside your NVCM checkout, so a relative
`--output-dir` or `--chart-dir` resolves against *that* directory. Pass absolute
paths.

## Development

```bash
make setup     # venv + SDK, templates, provider, dev tools
make check     # ruff + unit tests
make test      # unit tests only
```

Per-area suites match the functional modules: `make inventory`, `make ipam`,
`make ib`, `make render`, and so on (`make help` lists them all).

To work against a live NetBox without a cluster, see
[docs/deployment.md](docs/deployment.md#host-netbox-development-mode).

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
