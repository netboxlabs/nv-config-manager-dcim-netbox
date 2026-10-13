# Deployment

Running [NVIDIA Config Manager](https://github.com/nvidia/nv-config-manager) with
NetBox as its DCIM provider.

## How it fits together

NVCM's installer is pluggable: a DCIM provider package registers an installer
provider through the `nv_config_manager.installer.dcim` entry point, and the
stock `nv-config-manager-installer` composes that provider's steps, Helm values
and screens into its own pipeline. This package's `installer` extra registers
the `netbox` provider. Selecting it is one line in the install config:

```yaml
dcim:
  provider: netbox
  local: true
```

With `dcim.local: true`, `nv-config-manager-installer deploy`:

1. Generates the shared secrets. The DCIM token is a NetBox v2 token
   (`nbt_<key>.<secret>`); the provider seeds it so NVCM's own default is never
   used. The provider also declares the `dcim` database, so NVCM creates the
   `cluster-dcim-app` Secret (`username`, `password`) alongside its own.
2. Installs the operators (`install-crds`), including CloudNativePG with
   `--install-cnpg-operator`.
3. Runs the provider's `install-netbox` and `bootstrap-netbox` steps before the
   umbrella Helm release: `helm upgrade --install` of `deploy/netbox` with that
   token, the NATS password, the event stream/subject from `dcim.settings`, and
   the database, SSO, SPIFFE and monitoring values derived from the install
   config ([below](#parity-with-the-bundled-nautobot)); then `nvcm_init` (API
   token, custom fields, bootstrap data). The chart renders NetBox's PostgreSQL
   as a CNPG `Cluster` bootstrapped from `cluster-dcim-app`.
4. Installs NVCM with Helm values the provider merges in: `dcim.server:
   http://netbox`, `dcim.readiness.path: /login/`, the `netbox` JetStream stream
   and consumer, the provider wheelhouse image, the bundled Nautobot NATS
   account disabled, and the local-DCIM block (`dcim.local.*`, `cnpg.dcim.*`)
   from which NVCM's chart renders NetBox's HTTPRoutes, OIDC-proxy upstream,
   blackbox Probe and CNPG PodMonitor.
5. Runs `verify-netbox-route` after the release: checks that NVCM's chart
   created the `netbox` HTTPRoute and warns if it did not.

Re-running it is safe. The deployer keeps Secrets that already exist, and the
provider reads the deployed DCIM token, NATS password and database user back
before installing NetBox, so NetBox and NVCM stay on the same values.

With `dcim.local: false` and `dcim.server` set, every provider step is skipped
and NVCM is pointed at an existing NetBox.

## Requirements

- Docker with at least 24 GB for the `small` profile (`cluster.size: medium` /
  `large` need more)
- `kind`, `kubectl`, `helm`, `uv`
- A checkout of [nv-config-manager](https://github.com/nvidia/nv-config-manager)
- A checkout of [netbox-nv-config-manager](https://github.com/netboxlabs/netbox-nv-config-manager),
  which the NetBox image build installs the plugin from

The installer provider must be installed into the same Python environment as
`nv-config-manager-installer`: entry points are discovered per environment, so
an installer in one venv cannot see a provider in another. `make setup` installs
both into `.venv`. Check with:

```bash
.venv/bin/nv-config-manager-installer providers list
```

which must list `netbox`.

## Quick start

```bash
export NVCM_REPO=/path/to/nv-config-manager
make setup
make kind-up
```

`make kind-up` builds the two images, creates the kind cluster from NVCM's
`deploy/kind-config.yaml` if it is missing, loads the images into the node, and
runs:

```bash
.venv/bin/nv-config-manager-installer deploy deploy/configs/local-netbox.yaml \
  --project-root $NVCM_REPO --image-source local --build-images --load-kind \
  --kind-cluster nv-config-manager \
  --install-envoy-gateway --install-cnpg-operator --install-cert-manager
.venv/bin/nvcm-netbox verify
```

`--project-root` is where NVCM's Helm chart and image build contexts live. The
NetBox chart and images come from this repository and are not built by the
installer; `make images` builds them and `make kind-up` loads them.

To tune the config first, copy the profile and open it in the installer's wizard.
The External Services screen offers the `netbox` provider and its fields (image
tag, hostname, superuser name):

```bash
cp deploy/configs/local-netbox.yaml my.yaml
.venv/bin/nv-config-manager-installer init -c my.yaml
.venv/bin/nv-config-manager-installer validate my.yaml
make kind-up INSTALL_CONFIG=my.yaml
```

Afterwards:

```bash
kubectl -n nv-config-manager port-forward svc/netbox 8080:80
.venv/bin/nvcm-netbox verify
```

## Provider settings

`dcim.settings` holds the NetBox-specific options. Every key is optional:

| Key | Default | |
| --- | --- | --- |
| `chart` | `deploy/netbox` in this checkout | NetBox Helm chart directory |
| `release` | `netbox` | Helm release name |
| `image_repository` | `nv-config-manager-netbox` | NetBox image (plugin layered on netbox-docker) |
| `image_tag` | the package version | Built by `make netbox-image` |
| `provider_image` | `nv-config-manager-dcim-netbox:<version>` | Wheelhouse, used when `dcim.provider_packages` is empty |
| `hostname` | `netbox.<cluster.hostname>` | Gateway hostname NetBox is published under |
| `superuser_name` | `admin` | |
| `superuser_password` | generated by the chart | |
| `event_stream` / `event_subject` | `netbox` / `netbox.change` | Must equal the chart's `nvcm.eventStream` / `nvcm.eventSubject` |
| `consumer_name` | `nv-config-manager-dcim` | NVCM's durable consumer on that stream |
| `readiness_path` | `/login/` | Polled by NVCM's init containers and by NVCM's blackbox Probe |
| `database` | `cnpg` | `cnpg` renders a CloudNativePG cluster; `bundled` uses the upstream chart's PostgreSQL subchart |
| `sso.username_header` | `HTTP_X_AUTH_REQUEST_EMAIL` | Django META name of the gateway identity header NetBox logs users in from |
| `sso.superuser_groups` | `rbac.admin_roles` minus `all` | SSO groups mapped to `is_superuser` |
| `sso.staff_groups` | `rbac.admin_roles` minus `all` | SSO groups mapped to `is_staff` |
| `sso.default_groups` | `[]` | NetBox groups every SSO user joins |
| `sso.default_permissions` | `{}` | `REMOTE_AUTH_DEFAULT_PERMISSIONS`, passed through |
| `helm_set` | `[]` | Extra `--set key=value` overrides for the NetBox chart |

The chart is bound to the deployment at install time with overrides the
provider derives from the installer: `nvcm.natsService`, `nvcm.natsUser`,
`nvcm.natsPassword`, `nvcm.eventStream`, `nvcm.eventSubject`,
`nvcm.workflowUrl`, and the database, SSO, SPIFFE and monitoring values below.
Strings go through `--set`; lists and mappings through `--set-json`.
`dcim.settings` is the single source of truth for the stream and subject.

## Parity with the bundled Nautobot

The same install-config sections that shape NVCM's bundled Nautobot shape
NetBox. Nothing NetBox-specific has to be set for any of them.

### Database (CloudNativePG)

`deploy/netbox` renders NetBox's PostgreSQL as a CNPG `Cluster` named
`cluster-dcim` (database and owner `dcim`), bootstrapped from the
`cluster-dcim-app` Secret that NVCM's installer creates from the provider's
database contribution. NetBox connects to `cluster-dcim-rw` with the same
Secret. Sizing lives under `nvcm.database.cnpg` in the chart values
(`instances`, `storage`, `resources`, `imageName`); override with
`dcim.settings.helm_set`.

With `infrastructure.cnpg_s3_backup.enabled`, the provider passes the bucket,
path, endpoint and the shared `cnpg-backup-credentials` Secret into
`nvcm.database.cnpg.backup.*`, and the chart adds a `ScheduledBackup` on NVCM's
schedule (`0 0 0 * * *`, 30 days retention). NetBox's backups land under
`s3://<bucket>/<path>/cluster-dcim/`, beside NVCM's.

The CNPG operator must be registered before NetBox installs. NVCM's deploy does
that in its `install-crds` step with `--install-cnpg-operator`; the provider
refuses to install if the `clusters.postgresql.cnpg.io` CRD is missing.

### External PostgreSQL

With `external_services.postgres.enabled` and `dcim_host` set, no CNPG cluster
is rendered and NetBox connects to that host and `port`. Credentials still come
from `cluster-dcim-app`, so create the `dcim` role there with the password the
installer generated (or supply one under `secrets.k8s.postgres`).

### SSO

With `sso.enabled`, NetBox is published behind NVCM's OIDC proxy and configured
for remote-user authentication from the identity headers NVCM's gateway sets:

| NetBox setting | Value |
| --- | --- |
| `REMOTE_AUTH_ENABLED` / `REMOTE_AUTH_BACKEND` | `true` / `netbox.authentication.RemoteUserBackend` |
| `REMOTE_AUTH_HEADER` | `HTTP_X_AUTH_REQUEST_EMAIL` (`sso.username_header`) |
| `REMOTE_AUTH_USER_EMAIL` | `HTTP_X_AUTH_REQUEST_EMAIL` |
| `REMOTE_AUTH_GROUP_HEADER` / `_GROUP_SEPARATOR` | `HTTP_X_AUTH_REQUEST_GROUPS` / `,` |
| `REMOTE_AUTH_AUTO_CREATE_USER` / `_AUTO_CREATE_GROUPS` / `_GROUP_SYNC_ENABLED` | `true` |
| `REMOTE_AUTH_SUPERUSER_GROUPS` / `_STAFF_GROUPS` | `sso.superuser_groups` / `sso.staff_groups` |
| `LOGOUT_REDIRECT_URL` | `/oauth2/sign_out`, so logging out also ends the proxy session |

`X-Auth-Request-Email` is the header both of NVCM's authenticated paths set:
the OIDC proxy on the browser route (where it equals `X-Auth-Request-User`,
oauth2-proxy's default user id claim being `email`) and the gateway's JWT claim
mapping on the `svc-netbox` route. NetBox trusts it because every NVCM route
strips client-supplied identity headers before the proxy or backend sees them.
API clients keep using tokens: requests carrying an `Authorization` header to
`/api/` or `/graphql/` take the bypass route straight to NetBox. Direct
in-cluster calls (`http://netbox`) carry no identity header and authenticate
with tokens or the login form as before.

### Monitoring

With `infrastructure.monitoring.enabled` or `observability_enabled`, NetBox
exposes `/metrics` (`netbox.metrics.enabled`) and the chart renders a
`ServiceMonitor` for it, gated on the `monitoring.coreos.com/v1` API being
registered so a cluster without the Prometheus Operator CRDs still installs.
NVCM's chart adds a blackbox Probe against `dcim.settings.readiness_path` and a
PodMonitor for `cluster-dcim`, labelled like its own.

### SPIFFE

With `spiffe.enabled`, the `netbox` and `netbox-worker` pods mount the workload
API socket read-only at `spiffe.socket_mount_path` (CSI `csi.spiffe.io` for
`spire`, `hostPath` for `teleport`), as NVCM's Nautobot does. NetBox has no
consumer of it yet; the mount is in place for one.

## NetBox image

`make netbox-image` layers the plugin and `netbox-bgp` onto netbox-docker's
published NetBox image. The pins are in
[`build/versions.env`](../build/versions.env).

The base must be a **released** 4.7 tag. A `-betaN` tag still satisfies the
plugin's `min_version` (NetBox compares `RELEASE.version`, and a beta reports a
plain `4.7.0`) but the betas differ from the release in ways the plugin depends
on, so its migrations do not apply there. CI's `netbox-pin` job rejects a
pre-release tag and asserts the one pinned exists upstream.

`make version-check` asserts that the pinned version sits inside the plugin's
`min_version`/`max_version`, so bumping one without the other fails before a
build starts rather than at the image's plugin-import assert.

## NetBox first, NVCM later

`nvcm-netbox netbox up` runs the provider's pre-Helm steps on their own, for a
cluster NVCM will be deployed to afterwards:

```bash
.venv/bin/nvcm-netbox netbox up -c deploy/configs/local-netbox.yaml
.venv/bin/nv-config-manager-installer deploy deploy/configs/local-netbox.yaml \
  --project-root $NVCM_REPO ...
```

It creates the `dcim-token`, `nats-sys`, `nats-nv-config-manager` and
`cluster-dcim-app` Secrets with the names NVCM's deploy uses, adopting any that
already exist, so the later deploy keeps them and both sides share one token
and one database password. `make netbox-up` is the same command. The CNPG
operator has to be in the cluster first (`helm install cnpg
cnpg/cloudnative-pg -n cnpg-system --create-namespace`, or set
`dcim.settings.database: bundled`).

## Doing it by hand

```bash
# 1. NetBox. The chart renders a CNPG cluster bootstrapped from cluster-dcim-app,
#    so the operator and that Secret come first. (Or skip both and add
#    --set nvcm.database.cnpg.enabled=false --set netbox.postgresql.enabled=true
#    for the chart's bundled PostgreSQL.)
helm repo add cnpg https://cloudnative-pg.github.io/charts
helm upgrade --install cnpg cnpg/cloudnative-pg -n cnpg-system --create-namespace --wait
kubectl create namespace nv-config-manager
kubectl -n nv-config-manager create secret generic cluster-dcim-app \
  --from-literal=username=dcim --from-literal=password="$DBPW"
helm dependency build deploy/netbox
helm upgrade --install netbox deploy/netbox -n nv-config-manager \
  --set netbox.superuser.apiToken="$TOKEN" --set nvcm.natsPassword="$PW" --wait

# 2. Initialise it.
kubectl -n nv-config-manager exec -i deploy/netbox -c netbox -- sh -s <<EOF
export SUPERUSER_API_TOKEN='$TOKEN'
exec /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py nvcm_init --bootstrap
EOF

# 3. NVCM, pointed at the NetBox from step 1 rather than installing its own.
#    In my.yaml: dcim.local: false, dcim.server: http://netbox,
#    secrets.k8s.dcim.values.token: $TOKEN, secrets.k8s.nats.values.password: $PW
.venv/bin/nv-config-manager-installer deploy my.yaml --project-root $NVCM_REPO \
  --image-source local --build-images --load-kind --kind-cluster nv-config-manager \
  --install-envoy-gateway --install-cnpg-operator --install-cert-manager
```

With `dcim.local: false` NVCM renders no NetBox route, so reach this NetBox by
port-forward or publish it yourself. With `dcim.local: true` the provider does
steps 1 and 2 and NVCM's release renders the routes.

`$PW` is a NATS password you choose, at least 16 characters matching
`[A-Za-z][A-Za-z0-9]*`. Supplying it in the install config makes NVCM use it
rather than generating its own. `$DBPW` is any password; NVCM reuses the
`cluster-dcim-app` Secret it finds.

`$TOKEN` must be a NetBox v2 token: `nbt_` + a 12-character key + `.` + a
40-character secret, all alphanumeric. The installer validates a supplied one.
Generate one with:

```bash
python -c "from nv_config_manager_dcim_netbox.installer import generate_api_token as g; print(g())"
```

## Host-NetBox development mode

To iterate on the plugin, run NetBox on the host with only NVCM in the cluster.

1. Copy the profile and set `dcim.local: false`, `dcim.server:
   http://host.docker.internal:8000` and `dcim.public_url: http://127.0.0.1:8000`.
   Put the token NetBox holds under `secrets.k8s.dcim.values.token`.

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
grants below. The deployment works unchanged, and a leaked token then reads
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

**`DCIM provider "netbox" is not installed`.** The installer and this package's
`installer` extra are in different environments. Run `make setup`, or
`uv pip install -e $NVCM_REPO/installer -e '.[installer]'` into the same venv, and
check `nv-config-manager-installer providers list`.

**Provider not discovered in the pods, or NVCM cannot reach NetBox.** Check the
config has a `dcim.provider_packages` entry, or that `dcim.settings.provider_image`
names an image loaded into the cluster. Without the wheelhouse NVCM never installs
the provider.

**Events publish but nothing renders.** Three values must match: the plugin's
`nats_subject` (`nvcm.eventSubject` in `deploy/netbox/values.yaml`), NVCM's
stream subject, and the stream name. The provider sets all three from
`dcim.settings.event_subject` / `event_stream`. Run `nvcm-netbox verify -c
<install.yaml>` to test the path.

**`verify` reports `0 queued ... not enabled`.** The event got through; the device
just is not render-enabled. Enable it:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"device":<id>,"render_enabled":true}' \
  http://127.0.0.1:8080/api/plugins/nv-config-manager/device-config-statuses/
```

**NetBox pod not ready.** Usually memory. Check
`kubectl -n nv-config-manager get pods` and raise Docker's allocation. If
`cluster-dcim-1` is not running, check the CNPG operator
(`kubectl -n cnpg-system get pods`) and `kubectl -n nv-config-manager describe
cluster cluster-dcim`.

**`The CloudNativePG operator is not installed`.** The provider checks for the
`clusters.postgresql.cnpg.io` CRD before installing. Deploy with
`--install-cnpg-operator`, install the operator yourself, or set
`dcim.settings.database: bundled` to use the upstream chart's PostgreSQL.

**SSO users land on the login page.** NetBox got no `X-Auth-Request-Email`
header. Confirm the request came through NVCM's gateway (not a port-forward),
that `sso.enabled` was true when NetBox installed, and that the provider
sends the claim; set `dcim.settings.sso.username_header` to
`HTTP_X_AUTH_REQUEST_PREFERRED_USERNAME` if it sends that instead.

**`secrets.method: eso`.** Not supported with `dcim.local: true` yet: the
provider needs the DCIM token and NATS password in the installer's secret state,
which ESO deployments leave to Vault. Use `dcim.local: false` with a NetBox you
installed by hand, or `secrets.method: kubernetes`.

## DSX Air

To run this in a simulated fabric rather than a local kind cluster, see
[air-sim.md](air-sim.md).
