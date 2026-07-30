# Provider-package image for the NetBox DCIM provider.
#
# Referenced from an NVCM installer config as `dcim.provider_packages[].image`.
# NVCM's contract (deploy/helm/templates/_helpers.tpl): the image must contain
# the provider AND all its dependencies as wheels under **/plugin-wheels/**. An
# init container copies them out and installs them OFFLINE
# (`pip install --no-index --find-links=<dir> *.whl`) into the app pods.
#
# Because the install is offline, /plugin-wheels must carry every transitive
# dependency (aiohttp[speedups], pydantic-core, netaddr, ...). The wheels are
# built glibc/manylinux (cp313) to match NVCM's glibc app runtime, so the
# offline installer must also run on glibc — set
# `dcim.providerPackages.installerImage: python:3.13-slim` (the chart default of
# python:3.13-alpine is musl and cannot install/serve these wheels to a glibc app).
#
# The SDK (`nv-config-manager-dcim`) is not yet on an index, so it is supplied
# via a named build context. Build with:
#
#   docker build --build-context sdk=../nv-config-manager/components/dcim \
#     -t nv-config-manager-dcim-netbox:0.1.0 .
#
# or `make image`.

FROM python:3.13-slim AS build
WORKDIR /build
RUN pip install --no-cache-dir build
# Build the SDK + provider wheels...
COPY --from=sdk . /build/sdk
COPY . /build/provider
RUN python -m build --wheel --outdir /pkg /build/sdk \
 && python -m build --wheel --outdir /pkg /build/provider \
 # ...then collect them + every transitive dependency as wheels into /plugin-wheels
 # (--find-links /pkg resolves the not-yet-published SDK from the local build).
 && pip wheel --no-cache-dir --find-links /pkg --wheel-dir /plugin-wheels /pkg/*.whl

# Final image only needs a shell (the copy init container runs /bin/sh: ls/mkdir/cp)
# plus the wheels; the wheels themselves are glibc, carried as plain files.
FROM alpine:3.20
LABEL org.opencontainers.image.title="nv-config-manager-dcim-netbox" \
      org.opencontainers.image.description="NetBox DCIM provider package (offline wheels) for NVIDIA Config Manager"
COPY --from=build /plugin-wheels/ /plugin-wheels/
# Fail the build if the wheelhouse is empty or missing the provider wheel.
RUN ls /plugin-wheels/*.whl >/dev/null \
 && ls /plugin-wheels/ | grep -q "nv_config_manager_dcim_netbox"
