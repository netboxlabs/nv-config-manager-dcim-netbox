# Provider-package image for the NetBox DCIM provider.
#
# Referenced from an NVCM installer config as a `dcim.provider_packages[].image`.
# The image ships the provider (and, until it is published to an index, the SDK)
# installed into site-packages so the deployment can import
# `nv_config_manager_dcim_netbox` and resolve the `netbox` entry point.
#
# The SDK (`nv-config-manager-dcim`) is not yet on an index, so it is supplied
# via a named build context rather than pip. Build with:
#
#   docker build \
#     --build-context sdk=../nv-config-manager/components/dcim \
#     -t registry.example.com/nv-config-manager-dcim-netbox:0.1.0 .
#
# or simply `make image` (see the Makefile).

FROM python:3.13-slim AS build
WORKDIR /build
RUN pip install --no-cache-dir build
# SDK (from the named build context) + this provider -> wheels.
COPY --from=sdk . /build/sdk
COPY . /build/provider
RUN python -m build --wheel --outdir /wheels /build/sdk \
 && python -m build --wheel --outdir /wheels /build/provider

FROM python:3.13-slim
LABEL org.opencontainers.image.title="nv-config-manager-dcim-netbox" \
      org.opencontainers.image.description="NetBox DCIM provider for NVIDIA Config Manager"
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl \
 && rm -rf /wheels
# Sanity check: the entry point resolves.
RUN python -c "from importlib.metadata import entry_points; \
eps = entry_points(group='nv_config_manager.dcim'); \
assert any(e.name == 'netbox' for e in eps), 'netbox entry point missing'"
