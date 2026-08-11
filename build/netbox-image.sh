#!/usr/bin/env bash
# Build the NetBox base image from a pinned `feature`-branch commit.
#
# TEMPORARY -- see build/versions.env. Delete this script once the NetBox 4.7
# beta is pip-installable and build/netbox.Dockerfile can just FROM a published
# image.
#
# Why netbox-docker rather than overlaying source onto a released image: its
# Dockerfile takes ARG NETBOX_PATH and installs ${NETBOX_PATH}/requirements.txt,
# so the interpreter and every dependency come from the ref we pin. The feature
# branch is well ahead of the released images (it runs Python 3.14 / Django 6),
# so overlaying source onto a v4.6.x image would leave the base interpreter
# wrong in a way pip cannot fix.
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
netbox_dir="${workdir}/netbox"

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

echo "==> building ${NETBOX_BASE_IMAGE}"
(
  cd "${docker_dir}"
  # SKIP_GIT: use the tree we pinned above. The branch argument is only used to
  # derive a tag, which we override immediately after.
  SKIP_GIT=1 NETBOX_PATH="${netbox_dir}" \
    DOCKER_TAG="${NETBOX_BASE_IMAGE}" \
    ./build.sh feature
)

echo "==> built ${NETBOX_BASE_IMAGE} (netbox ${NETBOX_REF:0:9})"
