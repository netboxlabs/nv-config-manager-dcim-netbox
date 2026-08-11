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

uv pip install -e ".[installer]"
make images        # provider wheelhouse + NetBox with the plugin
nvcm-netbox up --nvcm-repo ../nv-config-manager
```

Running `up` again is safe.

```bash
nvcm-netbox verify                     # re-run the event-loop check
nvcm-netbox up --size medium           # larger NVCM resource profile
nvcm-netbox up --skip-nvcm             # NetBox only
kubectl -n nv-config-manager port-forward svc/netbox 8080:80
```

> **NetBox version.** The plugin needs a GraphQL feature that ships in NetBox
> 4.7, which isn't released yet, so `make images` builds NetBox from a pinned
> commit on the `feature` branch. The pins are in
> [`build/versions.env`](build/versions.env). Once the 4.7 beta is out we can drop
> that step and pull a published image instead.

See [docs/deployment.md](docs/deployment.md) for the manual sequence, the
host-NetBox development mode, and troubleshooting.

## Using NVCM's installer TUI

NVCM has an interactive TUI (`nv-config-manager-installer init`) for writing its
`nv-config-manager-install.yaml`. It can't set up a NetBox deployment on its own.
There's no field for `dcim.provider_packages`, and without that block NVCM never
installs this provider. It doesn't deploy NetBox either.

Generate the config first, then open it in the TUI to change anything else you
need, such as `cluster.size`, SSO, vault or ZTP. `provider_packages` is a real
field in NVCM's schema and just lacks a widget, so the TUI preserves it:

```bash
nvcm-netbox config -o my.yaml
uv run --project ../nv-config-manager/installer \
  nv-config-manager-installer init -c my.yaml     # TUI, pre-populated
nvcm-netbox up --nvcm-repo ../nv-config-manager --config-out my.yaml
```

Use `nvcm-netbox config` on its own if you'd rather run NVCM's installer
yourself. It writes the same file that `up` uses.

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
- [Event delivery](docs/event-delivery.md): how change events reach NVCM
- [Supported operations](docs/supported-operations.mdx): the full operation surface
  by area, and how each maps onto NetBox
- [Render data mapping](docs/netbox-data-mapping.mdx): where each `RenderData`
  field comes from

## License

Apache-2.0. See [LICENSE](LICENSE).
