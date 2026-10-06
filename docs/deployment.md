# Deployment

Running [NVIDIA Config Manager](https://github.com/nvidia/nv-config-manager) with
NetBox as its DCIM provider.

## What the installer does

`nvcm-netbox up`:

1. Creates the kind cluster if it is missing, from NVCM's `deploy/kind-config.yaml`.
   That config maps the Gateway NodePorts (30443/30080) onto host 443/80; a plain
   `kind create cluster` leaves the Gateway unreachable.
2. Loads the two locally-built images into the kind node. Neither is in a
   registry and both are pulled `IfNotPresent`, so the node needs them up front.
3. Writes NVCM's installer config from `deploy/configs/local-netbox.yaml`, filling
   in the API token, image refs and cluster size.
4. Runs a `NetBoxDeployer` derived from NVCM's deployer. Its pre-Helm hook
   deploys NetBox with this provider's plugin, PostgreSQL, Valkey, and the same
   NATS password the common installer provisions.
5. Installs NVCM through the common deployment pipeline.
6. Uses the provider post-Helm hook to run `nvcm_init` (API token, custom fields,
   bootstrap data) and publish NetBox through NVCM's Gateway.
7. Publishes a test event and checks NVCM's render consumer handles it.

Re-running it is safe.

## Requirements

- Docker with ≥24 GB for the `small` profile (`--size medium` / `large` need more)
- `kind`, `kubectl`, `helm`, `uv`
- A checkout of [nv-config-manager](https://github.com/nvidia/nv-config-manager)
- A checkout of [netbox-nv-config-manager](https://github.com/netboxlabs/netbox-nv-config-manager),
  which the NetBox image build installs the plugin from

## Quick start

```bash
export NVCM_REPO=/path/to/nv-config-manager
make setup
make images
.venv/bin/nvcm-netbox up
```

`NVCM_REPO` is read by both `make` and `nvcm-netbox`; `--nvcm-repo` overrides it
for a single run. Without either, both assume `../nv-config-manager`, which only
holds if the repos were cloned side by side.

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
.venv/bin/nvcm-netbox verify
```

The generated config holds a live API token, so it is written `0600` into
`generated/`, which is gitignored. Keep it: it can be used with the standalone
installer commands.

## NetBox image

`make netbox-image` layers the plugin and `netbox-bgp` onto netbox-docker's
published NetBox image. The pins are in
[`build/versions.env`](../build/versions.env).

The base must be a **released** 4.7 tag. A `-betaN` tag still satisfies the
plugin's `min_version` -- NetBox compares `RELEASE.version`, and a beta reports a
plain `4.7.0` -- but the betas differ from the release in ways the plugin depends
on, so its migrations do not apply there. CI's `netbox-pin` job rejects a
pre-release tag and asserts the one pinned exists upstream.

`make version-check` asserts that the pinned version sits inside the plugin's
`min_version`/`max_version`, so bumping one without the other fails before a
build starts rather than at the image's plugin-import assert.

## Doing it by hand

```bash
# 1. NetBox.
helm dependency build deploy/netbox
helm upgrade --install netbox deploy/netbox -n nv-config-manager --create-namespace \
  --set netbox.superuser.apiToken="$TOKEN" --set nvcm.natsPassword="$PW" --wait

# 2. Initialise it.
kubectl -n nv-config-manager exec -i deploy/netbox -c netbox -- sh -s <<EOF
export SUPERUSER_API_TOKEN='$TOKEN'
exec /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py nvcm_init --bootstrap
EOF

# 3. NVCM.
.venv/bin/nvcm-netbox config -o my.yaml --token "$TOKEN"
.venv/bin/nvcm-netbox deploy my.yaml --image-source local \
  --build-images --load-kind --kind-cluster nv-config-manager \
  --install-envoy-gateway --install-cnpg-operator --install-cert-manager

# 4. Publish NetBox through NVCM's Gateway.
helm upgrade netbox deploy/netbox -n nv-config-manager --reuse-values --wait
kubectl -n nv-config-manager get httproute netbox
```

Run steps 1 and 2 before step 3: NVCM authenticates to NetBox with `$TOKEN`.

Step 4 is not optional if you want to reach NetBox at
`https://netbox.<cluster.hostname>`. Its HTTPRoute is skipped during step 1
because rendering one before the Gateway API CRDs exist fails the whole release,
and those CRDs arrive with `--install-envoy-gateway` in step 3 — so the release
has to be upgraded once more afterwards to create it. Nothing else about the
release changes, and `nvcm-netbox up` does this for you.

`$PW` is a NATS password you choose, at least 16 characters matching
`[A-Za-z][A-Za-z0-9]*`. It goes into the install config at
`secrets.k8s.nats.values.password`, which `nvcm-netbox config` fills in,
and NVCM uses it rather than generating its own. That is what lets NetBox be
installed once with the publisher already configured.

`nvcm-netbox deploy` is NVCM's own `deploy` with its arguments forwarded
unchanged, so its flags are documented in
[NVIDIA's repository](https://github.com/nvidia/nv-config-manager). The same
holds for `validate`, `generate-values` and `init`. They run inside your NVCM
checkout, so relative paths in their flags resolve there; pass absolute ones.

`$TOKEN` must be a NetBox v2 token: `nbt_` + a 12-character key + `.` + a
40-character secret, all alphanumeric. Generate one with:

```bash
python -c "from nv_config_manager_dcim_netbox.installer import generate_api_token as g; print(g())"
```

## Host-NetBox development mode

To iterate on the plugin, run NetBox on the host with only NVCM in the cluster.

1. Generate a config and set `dcim.server: http://host.docker.internal:8000` and
   `public_url: http://127.0.0.1:8000`:

   ```bash
   .venv/bin/nvcm-netbox config -o host.yaml
   ```

2. Deploy NVCM with it, then expose the cluster's NATS to the host:

   ```bash
   kubectl port-forward -n nv-config-manager svc/nv-config-manager-nats 4222:4222
   ```

3. In NetBox's `PLUGINS_CONFIG`, set `nats_url: nats://127.0.0.1:4222` and the
   password from the `nats-nv-config-manager` secret. Do not set `nats_tls`; the
   bundled NATS has no TLS and the client will hang on the handshake.

## NetBox permissions

The provider authenticates with a v2 API token (`Authorization: Bearer
nbt_<key>.<secret>`). The installer's token belongs to the superuser the chart
mints (`netbox.superuser`), which is a bring-up convenience and nothing more:
no code path in the provider or the plugin tests `is_superuser`, and the token is
never rotated. Past a lab, point NVCM at a service account holding only the
grants below — the deployment works unchanged, and a leaked token then reads
fabric data instead of owning the NetBox instance.

Grant the token's user:

- **Read** on `dcim` (devices, interfaces, sites, locations, regions,
  device-types, platforms, roles, cables), `ipam` (prefixes, vlans, vrfs,
  ip-addresses, mac-addresses), `tenancy` (tenants), `extras` (tags,
  config-contexts), the plugin models under `plugins/nv-config-manager/`, and
  `netbox-bgp` sessions.
- **Write** on the plugin lifecycle models (device/intended/backup config),
  overlays, vxlans and VRFs (Spectrum-X), interfaces and mac-addresses (DPU
  inventory), devices (serial updates, interface VRF assignment), and cables
  (validation-status custom-field updates).
- **OPTIONS**, used to read field choices such as statuses.

## Troubleshooting

**Provider not discovered, or NVCM cannot reach NetBox.** Check the generated
config has a `dcim.provider_packages` entry. Without it NVCM never installs the
provider.

**Events publish but nothing renders.** Three values must match: the plugin's
`nats_subject` (`nvcm.eventSubject` in `deploy/netbox/values.yaml`), NVCM's
`dcim.event_subject`, and `dcim.event_stream`. Run `nvcm-netbox verify` to test
the path.

**`verify` reports `0 queued ... not enabled`.** The event got through; the device
just is not render-enabled. Enable it:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"device":<id>,"render_enabled":true}' \
  http://127.0.0.1:8080/api/plugins/nv-config-manager/device-config-statuses/
```

**NetBox pod not ready.** Usually memory. Check
`kubectl -n nv-config-manager get pods` and raise Docker's allocation.

## DSX Air

To run this in a simulated fabric rather than a local kind cluster, see
[air-sim.md](air-sim.md).
