# Air-gapped installation

NVCM ships an air-gapped bundle. This adds NetBox to it, so one tarball carries
both and the target environment needs no internet access.

## Building the bundle

On a machine with network access, with the images built for the architecture the
bundle is for:

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

The result is `generated/nv-config-manager-netbox-airgapped-<version>-<arch>.tar.gz`
and its `.sha256`, their bundle plus `netbox-chart/` and `install-netbox.sh`.

### One architecture per bundle

NVCM's script defaults to `--arch both`. This one defaults to the build host's
instead, because the two images it adds are built by `make images` one
architecture at a time -- a Docker tag holds one -- so half of a `both` bundle
would carry images the target cannot execute. Build one bundle per architecture:

```bash
make images PLATFORM=linux/amd64
deploy/airgapped/create-airgapped.sh --version v0.1.0 --arch amd64
```

The build checks the local images before running theirs, and stops with that
command line rather than failing several minutes in with "no suitable export
target found for platform".

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

The digest is the *index* digest, not one platform's manifest inside it.
`docker manifest inspect -v` answers with one entry per platform, and taking the
first of them pinned amd64 -- which pulls the amd64 image whatever `--arch` says
and then cannot be exported for arm64 at all.

The provider wheelhouse is included explicitly. It appears in neither chart,
since NVCM's pods install it from `dcim.provider_packages` in the install config,
so nothing renders it and it would otherwise be missed. A profile without one is
an error rather than a bundle with five images: NVCM would install no provider
and the DCIM consumer would start and handle nothing.

### Staging, before their script runs

Neither kind of image this bundle adds can be pulled the way their script pulls
the rest, so both are staged into the local Docker store first and handed over
with their `--local-image-fallback`:

- **Ours** (`nv-config-manager-netbox`, `nv-config-manager-dcim-netbox`) exist in
  no registry at all; `make images` built them locally.
- **The pinned ones** are re-tagged `<repo>:sha256-<digest>`. A digest reference
  survives neither step of their pipeline: `docker save --platform` reports "no
  suitable export target found for platform", and both its Skopeo fallback and
  `upload-to-registry.sh` reading the archive back reject "reference does not
  include a tag". The tag is exactly what `upload-to-registry.sh` pushes a digest
  reference to and what `install-netbox.sh` then asks the chart for, so the
  private registry still ends up holding an immutable tag rather than a floating
  `latest`.

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

If that registry needs credentials to pull from -- it usually does, since the
upload above authenticated to it -- either name a pull secret that already
exists or let the install create one:

```bash
./install-netbox.sh --registry registry.example.com/nv-config-manager \
  --registry-username '<user>' --registry-password-stdin
# or, reusing NVCM's:
./install-netbox.sh --registry registry.example.com/nv-config-manager \
  --pull-secret registry-credentials
```

One `netbox.global.imagePullSecrets` covers all six pod specs, NetBox's and the
Bitnami subcharts'. Without it every pod sits in `ImagePullBackOff` with
`unauthorized`, which reads like a missing image rather than a missing secret.

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
the NATS password does not exist until NVCM is deployed. The same upgrade also
creates NetBox's HTTPRoute: it is skipped at install time because the Gateway API
CRDs arrive with NVCM, so do not omit this step even if you set
`nvcm.natsPassword` by hand earlier.

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

Everything else about the file is NVCM's, and their air-gapped requirements still
apply on top of these three -- `cluster.airgapped: true`, `images.source:
registry` and an `images.registry` pointing at the same namespace
`upload-to-registry.sh` uploaded to, so the installer uses the bundled
dependency charts and manifests rather than reaching for them. See their
[air-gapped bundle README](https://github.com/dsx-ai-factory/nv-config-manager/blob/main/deploy/airgapped/README.md)
for the full set.

## Notes

`install-netbox.sh` repoints every image in the chart, not just the main one.
The init and `waitForBackend` containers default to `docker.io` and would
otherwise be the only thing reaching for the internet.

It also sets `global.security.allowInsecureImages`. That is Bitnami's guard
against running their charts with non-Bitnami registries; pushing their images
into a private registry is exactly what an air-gapped install does, and the
chart refuses to render without it.

The chart's `helm test` hook is the one image built without a registry value:
`templates/tests/test-connection.yaml` renders
`{{ .Values.test.image.repository }}:{{ .Values.test.image.tag }}` and nothing
else, so the registry has to be carried in the repository instead. It is, so
`helm test` works offline too. Nothing creates that Pod during an install either
way.

The merge step depends on NVCM's bundle layout (`helm/`, `images/`,
`installer/`). It checks for those directories and stops with a clear message if
they move, rather than producing a bundle that is quietly wrong.
