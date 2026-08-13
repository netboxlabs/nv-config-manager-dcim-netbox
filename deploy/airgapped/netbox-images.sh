#!/usr/bin/env bash
# List every image the NetBox chart needs, one per line, digest-pinned.
#
# Rendering the chart rather than maintaining a hand-written list: the bundle
# then cannot silently miss an image added by a chart bump.
#
#   deploy/airgapped/netbox-images.sh [-o FILE]
#
# The bitnami subcharts ship `tag: latest`, which is meaningless offline and
# would drift between building a bundle and installing from it, so floating tags
# are resolved to digests here and the script fails if one cannot be resolved.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CHART="${CHART:-$REPO_ROOT/deploy/netbox}"
OUTPUT=""

while [[ $# -gt 0 ]]; do
    case $1 in
        -o|--output) OUTPUT="$2"; shift 2 ;;
        --chart) CHART="$2"; shift 2 ;;
        -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; exit 1 ;;
    esac
done

command -v helm >/dev/null || { echo "helm is required" >&2; exit 1; }

# `helm template` needs the dependency present; it is vendored in the repo, but
# a fresh clone may not have run `helm dependency build` yet.
if ! compgen -G "$CHART/charts/netbox-*.tgz" >/dev/null; then
    helm dependency build "$CHART" >&2
fi

resolve() {
    # Pin a floating tag to a digest. Local builds have no registry to ask, so
    # they are left alone; the bundle carries them as tarballs.
    local image="$1"
    case "$image" in
        *@sha256:*) echo "$image"; return ;;
    esac
    # A registry host is the first path component, and it always contains a dot
    # or a colon. Anything else is a locally-built image with nothing to ask.
    local first="${image%%/*}"
    if [[ "$first" != *.* && "$first" != *:* ]]; then
        echo "$image"
        return
    fi
    local digest
    digest="$(docker manifest inspect -v "$image" 2>/dev/null \
        | python3 -c 'import json,sys
d = json.load(sys.stdin)
d = d[0] if isinstance(d, list) else d
print(d.get("Descriptor", {}).get("digest", ""))' 2>/dev/null || true)"
    if [[ -z "$digest" ]]; then
        echo "Could not resolve a digest for $image" >&2
        echo "An offline bundle cannot carry a floating tag, so this is fatal." >&2
        echo "Most often Docker Hub's anonymous pull-rate limit: \`docker login\` and retry." >&2
        exit 1
    fi
    echo "${image%%:*}@${digest}"
}

# The provider wheelhouse is in neither chart: NVCM's pods install it from
# dcim.provider_packages in the install config, so nothing renders it and
# neither bundle would carry it otherwise.
PROFILE="${PROFILE:-$REPO_ROOT/deploy/configs/local-netbox.yaml}"
provider_image=""
if [[ -f "$PROFILE" ]]; then
    provider_image="$(grep -oE '^[[:space:]]*image:[[:space:]]*[^[:space:]]+' "$PROFILE" \
        | sed -E 's/^[[:space:]]*image:[[:space:]]*//; s/^"//; s/"$//' | head -1)"
fi

# BSD grep has no \s, so use POSIX classes.
images="$(helm template netbox "$CHART" \
    | grep -oE '^[[:space:]]*image:[[:space:]]*[^[:space:]]+' \
    | sed -E 's/^[[:space:]]*image:[[:space:]]*//; s/^"//; s/"$//' \
    | sort -u)"

out=()
while read -r image; do
    [[ -n "$image" ]] || continue
    # Ours, built by `make images`. They exist in no registry, so there is no
    # digest to resolve and the bundle has to carry them as tarballs.
    if [[ "$image" == nv-config-manager-* ]]; then
        out+=("$image")
        continue
    fi
    # `busybox:1.38.0` and `docker.io/busybox:1.38.0` are the same image.
    [[ "$image" == */* ]] || image="docker.io/$image"
    out+=("$(resolve "$image")")
done <<< "$images"

[[ -n "$provider_image" ]] && out+=("$provider_image")

printf '%s\n' "${out[@]}" | sort -u | { [[ -n "$OUTPUT" ]] && tee "$OUTPUT" || cat; }
