#!/usr/bin/env bash
# Build the NetBox base image from the commit pinned in build/versions.env
# (4.7.0-beta1 at the time of writing).
#
# TEMPORARY -- see build/versions.env. Delete once a 4.7 image is published and
# build/netbox.Dockerfile can FROM it.
#
# Why netbox-docker rather than overlaying source onto a released image: its
# Dockerfile takes ARG NETBOX_PATH and installs ${NETBOX_PATH}/requirements.txt,
# so the interpreter and every dependency come from the ref we pin. 4.7 runs
# Python 3.14 / Django 6, so overlaying source onto a v4.6.x image would leave
# the base interpreter wrong in a way pip cannot fix.
#
# Usage: build/netbox-image.sh [workdir]   (default workdir: .build)
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${here}/versions.env"

workdir="${1:-${here}/../.build}"
mkdir -p "${workdir}"
workdir="$(cd "${workdir}" && pwd)"

docker_dir="${workdir}/netbox-docker"
# Inside the netbox-docker clone, not beside it: its Dockerfile does
# `COPY ${NETBOX_PATH} /opt/netbox`, and COPY cannot reach outside the build
# context (which is that clone). `.netbox` is netbox-docker's own default.
netbox_rel=".netbox"
netbox_dir="${docker_dir}/${netbox_rel}"

echo "==> netbox-docker ${NETBOX_DOCKER_REF}"
if [ ! -d "${docker_dir}/.git" ]; then
  git clone -q --depth 1 --branch "${NETBOX_DOCKER_REF}" \
    https://github.com/netbox-community/netbox-docker.git "${docker_dir}"
else
  git -C "${docker_dir}" fetch -q --depth 1 origin "${NETBOX_DOCKER_REF}"
  git -C "${docker_dir}" checkout -q FETCH_HEAD
fi

# Check NetBox out ourselves at an exact SHA. netbox-docker's own clone only
# handles branches/tags, so we manage the tree and set SKIP_GIT to stop it
# touching what we prepared.
echo "==> netbox ${NETBOX_REF}"
if [ ! -d "${netbox_dir}/.git" ]; then
  git init -q "${netbox_dir}"
  git -C "${netbox_dir}" remote add origin https://github.com/netbox-community/netbox.git
fi
git -C "${netbox_dir}" fetch -q --depth 1 origin "${NETBOX_REF}"
git -C "${netbox_dir}" checkout -q FETCH_HEAD

# netbox-docker's preflight (build-functions/check-commands.sh) requires skopeo
# unconditionally, but every skopeo call sits behind `if [ -n "$GH_ACTION" ]` --
# the registry checks that decide whether a push is needed. An interactive build
# sets SHOULD_BUILD=true and never calls it. Rather than make skopeo a
# prerequisite for a code path we do not take, satisfy the preflight with a stub
# that fails loudly if it is ever actually invoked.
stub_dir=""
if ! command -v skopeo >/dev/null 2>&1; then
  stub_dir="$(mktemp -d)"
  trap 'rm -rf "${stub_dir}"' EXIT
  cat > "${stub_dir}/skopeo" <<'STUB'
#!/bin/sh
echo "skopeo was actually invoked by netbox-docker, which this build does not expect." >&2
echo "It is only used for registry checks under GH_ACTION. Install skopeo" >&2
echo "(brew install skopeo) and re-run." >&2
exit 1
STUB
  chmod +x "${stub_dir}/skopeo"
  echo "==> skopeo not installed; stubbing it for netbox-docker's preflight"
fi

# netbox-docker defaults to linux/amd64. kind's nodes match the host, so on an
# arm64 machine that image would only run under emulation. Build for the host
# unless BUILDX_PLATFORM says otherwise.
case "$(uname -m)" in
  arm64 | aarch64) host_platform="linux/arm64" ;;
  *) host_platform="linux/amd64" ;;
esac
BUILDX_PLATFORM="${BUILDX_PLATFORM:-${host_platform}}"
export BUILDX_PLATFORM

case "${NETBOX_BASE_IMAGE}" in
  *:*) base_name="${NETBOX_BASE_IMAGE%:*}"; base_tag="${NETBOX_BASE_IMAGE##*:}" ;;
  *)   base_name="${NETBOX_BASE_IMAGE}";    base_tag="latest" ;;
esac

echo "==> building ${base_name}:${base_tag} for ${BUILDX_PLATFORM}"
(
  cd "${docker_dir}"
  [ -n "${stub_dir}" ] && PATH="${stub_dir}:${PATH}" && export PATH
  # build.sh composes ${IMAGE_NAMES}:${DOCKER_TAG}, so the two halves are passed
  # separately; handing it a full "name:tag" as DOCKER_TAG yields an image
  # reference with two colons, which docker rejects. It also always emits a
  # second, version-suffixed tag (…:<tag>-5.0.2) that we simply ignore.
  #
  # SKIP_GIT: use the tree we pinned above. The ref argument then only supplies
  # a default TAG, which DOCKER_TAG overrides.
  SKIP_GIT=1 NETBOX_PATH="${netbox_rel}" \
    IMAGE_NAMES="${base_name}" \
    DOCKER_TAG="${base_tag}" \
    ./build.sh "${NETBOX_REF}"
)

echo "==> built ${NETBOX_BASE_IMAGE} (netbox ${NETBOX_REF:0:9})"
