# NetBox image carrying the NV Config Manager plugin, for the in-cluster deploy.
#
# The base is built by build/netbox-image.sh from the commit pinned in
# build/versions.env (4.7.0-beta1). Everything NetBox-specific -- interpreter,
# dependencies, entrypoints, static -- comes from there; this file only adds the
# two plugins.
#
# Build via `make netbox-image`, or directly:
#
#   docker build -f build/netbox.Dockerfile \
#     --build-arg NETBOX_BASE_IMAGE=nv-config-manager-netbox-base:4.7.0-beta1 \
#     --build-arg NETBOX_BGP_REF=da40b401055ce489d66a1a33c5d97f91100f303e \
#     --build-context plugin=../netbox-nv-config-manager \
#     -t nv-config-manager-netbox:0.1.0 .
#
# ONCE A 4.7 IMAGE IS PUBLISHED: replace the ARG/FROM pair below with
#   FROM ghcr.io/netbox-community/netbox:v4.7.x
# and delete build/netbox-image.sh + build/versions.env. Nothing else here
# changes.

ARG NETBOX_BASE_IMAGE=nv-config-manager-netbox-base:4.7.0-beta1

# The plugin is not published to an index, so it arrives as a named build context
# and is built to a wheel here rather than pip-installed from a path (keeps the
# final image free of the plugin's build deps).
FROM python:3.13-slim AS plugin-build
WORKDIR /build
RUN pip install --no-cache-dir build
COPY --from=plugin . /build/plugin
RUN python -m build --wheel --outdir /wheels /build/plugin

FROM ${NETBOX_BASE_IMAGE} AS main
ARG NETBOX_BGP_REF

COPY --from=plugin-build /wheels/ /tmp/wheels/
# uv, not pip: netbox-docker builds /opt/netbox/venv with `uv venv`, which does
# not put pip inside it. uv ships in the image at /usr/local/bin/uv.
# The [nats] extra pulls nats-py, which the plugin's event publisher imports
# lazily. Without it the chart's `event_publisher: nats` config still loads, and
# every change event is then lost at publish time with ModuleNotFoundError.
#
# The wheel is matched with a shell loop and the count asserted rather than
# interpolated with `"$(echo <glob>)[nats]"`: two matches collapse into a single
# quoted argument (`/tmp/wheels/a.whl /tmp/wheels/b.whl[nats]`) that uv rejects
# with an opaque error, and a cached plugin-build layer holding a wheel from a
# previous version is enough to produce two.
RUN set -eu; \
    wheels=""; count=0; \
    for w in /tmp/wheels/netbox_nv_config_manager-*.whl; do \
      [ -f "$w" ] || continue; \
      wheels="$w"; count=$((count + 1)); \
    done; \
    if [ "$count" -ne 1 ]; then \
      echo "Expected exactly one netbox_nv_config_manager wheel, found $count:" >&2; \
      ls -1 /tmp/wheels >&2; exit 1; \
    fi; \
    uv pip install --python /opt/netbox/venv/bin/python --no-cache \
      "https://github.com/netbox-community/netbox-bgp/archive/${NETBOX_BGP_REF}.tar.gz" \
      "${wheels}[nats]" \
 && rm -rf /tmp/wheels

# Collect the plugins' static assets, then assert NetBox actually loaded both.
# NetBox only sees a plugin's static files when the plugin is enabled, and the
# base image ran collectstatic before these were installed -- so enable them just
# long enough to collect and check, then drop the file. At runtime PLUGINS comes
# from the chart, which mounts its own config over /etc/netbox/config.
COPY build/assert-plugins-load.py /tmp/assert-plugins-load.py
RUN printf "PLUGINS = ['netbox_nv_config_manager', 'netbox_bgp']\n" \
      > /etc/netbox/config/zz-build-plugins.py \
 && export DEBUG=true SECRET_KEY="dummyKeyWithMinimumLength-------------------------" \
 && /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py collectstatic --no-input \
 && /opt/netbox/venv/bin/python /tmp/assert-plugins-load.py \
      netbox_nv_config_manager netbox_bgp \
 && rm /etc/netbox/config/zz-build-plugins.py /tmp/assert-plugins-load.py

# Registers the plugin's extra device statuses. NetBox reads FIELD_CHOICES from
# its configuration before plugins load, so this cannot come from the plugin.
# The chart mounts only configuration.py into this directory, so it survives.
COPY build/netbox-field-choices.py /etc/netbox/config/zz-nvcm-field-choices.py

# Fail the build rather than the cluster if either plugin is unimportable.
RUN /opt/netbox/venv/bin/python -c \
      "import netbox_nv_config_manager, netbox_bgp; print(netbox_nv_config_manager.__version__)"

# Likewise for nats: the publisher imports it lazily, so a missing package only
# shows up as change events silently failing at runtime.
RUN /opt/netbox/venv/bin/python -c "import nats; print('nats-py ok:', nats.__file__)"

# Likewise for the statuses: an image that cannot register them would look fine
# and silently write every device as `active`.
RUN /opt/netbox/venv/bin/python -c \
      "from netbox_nv_config_manager.field_choices import FIELD_CHOICES as c; \
       assert 'dcim.Device.status+' in c, sorted(c); print(sorted(c))"

# netbox-docker creates this account (uid 999, gid 0) but never switches to it,
# so its image runs as root by default. The chart already pins a non-root
# securityContext, but the image should not depend on the caller for that.
# Group 0, not 999: netbox-docker chowns media/reports/scripts to netbox:root
# and makes them group-writable, which is what grants write access.
#
# Last instruction on purpose -- every RUN above writes to /opt/netbox or
# /etc/netbox as root.
USER 999:0
