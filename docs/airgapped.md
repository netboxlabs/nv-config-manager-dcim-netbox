# Air-gapped installation

NVCM ships an air-gapped bundle. This adds NetBox to it, so one tarball carries
both and the target environment needs no internet access.

## Building the bundle

On a machine with network access and the images already built:

```bash
make images                                  # provider wheelhouse + NetBox
export NVCM_REPO=../nv-config-manager
make airgapped                               # or: deploy/airgapped/create-airgapped.sh --version v0.1.0
```

This calls NVCM's own `create-airgapped.sh` rather than reimplementing it, and
forwards every argument, so all its flags work:

```bash
deploy/airgapped/create-airgapped.sh --version v0.1.0 --arch amd64 --ngc-api-key "$NGC_API_KEY"
```

The result is `generated/nv-config-manager-netbox-airgapped-<version>-<arch>.tar.gz`,
their bundle plus `netbox-chart/` and `install-netbox.sh`.

### Which images go in

Rather than a hand-written list, the images are read off the rendered chart, so
a chart bump cannot silently leave one out:

```bash
make airgapped-images     # writes generated/netbox-extra-images.txt
```

Six images: NetBox with the plugin, the provider wheelhouse, Postgres, Valkey,
busybox and rancher/kubectl. The last four are pinned to digests, because the
Bitnami subcharts ship `tag: latest`, which means nothing offline and would drift
between building the bundle and installing from it. The build fails rather than
bundling a floating tag.

The provider wheelhouse is included explicitly. It appears in neither chart,
since NVCM's pods install it from `dcim.provider_packages` in the install config,
so nothing renders it and it would otherwise be missed.

## Installing

Upload the images and chart as NVCM's documentation describes, then install
NetBox **first**. It issues the API token NVCM authenticates with, so NVCM
cannot come up correctly before it exists.

```bash
tar -xzf nv-config-manager-netbox-airgapped-v0.1.0-amd64.tar.gz
cd nv-config-manager-netbox-airgapped-v0.1.0-amd64

./upload-to-registry.sh --registry registry.example.com/nv-config-manager \
  --chart-registry registry.example.com/nv-config-manager/charts \
  --username '<user>' --password-stdin

./install-netbox.sh --registry registry.example.com/nv-config-manager
```

That prints an API token. Put it in `install.yaml` under
`secrets.k8s.dcim.values.token`, then deploy NVCM:

```bash
./installer/nv-config-manager-installer deploy install.yaml \
  --chart-dir helm \
  --image-source registry \
  --install-envoy-gateway \
  --install-cert-manager \
  --install-cnpg-operator
```

Finally connect NetBox to NVCM's event bus, which cannot happen earlier because
the NATS password does not exist until NVCM is deployed:

```bash
PW=$(kubectl -n nv-config-manager get secret nats-nv-config-manager \
       -o jsonpath='{.data.password}' | base64 -d)
helm upgrade netbox netbox-chart -n nv-config-manager --reuse-values \
  --set nvcm.natsPassword="$PW"
```

`install-netbox.sh` prints these last two steps as well.

## install.yaml

Whether you write it by hand or with `nvcm-netbox init`, three things must be
true or NVCM will not use NetBox:

```yaml
dcim:
  provider: netbox
  server: http://netbox
  provider_packages:
    - name: netbox
      image: registry.example.com/nv-config-manager/nv-config-manager-dcim-netbox:0.1.0
services:
  nautobot: false
```

Without the `provider_packages` entry NVCM never installs the provider, and the
DCIM consumer starts but handles nothing.

## Notes

`install-netbox.sh` repoints every image in the chart, not just the main one.
The init and `waitForBackend` containers default to `docker.io` and would
otherwise be the only thing reaching for the internet.

It also sets `global.security.allowInsecureImages`. That is Bitnami's guard
against running their charts with non-Bitnami registries; pushing their images
into a private registry is exactly what an air-gapped install does, and the
chart refuses to render without it.

The chart's `helm test` hook references `busybox` from Docker Hub directly and
is not repointed. Nothing creates that Pod during an install, so it does not
affect an offline deployment, but `helm test` will not work.

The merge step depends on NVCM's bundle layout (`helm/`, `images/`,
`installer/`). It checks for those directories and stops with a clear message if
they move, rather than producing a bundle that is quietly wrong.
