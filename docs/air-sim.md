# DSX Air simulations

Running a simulated fabric in NVIDIA AIR with NVCM and NetBox on it.

NVCM's own installer creates the simulation, boots an NVCM box inside it and
deploys NVCM onto a kind cluster on that box. `nvcm-netbox air up` adds the DCIM
half: it deploys NetBox into the same cluster, loads the simulated fabric into
NetBox, and points NVCM at it.

## What it does

1. Runs NVCM's orchestrator, which creates the sim, boots the NVCM box and
   uploads NVCM's install config.
2. Ships the provider and NetBox images into the sim's kind cluster. Neither is
   in a registry, so both are `docker save`d, uploaded once and `kind load`ed.
3. Deploys NetBox with `deploy/netbox` and runs `nvcm_init` (migrations,
   superuser, API token, custom fields, bootstrap data).
4. Loads the sim's topology into NetBox with `nvcm_load_dcim`. This replaces the
   topology job the bundled-DCIM path runs.
5. Rewrites the install config on the box to select NetBox, then lets NVCM's
   deploy run.
6. Rolls NetBox once more at the end, to pick up the NATS password NVCM created.

## Requirements

- Everything in [deployment.md](deployment.md), plus an AIR account and
  `NGC_API_KEY` in the environment
- The `air` extra, which needs an NVCM checkout:

  ```bash
  export NVCM_REPO=/path/to/nv-config-manager
  make setup-air
  ```

The NVCM checkout must be on **`zb/pluggable-dcim`**. `main` has none of the
pluggable-DCIM framework. The sim clones that branch onto its box too, which
`NetBoxSimConfig` defaults to; NVCM's own `SimConfig` defaults to `main`.

## Running it

```bash
export NGC_API_KEY=<your key>
make images PLATFORM=linux/amd64     # sim boxes are x86_64

.venv/bin/nvcm-netbox air config     # writes ~/.nvcm-air-sim.yaml
.venv/bin/nvcm-netbox air up
```

`PLATFORM=linux/amd64` matters on an Apple Silicon Mac. `make images` builds for
the host, and arm64 images loaded onto an x86_64 box die immediately with
"exec format error". `air up` checks this before shipping anything and stops with
the command to run, rather than letting it surface later as a helm timeout.

The config path matches NVCM's air-sim: `~/.nvcm-air-sim.yaml` by default,
overridden by `$NVCM_AIR_CONFIG` or `--config`, so their documentation applies
here unchanged.

`make setup-air` installs the CLI into the repo's virtualenv rather than on your
PATH, hence the `.venv/bin/` prefix. `source .venv/bin/activate` once and you can
drop it.

`air config` writes only the fields you must choose; everything NetBox needs
already defaults correctly:

| Field | Why it is written |
| --- | --- |
| `mock_blueprint` | `air_trial` (7 devices) or `air_superpod` (24) |
| `deployment_name`, `simulation_name` | Name the sim |
| `oob_ssh_password` | Pinned so you can SSH to the box, and so a re-run matches one already provisioned |

`ngc_api_key` is deliberately absent, so it comes from `$NGC_API_KEY` as the
guide describes. Add it to the file only to override that. The optional `dcim_*`
and `netbox_*` keys are listed commented-out at their defaults.

> **Do not replace it with NVCM's `development/air_sim/configs/air_trial.yaml`.**
> That sets `config_manager_ref: main`, which has no pluggable-DCIM framework, so
> the sim would clone a tree this provider cannot run on. `air up` refuses that
> config rather than deploying something broken.

| Flag | Effect |
| --- | --- |
| `--config`, `-c` | Sim config to use |
| `--token` | Use a specific v2 token instead of generating one |
| `--chart` | NetBox chart directory (defaults to `deploy/netbox`) |
| `--no-blueprint` | Skip the topology load, for a NetBox that already holds the fabric |

Start with `air_trial` (7 devices). `air_superpod` is 24 and needs more headroom
on the NVCM box.

## Accessing it

Exactly as in NVIDIA's
[DSX Air simulation user guide](https://docs.nvidia.com/switch-infrastructure/config-manager/getting-started/dsx-air-simulation-user-guide),
because the sim is theirs: open the SOCKS tunnel printed on the Access panel,
point a browser at it, then use the service URLs. NetBox takes the place of the
bundled DCIM and sits alongside the rest:

| | |
| --- | --- |
| Config Manager UI | `https://nvcm.air` |
| **NetBox** | `https://netbox.nvcm.air` |
| Workflow API | `https://workflow.nvcm.air` |
| Config Store API | `https://config-store.nvcm.air` |
| Render API | `https://render.nvcm.air` |

`air up` publishes NetBox on the sim's Gateway and adds `netbox.nvcm.air` to the
box's `/etc/hosts`, alongside the entries NVCM writes for its own services, so
the name resolves through the same tunnel with nothing extra to configure.

The NetBox admin password and API token are in the `netbox-superuser` secret:

```bash
kubectl -n nv-config-manager get secret netbox-superuser -o jsonpath='{.data.password}' | base64 -d && echo
```

`air up` also prints the API token when it starts, and the box's SSH address, so
you can work on it directly:

```bash
ssh nvcm@<box>            # password is oob_ssh_password from the config
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
kind cluster.

## Cleaning up

In the DSX Air UI, stop or delete the simulation. Deleting it removes the whole
cluster and everything on it, NetBox included.

## Notes

**A Nautobot error in post-deploy is expected.** NVCM's post-deploy tries to
create a demo user in a Nautobot deployment that is not there. It logs a failure
and continues; nothing depends on it.

**The event bus is connected last.** Until NVCM's deploy has created the NATS
password, NetBox publishes change envelopes to its log instead. If the final step
reports it could not connect, the sim is otherwise fine, and re-running
`nvcm-netbox air up` reconciles it.
