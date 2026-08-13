# NetBox image carrying the NV Config Manager plugin, for the in-cluster deploy.
#
# The base is built by build/netbox-image.sh from a pinned `feature`-branch commit
# (see build/versions.env for why). Everything NetBox-specific -- interpreter,
# dependencies, entrypoints, static -- comes from there; this file only adds the
# two plugins.
#
# Build via `make netbox-image`, or directly:
#
#   docker build -f build/netbox.Dockerfile \
#     --build-arg NETBOX_BASE_IMAGE=nv-config-manager-netbox-base:feature \
#     --build-arg NETBOX_BGP_VERSION=0.19.0 \
#     --build-context plugin=../netbox-nv-config-manager \
#     -t nv-config-manager-netbox:0.1.0 .
#
# POST-4.7-BETA: replace the ARG/FROM pair below with
#   FROM ghcr.io/netbox-community/netbox:v4.7.x
# and delete build/netbox-image.sh + build/versions.env. Nothing else here changes.

ARG NETBOX_BASE_IMAGE=nv-config-manager-netbox-base:feature

# The plugin is not published to an index, so it arrives as a named build context
# and is built to a wheel here rather than pip-installed from a path (keeps the
# final image free of the plugin's build deps).
FROM python:3.13-slim AS plugin-build
WORKDIR /build
RUN pip install --no-cache-dir build
COPY --from=plugin . /build/plugin
RUN python -m build --wheel --outdir /wheels /build/plugin

FROM ${NETBOX_BASE_IMAGE} AS main
ARG NETBOX_BGP_VERSION

COPY --from=plugin-build /wheels/ /tmp/wheels/
# uv, not pip: netbox-docker builds /opt/netbox/venv with `uv venv`, which does
# not put pip inside it. uv ships in the image at /usr/local/bin/uv.
RUN uv pip install --python /opt/netbox/venv/bin/python --no-cache \
      "netbox-bgp==${NETBOX_BGP_VERSION}" \
      /tmp/wheels/netbox_nv_config_manager-*.whl \
 && rm -rf /tmp/wheels

# Collect the plugins' static assets. NetBox only sees a plugin's static files
# when the plugin is enabled, and the base image ran collectstatic before these
# were installed -- so enable them just long enough to collect, then drop the
# file. At runtime PLUGINS comes from the chart, which mounts its own config
# over /etc/netbox/config.
RUN printf "PLUGINS = ['netbox_nv_config_manager', 'netbox_bgp']\n" \
      > /etc/netbox/config/zz-build-plugins.py \
 && DEBUG=true SECRET_KEY="dummyKeyWithMinimumLength-------------------------" \
      /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py collectstatic --no-input \
 && rm /etc/netbox/config/zz-build-plugins.py

# Registers the plugin's extra device statuses. NetBox reads FIELD_CHOICES from
# its configuration before plugins load, so this cannot come from the plugin.
# The chart mounts only configuration.py into this directory, so it survives.
COPY build/netbox-field-choices.py /etc/netbox/config/zz-nvcm-field-choices.py

# Fail the build rather than the cluster if either plugin is unimportable.
RUN /opt/netbox/venv/bin/python -c \
      "import netbox_nv_config_manager, netbox_bgp; print(netbox_nv_config_manager.__version__)"

# Likewise for the statuses: an image that cannot register them would look fine
# and silently write every device as `active`.
RUN /opt/netbox/venv/bin/python -c \
      "from netbox_nv_config_manager.field_choices import FIELD_CHOICES as c; \
       assert 'dcim.Device.status+' in c, sorted(c); print(sorted(c))"
