# DSX Air simulations

Running a simulated fabric in NVIDIA AIR with NVCM and NetBox on it.

NVCM's installer owns the simulation: `nv-config-manager-installer air-sim`
creates the sim, boots an NVCM box inside it and deploys NVCM onto a kind cluster
on that box. Its wizard and orchestrator call the selected DCIM provider's AIR
hooks at fixed points, and this package supplies the NetBox hooks. Selecting them
is one line in the sim config:

```yaml
dcim_provider: netbox
```

## What the hooks do

1. **Before the sim is allocated**, build the provider and NetBox images for
   `linux/amd64` (`prepare_images`). Docker's layer cache makes an unchanged
   rebuild quick while still picking up source changes under an existing tag. A
   separate `make images` step is neither required nor recommended.
2. **When NVCM's install config is generated**, write `dcim: {provider: netbox,
   local: true, settings: ...}` into it (`install_settings`), with the chart path
   pointing at where step 3 unpacks it on the box.
3. **Before NVCM deploys**, ship both images and the chart into the sim over SSH,
   `kind load` them, install NetBox with `deploy/netbox` already holding a NATS
   password, run `nvcm_init`, and load the sim's topology into NetBox with
   `nvcm_load_dcim` (`pre_deploy`). This runs as one uploaded script in a single
   SSH session.
4. **After NVCM deploys**, add `netbox.nvcm.air` to the box's `/etc/hosts`
   alongside NVCM's own names (`gateway_hostnames`), wait for NetBox to hold
   intended configs for the render-enabled devices (`wait_for_configs`), and
   publish NetBox on the sim's Gateway with a values-preserving Helm pass now that
   the Gateway API CRDs exist (`post_deploy`).
5. **While the dashboard runs**, report switch provisioning from NetBox's
   `DeviceConfigStatus` records (`provisioning_status`).

## Requirements

- Everything in [deployment.md](deployment.md), plus an AIR account and
  `NGC_API_KEY` in the environment
- The `installer` extra in the same environment as `nv-config-manager-installer`,
  which is what `make setup` does:

  ```bash
  export NVCM_REPO=/path/to/nv-config-manager
  make setup
  .venv/bin/nv-config-manager-installer providers list   # must list netbox
  ```

The sim clones NVCM's `config_manager_ref` onto its box (`main` by default).
That checkout must carry the pluggable installer, and the box's installer
environment must have this package's `installer` extra for `dcim.provider:
netbox` to resolve there. See NOTES.md for the current state of that.

## Running it

```bash
export NGC_API_KEY=<your key>
cp deploy/configs/air-trial.yaml air-trial.yaml

.venv/bin/nv-config-manager-installer air-sim init -c air-trial.yaml    # guided setup and launch
.venv/bin/nv-config-manager-installer air-sim deploy -c air-trial.yaml  # headless
```

The wizard is NVCM's shared Air wizard. With `dcim_provider: netbox` its
Topology screen shows the NetBox population switch (load the generated fabric
into NetBox), and its Launch screen tracks the `netbox` pod, reports
provisioning from NetBox and links to `https://netbox.nvcm.air`. To use the
internal `air-inside` service, turn off **Use Public DSX Air** on the Options
screen; the equivalent YAML setting is `use_internal: true`.

The disposable AIR deployment logs into the NetBox UI with `demo` / `demo` by
default. Override `superuser_name` and `superuser_password` under
`dcim_settings` when needed. Non-AIR deployments use `admin` and a password the
chart generates.

### Sim config

[`deploy/configs/air-trial.yaml`](../deploy/configs/air-trial.yaml) is NVCM's
air-sim format with `dcim_provider: netbox`. The NetBox options live under
`dcim_settings` and are validated by `NetBoxAirSettings`; every key is optional:

| Key | Default | |
| --- | --- | --- |
| `image_repository`, `image_tag` | `nv-config-manager-netbox`, the package version | Built for `linux/amd64` before the sim is allocated |
| `provider_image` | `nv-config-manager-dcim-netbox:<version>` | The wheelhouse, built the same way |
| `token` | generated | NetBox v2 token to install; otherwise one is minted |
| `superuser_name`, `superuser_password` | `demo`, `demo` | |
| `load_blueprint` | `true` | Load the sim's topology with `nvcm_load_dcim`; off leaves an empty NetBox, which renders nothing |
| `helm_set` | `[]` | Extra `--set` arguments for the NetBox chart |

`ngc_api_key` is deliberately absent from the example, so it comes from
`$NGC_API_KEY`. Add it to the file only to override that.

The plugin layer is built from the ref in
[`build/versions.env`](../build/versions.env) (`NETBOX_PLUGIN_REF`, `main` by
default). Set it to a commit to make a simulation reproducible, or point
`NETBOX_NVCM_PLUGIN_REPO` at a local checkout to build what you are editing.
`NVCM_NETBOX_SKIP_IMAGE_BUILD=1` ships the images already built.

Start with `air_trial` (7 devices). `air_superpod` is 24 and needs more headroom
on the NVCM box.

## Accessing it

Everything on the sim is reached through **one SOCKS tunnel**, not one per
service. Open it with:

```bash
make tunnel                    # or: .venv/bin/nvcm-netbox air tunnel
```

It takes no arguments and needs no secrets on the command line. AIR allocates the
box's SSH host and port per simulation, so they change every run and cannot be
written down, but the sim config already names the simulation, which AIR can be
asked about, and already holds `oob_ssh_password`. The password is handed to ssh
down a pipe, so it never reaches a command line, a file, or the terminal.

| Flag | `make` | Effect |
| --- | --- | --- |
| `--config`, `-c` | `CONFIG=` | Sim config to read; defaults to `$NVCM_AIR_CONFIG`, then `~/.nvcm-air-sim.yaml` |
| `--print` | `PRINT=1` | Print the command instead of opening the tunnel (prompting form, no password in it) |
| `--browser` | `BROWSER=1` | Also launch a Chromium-family browser through the tunnel |
| `--socks-port` | `SOCKS_PORT=` | Move it off 8080, which is a busy port locally |
| `--url` | | What `--browser` opens. Defaults to NetBox's |

Three things about the tunnel are worth knowing, because they are what goes wrong:

- **DNS must resolve remotely.** `nvcm.air` and `netbox.nvcm.air` exist only in
  the *box's* `/etc/hosts`. A SOCKS proxy resolves names at the far end, which is
  why this works at all. Chrome does that for `socks5://` by default; in Firefox
  you must enable `network.proxy.socks_remote_dns`, or the names will not resolve.
- **The certificate is internal.** The Gateway's cert is issued by the sim's own
  CA, so a browser will refuse it until told not to (`--browser` passes
  `--ignore-certificate-errors`, scoped to a throwaway profile).
- **`make port-forward` in NVCM's repo is a different thing.** It is
  `kubectl port-forward` against your *current kubeconfig*, a local kind cluster,
  and it forwards `svc/nautobot`, which this deployment does not have. It cannot
  reach a sim.

With the tunnel up, the service URLs work as in NVIDIA's
[DSX Air simulation user guide](https://docs.nvidia.com/switch-infrastructure/config-manager/getting-started/dsx-air-simulation-user-guide),
because the sim is theirs. NetBox takes the place of the bundled DCIM and sits
alongside the rest:

| | |
| --- | --- |
| Config Manager UI | `https://nvcm.air` |
| **NetBox** | `https://netbox.nvcm.air` |
| Workflow API | `https://workflow.nvcm.air` |
| Config Store API | `https://config-store.nvcm.air` |
| Render API | `https://render.nvcm.air` |

The tunnel also works for the API (`socks5h` rather than `socks5`, so curl
resolves the name remotely too):

```bash
curl -k --proxy socks5h://localhost:8080 \
  -H "Authorization: Bearer $TOKEN" https://netbox.nvcm.air/api/dcim/devices/?limit=1
```

The NetBox admin password and API token are in the `netbox-superuser` secret on
the box:

```bash
ssh nvcm@<box>            # password is oob_ssh_password from the config
kubectl -n nv-config-manager get secret netbox-superuser -o jsonpath='{.data.api_token}' | base64 -d && echo
kubectl -n nv-config-manager get pods    # NetBox and NVCM, no bundled DCIM
```

## Checking it

The guide's verification steps apply, reading NetBox where it says the bundled
DCIM:

1. NetBox holds the demo site, devices, interfaces, IPs, cables, roles and
   platforms. `/api/status/` lists `netbox_nv_config_manager` and `netbox_bgp`,
   and the device count matches the blueprint.
2. Config Manager's workflow forms are available.
3. The Config Store holds intended configs for `tan-leaf-01` .. `tan-leaf-05`.
4. Those TAN leaves show status **Provisioning**, then **Provisioned** once NVCM
   has pushed to them. The image registers those statuses with NetBox; without
   that they would silently read `Active`.

`nvcm-netbox verify` exercises the change-event loop the same way it does for a
kind cluster; run it on the box, or through the tunnel with a kubeconfig that
points at the sim.

## Workflow Support

The demo is for Ethernet and Cumulus Linux workflows, and the same ones NVIDIA
lists apply here. NetBox replaces the DCIM they read and write; the workflows
themselves are NVCM's and are driven the same way.

Worth trying: Configuration Backup, Configuration Deploy, Batch Deploy,
Multi-Device Deploy, Tenant Deploy (scoped to demo devices with tenant data),
Cable Validation, Hardware Validation, Device Reprovision, and the lifecycle
actions (approve, reject, retry, terminate).

Not usable in the sim, for reasons that have nothing to do with the DCIM:

| Workflow family | Why |
| --- | --- |
| InfiniBand | DSX Air does not simulate the InfiniBand fabric for this demo |
| NVLink switch | DSX Air does not simulate NVLink switches for this demo |
| Switch OS Upgrade | DSX Air cannot complete the ONIE install path it uses |

### Try Multi-Deploy

A good first Day-2 workflow, because the trial has five TAN leaves that should
receive the same diff. NVIDIA's example appends a DNS server to the site config
context; in NetBox that is a config context object, and the flow is the same.

The demo context already carries `8.8.8.8`. Append `8.8.4.4`.

`data` is a single JSON field, so PATCHing it replaces the whole object. Read it,
change the one key, write it back. Overwriting it wholesale drops `ztp`,
`management_prefixes` and `provisioning_servers`, and the DHCP service then
crashloops with `No ZTP server found ... in DHCP context`:

```bash
TOKEN=$(kubectl -n nv-config-manager get secret netbox-superuser \
          -o jsonpath='{.data.api_token}' | base64 -d)
API=https://netbox.nvcm.air/api/extras/config-contexts
CTX=$(curl -sk -H "Authorization: Bearer $TOKEN" \
        "$API/?q=network-management-services" \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["results"][0]["id"])')

curl -sk -H "Authorization: Bearer $TOKEN" "$API/$CTX/" \
  | python3 -c 'import json,sys
ctx = json.load(sys.stdin)["data"]
ctx.setdefault("dns", {}).setdefault("ipv4", []).append("8.8.4.4")
print(json.dumps({"data": ctx}))' \
  | curl -sk -X PATCH -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
      --data-binary @- "$API/$CTX/"
```

Editing it in the NetBox UI is safer, since the form round-trips the whole
object. The plugin publishes an `extras.configcontext` change, this provider
resolves the context's scope to the devices it applies to, the render service
produces new intended configs, and Multi-Deploy groups the identical diffs for
approval.

To watch it happen:

```bash
kubectl -n nv-config-manager logs -l 'component in (consumer-dcim,consumer-nautobot)' -c consumer -f
```

Renders bump `commit_id` on the affected devices:

```bash
curl -sk -H "Authorization: Bearer $TOKEN" \
  "https://netbox.nvcm.air/api/plugins/nv-config-manager/intended-configs/"
```

## Cleaning up

In the DSX Air UI, stop or delete the simulation. Deleting it removes the whole
cluster and everything on it, NetBox included.
