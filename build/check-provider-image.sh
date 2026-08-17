#!/usr/bin/env bash
# Assert the provider-package image satisfies NVCM's offline /plugin-wheels contract.
#
# NVCM's init container copies /plugin-wheels out of this image and installs it
# with `pip install --no-index --find-links=<dir>` into the app pods
# (deploy/helm/templates/_helpers.tpl). Nothing in that path fails loudly: an
# incomplete wheelhouse, or wheels built for another architecture, install
# nothing and the pod starts with no provider registered.
#
# Checks, in the order they would bite:
#   1. the wheelhouse carries the SDK and the provider
#   2. no wheel is built for an architecture other than the target
#   3. the offline install actually resolves, on glibc
#   4. the `netbox` entry point is discoverable afterwards
#
# Usage: build/check-provider-image.sh <image> [linux/amd64|linux/arm64]
set -euo pipefail

image="${1:?usage: check-provider-image.sh <image> [platform]}"
platform="${2:-linux/amd64}"

case "${platform}" in
  */amd64) want_arch="x86_64"; other_arch="aarch64" ;;
  */arm64) want_arch="aarch64"; other_arch="x86_64" ;;
  *) echo "unsupported platform: ${platform} (expected linux/amd64 or linux/arm64)" >&2; exit 2 ;;
esac

workdir="$(mktemp -d)"
cleanup() {
  [ -n "${cid:-}" ] && docker rm -f "${cid}" >/dev/null 2>&1
  rm -rf "${workdir}"
}
trap cleanup EXIT

# Copy the wheels out exactly as the init container does, rather than reading
# them in place: this is the step that proves they are reachable at the path
# NVCM expects.
echo "==> extracting /plugin-wheels from ${image}"
cid="$(docker create "${image}")"
docker cp "${cid}:/plugin-wheels" "${workdir}/plugin-wheels"
wheels="${workdir}/plugin-wheels"

count="$(find "${wheels}" -name '*.whl' | wc -l | tr -d ' ')"
[ "${count}" -gt 0 ] || { echo "no wheels in /plugin-wheels" >&2; exit 1; }
echo "    ${count} wheels"

echo "==> the SDK and the provider are both present"
for pkg in nv_config_manager_dcim nv_config_manager_dcim_netbox; do
  if ! find "${wheels}" -name "${pkg}-*.whl" | grep -q .; then
    echo "missing ${pkg} wheel:" >&2
    ls -1 "${wheels}" >&2
    exit 1
  fi
done

echo "==> every wheel targets ${want_arch} (or is pure Python)"
if wrong="$(find "${wheels}" -name "*${other_arch}*.whl" -print)" && [ -n "${wrong}" ]; then
  echo "wheels built for ${other_arch}, but the target is ${platform}:" >&2
  echo "${wrong}" >&2
  echo "A cross-arch wheelhouse installs nothing and the pod starts with no provider." >&2
  exit 1
fi

# glibc, matching NVCM's app runtime. The chart's default installer image is
# musl (python:3.13-alpine), which cannot install these wheels -- so pin the
# same glibc image the deployment docs tell you to set.
echo "==> offline install into python:3.13-slim (${platform})"
docker run --rm --platform "${platform}" \
  -v "${wheels}:/plugin-wheels:ro" \
  python:3.13-slim \
  sh -euc '
    pip install --quiet --no-index --find-links=/plugin-wheels \
      $(ls /plugin-wheels/nv_config_manager_dcim_netbox-*.whl)
    python - <<PY
from nv_config_manager_dcim import discover_dcim_providers
providers = discover_dcim_providers()
assert "netbox" in providers, providers
print("    entry point discovered:", sorted(providers))
PY
  '

echo "==> ${image} satisfies the /plugin-wheels contract for ${platform}"
