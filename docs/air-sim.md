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
  make setup-air NVCM=../nv-config-manager
  ```

The NVCM checkout must be on **`zb/pluggable-dcim`**. `main` has none of the
pluggable-DCIM framework. The sim clones that branch onto its box too, which
`NetBoxSimConfig` defaults to; NVCM's own `SimConfig` defaults to `main`.

## Running it

Write a sim config. It is NVCM's `air-sim` YAML plus this provider's keys:

```yaml
# air-trial.yaml
mock_blueprint: air_trial          # or air_superpod
deployment_name: air-trial
size: small
ngc_api_key: ""                    # or set NGC_API_KEY

# Optional; these are the defaults.
dcim_server: http://netbox
netbox_image: nv-config-manager-netbox
netbox_image_tag: "0.1.0"
dcim_provider_package_image: nv-config-manager-dcim-netbox:0.1.0
load_blueprint: true
```

Then:

```bash
make images
nvcm-netbox air up -c air-trial.yaml
```

| Flag | Effect |
| --- | --- |
| `--token` | Use a specific v2 token instead of generating one |
| `--chart` | NetBox chart directory (defaults to `deploy/netbox`) |
| `--no-blueprint` | Skip the topology load, for a NetBox that already holds the fabric |

Start with `air_trial` (7 devices). `air_superpod` is 24 and needs more headroom
on the NVCM box.

## Checking it

The command prints the API token and the box's SSH address. On the box:

```bash
kubectl -n nv-config-manager get pods           # NetBox and NVCM, no bundled DCIM
kubectl -n nv-config-manager port-forward svc/netbox 8080:80
```

Then `/api/status/` should list `netbox_nv_config_manager` and `netbox_bgp`, and
the device count should match the blueprint.

`nvcm-netbox verify` exercises the change-event loop the same way it does for a
kind cluster.

## Notes

**A Nautobot error in post-deploy is expected.** NVCM's post-deploy tries to
create a demo user in a Nautobot deployment that is not there. It logs a failure
and continues; nothing depends on it.

**The event bus is connected last.** Until NVCM's deploy has created the NATS
password, NetBox publishes change envelopes to its log instead. If the final step
reports it could not connect, the sim is otherwise fine, and re-running
`nvcm-netbox air up` reconciles it.
