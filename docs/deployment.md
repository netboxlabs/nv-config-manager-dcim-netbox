# Deployment

Running [NVIDIA Config Manager](https://github.com/nvidia/nv-config-manager) with
NetBox as its DCIM provider.

## What the installer does

NVCM's own installer expects an external DCIM provider to be stood up separately.
`nvcm-netbox` fills that gap and sequences the whole thing:

1. **Deploys NetBox** in-cluster from `deploy/netbox` — a thin wrapper over the
   official [netbox chart](https://github.com/netbox-community/netbox-chart) that
   enables this provider's plugin and `netbox_bgp`, with bundled PostgreSQL and
   Valkey so NetBox does not depend on NVCM's datastores.
2. **Initialises NetBox** by running the plugin's `nvcm_init` (migrations,
   superuser, API token, custom fields, bootstrap data).
3. **Generates NVCM's installer config** from `deploy/configs/local-netbox.yaml`,
   filling in the API token, image refs and cluster size — including the
   `dcim.provider_packages` block that installs this provider into NVCM's pods and
   that NVCM's TUI cannot express.
4. **Runs NVCM's installer** (`validate` then `deploy`) against your checkout.
5. **Connects NetBox to the event bus** — NVCM's deployer creates the NATS
   password, so NetBox is rolled a second time to pick it up.
6. **Verifies** that a change event reaches NVCM's DCIM render consumer.

Order matters. NetBox goes first because it owns the API token NVCM authenticates
with; the NATS password only exists after NVCM is deployed, which is why NetBox is
rolled twice. Until it has that password the publisher stays on the `log` backend
rather than failing.

The installer **shells out to NVCM's CLI** and imports nothing from
`nv_config_manager_installer`. The coupling is their CLI and their config-file
schema, both far more stable than their Python internals.

## Requirements

- Docker with ≥24 GB for the `small` profile (`--size medium` / `large` need more)
- `kind`, `kubectl`, `helm`, `uv`
- A checkout of [nv-config-manager](https://github.com/nvidia/nv-config-manager) —
  its chart and images are unpublished, so this cannot be skipped
- A checkout of [netbox-nv-config-manager](https://github.com/netboxlabs/netbox-nv-config-manager)
  (the NetBox image build installs the plugin from it)

## Quick start

```bash
uv pip install -e ".[installer]"
make images
nvcm-netbox up --nvcm-repo ../nv-config-manager
```

Useful flags:

| Flag | Effect |
| --- | --- |
| `--size {small,medium,large}` | NVCM resource profile (`cluster.size`) |
| `--namespace` | Target namespace (default `nv-config-manager`) |
| `--token` | Use a specific v2 token instead of generating one |
| `--skip-nvcm` | Deploy NetBox only, and print the NVCM command to run |
| `--skip-verify` | Skip the closing event-loop check |
| `--config-out` | Where to write the generated installer config |

Afterwards:

```bash
kubectl -n nv-config-manager port-forward svc/netbox 8080:80
nvcm-netbox verify
```

The generated config contains a live API token and is written `0600` into
`generated/` (gitignored). Keep it — it is what you would re-run NVCM's installer
with.

## NetBox image

The plugin needs the plugin-extensible GraphQL API (`graphql_type_extensions`)
landing in NetBox 4.7. That is unreleased and absent from every published image,
so `make netbox-image` builds NetBox from a pinned `feature` commit using
[netbox-docker](https://github.com/netbox-community/netbox-docker), then layers
the plugin and `netbox-bgp` on top.

Pins live in [`build/versions.env`](../build/versions.env). netbox-docker installs
`requirements.txt` from the ref being built, so the interpreter and dependencies
come from that commit rather than from a mismatched release image.

**When the 4.7 beta ships** and is pip-installable: delete `build/netbox-image.sh`
and `build/versions.env`, and change the first line of `build/netbox.Dockerfile`
to `FROM ghcr.io/netbox-community/netbox:v4.7.x`. Nothing else changes — the
plugin install is deliberately a separate layer.

## Doing it by hand

`up` is a sequence of ordinary commands; run them yourself if you want to stop
partway.

```bash
# 1. NetBox. No NATS password yet, so the publisher starts on the `log` backend.
helm dependency build deploy/netbox
helm upgrade --install netbox deploy/netbox -n nv-config-manager --create-namespace \
  --set netbox.superuser.apiToken="$TOKEN" --wait

# 2. Initialise it (idempotent, advisory-locked).
kubectl -n nv-config-manager exec -i deploy/netbox -c netbox -- sh -s <<EOF
export SUPERUSER_API_TOKEN='$TOKEN'
exec /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py nvcm_init --bootstrap
EOF

# 3. NVCM.
nvcm-netbox config -o my.yaml --token "$TOKEN"
uv run --project ../nv-config-manager/installer \
  nv-config-manager-installer deploy my.yaml --image-source local \
  --build-images --load-kind --kind-cluster nv-config-manager \
  --install-envoy-gateway --install-cnpg-operator --install-cert-manager

# 4. Point NetBox at the bus NVCM just created.
PW=$(kubectl -n nv-config-manager get secret nats-nv-config-manager \
       -o jsonpath='{.data.password}' | base64 -d)
helm upgrade netbox deploy/netbox -n nv-config-manager --reuse-values \
  --set nvcm.natsPassword="$PW"
```

`$TOKEN` must be a NetBox v2 token — `nbt_` + a 12-character key + `.` + a
40-character secret, all alphanumeric. The plugin silently skips installing a
malformed one. Generate one with
`python -c "from nv_config_manager_dcim_netbox.installer import generate_api_token as g; print(g())"`.

## Host-NetBox development mode

For fast plugin iteration you may prefer NetBox on the host, with only NVCM in the
cluster:

```bash
nvcm-netbox config -o host.yaml            # then edit dcim.server
```

Set `dcim.server: http://host.docker.internal:8000` and `public_url:
http://127.0.0.1:8000`, deploy NVCM with that config, then point your host NetBox
at the cluster's NATS with `kubectl port-forward -n nv-config-manager
svc/nv-config-manager-nats 4222:4222` and a `PLUGINS_CONFIG` block setting
`nats_url: nats://127.0.0.1:4222`. The bundled NATS uses password auth with **no
TLS** — setting `nats_tls` makes the client hang on the handshake.

## NetBox permissions

The provider authenticates with a **v2 API token** (`Authorization: Bearer
nbt_<key>.<secret>`; a non-`nbt_` token falls back to the v1 `Token` scheme). The
installer's generated token belongs to the superuser, which is fine for a lab. For
production, scope a token's user to:

- **Read** across what the render/inventory/reference paths consume: `dcim`
  (devices, interfaces, sites, locations, regions, device-types, platforms, roles,
  cables), `ipam` (prefixes, vlans, vrfs, ip-addresses, mac-addresses), `tenancy`
  (tenants), `extras` (tags, config-contexts), the plugin models under
  `plugins/nv-config-manager/` (overlays, vxlans, overlay-assignments,
  device-config-statuses, intended-configs, backup-configs), and `netbox-bgp`
  sessions.
- **Write** where the provider mutates NetBox: the plugin lifecycle models
  (device/intended/backup config — upsert), overlays + vxlans + VRFs (Spectrum-X
  provisioning), interfaces + mac-addresses (DPU inventory), and devices (serial
  updates, interface VRF assignment).
- **OPTIONS**, which the provider issues to read field choices (statuses).

## Troubleshooting

**Provider not discovered / NVCM cannot reach NetBox.** Check the generated config
has a `dcim.provider_packages` entry. Without it the wheelhouse image is never
installed into NVCM's pods and the `netbox` entry point does not exist. This is
the one thing NVCM's TUI cannot author.

**Events publish but nothing renders.** Three values must agree: the plugin's
`nats_subject` (`nvcm.eventSubject` in `deploy/netbox/values.yaml`), NVCM's
`dcim.event_subject`, and the JetStream stream NVCM provisions from
`dcim.event_stream`. `nvcm-netbox verify` checks the whole path; a test asserts the
first two cannot drift.

**`verify` reports `0 queued ... not enabled`.** That still proves the full path —
the event reached the consumer and dispatched through this provider. The device
simply is not render-enabled. Set `render_enabled` on its `DeviceConfigStatus`:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"device":<id>,"render_enabled":true}' \
  http://127.0.0.1:8080/api/plugins/nv-config-manager/device-config-statuses/
```

**NetBox pod not ready.** The bundled PostgreSQL and Valkey start first; on a
constrained Docker VM this is usually memory. Check
`kubectl -n nv-config-manager get pods` and raise Docker's allocation.
