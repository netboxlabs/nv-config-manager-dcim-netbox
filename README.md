# NetBox DCIM Provider for NVIDIA Config Manager

The **NetBox** implementation of the DCIM provider API for
[NVIDIA Config Manager](https://github.com/nvidia/nv-config-manager) (NVCM) —
NVIDIA's network configuration platform for AI/HPC fabrics.

It translates NetBox's REST and GraphQL APIs, data model, and change events into
the provider-neutral contracts of the `nv-config-manager-dcim` SDK, so NVCM can
use NetBox as its source of truth. NVCM services never import it directly — they
discover it through the `nv_config_manager.dcim` entry point and talk to it only
through the SDK's `DCIMClient` protocol.

It works with the companion plugin
[`netbox-nv-config-manager`](https://github.com/netboxlabs/netbox-nv-config-manager),
which adds the fabric/overlay models NVCM needs and publishes NetBox object
changes onto NVCM's event bus.

## Deploying

`nvcm-netbox` brings up a complete environment — NetBox in-cluster as the DCIM
provider, NVCM alongside it — and verifies the change-event loop end to end. It
handles the parts NVCM's own installer cannot: deploying NetBox, generating the
API token both sides share, writing an installer config containing the
`dcim.provider_packages` block that installs this provider into NVCM's pods, and
connecting NetBox to NVCM's NATS bus once that exists.

NVCM's Helm chart and images are not published anywhere, so **a checkout of
[nv-config-manager](https://github.com/nvidia/nv-config-manager) is required**;
the installer drives NVCM's own CLI against it.

Prerequisites: Docker (≥24 GB for the `small` profile), `kind`, `kubectl`, `helm`,
`uv`.

```bash
git clone https://github.com/nvidia/nv-config-manager
git clone https://github.com/netboxlabs/netbox-nv-config-manager
git clone https://github.com/netboxlabs/nv-config-manager-dcim-netbox
cd nv-config-manager-dcim-netbox

uv pip install -e ".[installer]"
make images        # provider wheelhouse + NetBox with the plugin
nvcm-netbox up --nvcm-repo ../nv-config-manager
```

`up` deploys NetBox, initialises it, generates the NVCM installer config, runs
NVCM's installer, wires NetBox to the event bus, then checks that a change event
reaches the render consumer. Re-running it is safe.

```bash
nvcm-netbox verify                     # re-run the event-loop check
nvcm-netbox up --size medium           # larger NVCM resource profile
nvcm-netbox up --skip-nvcm             # NetBox only
kubectl -n nv-config-manager port-forward svc/netbox 8080:80
```

> **NetBox version.** The plugin needs the plugin-extensible GraphQL API landing
> in NetBox 4.7, which is unreleased — so `make images` builds NetBox from a
> pinned `feature` commit. Pins live in
> [`build/versions.env`](build/versions.env); when the 4.7 beta ships this
> collapses to a single `FROM` line.

See [docs/deployment.md](docs/deployment.md) for the manual sequence, the
host-NetBox development mode, and troubleshooting.

## Using NVCM's installer TUI

NVCM has an interactive TUI (`nv-config-manager-installer init`) that authors its
`nv-config-manager-install.yaml`. You can use it, but **not on its own** for a
NetBox deployment: it has no field for `dcim.provider_packages`, and without that
block this provider is never installed into NVCM's pods and discovery fails. It
also does not deploy NetBox.

So generate the config first, then open it in the TUI to adjust anything else
(`cluster.size`, SSO, vault, ZTP). `provider_packages` is a real field in their
schema, just without a widget, so it survives the round-trip:

```bash
nvcm-netbox config -o my.yaml
uv run --project ../nv-config-manager/installer \
  nv-config-manager-installer init -c my.yaml     # TUI, pre-populated
nvcm-netbox up --nvcm-repo ../nv-config-manager --config-out my.yaml
```

`nvcm-netbox config` is also the way in if you would rather drive NVCM's
installer yourself — the file it writes is exactly what `up` feeds them.

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

- [Deployment](docs/deployment.md) — the installer, manual bring-up, troubleshooting
- [Event delivery](docs/event-delivery.md) — the change-event path and handler fan-out
- [Supported operations & NetBox semantics](docs/supported-operations.mdx) — the
  full operation surface by area, and NetBox-specific mappings
- [NetBox render data mapping](docs/netbox-data-mapping.mdx) — `RenderData` field
  → NetBox source

## Support

This is a NetBox Labs private commercial plugin. Customers should use
their existing NetBox Labs support channel. Internal maintainers can
file issues in this repository.

## License

Apache-2.0. See [LICENSE](LICENSE).
