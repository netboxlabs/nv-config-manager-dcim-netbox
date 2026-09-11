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

index_digest() {
    # The digest of the image *index*, not of one platform's manifest inside it.
    #
    # `docker manifest inspect -v` returns one entry per platform for a
    # multi-arch image, and taking the first of them pinned amd64's manifest.
    # `docker pull --platform linux/arm64 foo@<amd64 manifest digest>` then
    # fetches the amd64 image, and `docker save --platform linux/arm64` fails
    # with "no suitable export target found for platform" -- so the bundle could
    # not be built for arm64 at all, and an amd64 bundle was right only by
    # accident.
    #
    # Both readers are build-host requirements already: buildx for `make image`,
    # skopeo for NVCM's create-airgapped.sh. Either is asked, in that order,
    # because a host may have one authenticated and not the other.
    local image="$1" digest=""

    digest="$(docker buildx imagetools inspect --format '{{.Manifest.Digest}}' \
        "$image" 2>/dev/null || true)"
    case "$digest" in
        sha256:*) printf '%s' "$digest"; return 0 ;;
    esac

    # A manifest's digest is the hash of its own bytes, so the raw fetch gives
    # the index digest without skopeo having to pick a platform (it cannot: this
    # host is darwin, and `skopeo inspect` without --raw fails on an index that
    # has no darwin entry).
    local raw
    raw="$(skopeo inspect --raw "docker://$image" 2>/dev/null || true)"
    if [[ -n "$raw" ]]; then
        digest="$(printf '%s' "$raw" | shasum -a 256 | cut -d' ' -f1)"
        [[ -n "$digest" ]] && { printf 'sha256:%s' "$digest"; return 0; }
    fi
    return 0
}

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
    digest="$(index_digest "$image")"
    if [[ -z "$digest" ]]; then
        echo "Could not resolve a digest for $image" >&2
        echo "An offline bundle cannot carry a floating tag, so this is fatal." >&2
        echo "Most often Docker Hub's anonymous pull-rate limit: \`docker login\` and retry." >&2
        echo "Needs docker buildx or skopeo on PATH to read the index digest." >&2
        exit 1
    fi
    # Strip the tag, not the first colon: `myreg.example.com:5000/foo:bar` would
    # otherwise become `myreg.example.com@sha256:...`. Only the last path
    # component can carry a tag, and it may legitimately carry none.
    local base="$image"
    case "${image##*/}" in
        *:*) base="${image%:*}" ;;
    esac
    echo "${base}@${digest}"
}

# The provider wheelhouse is in neither chart: NVCM's pods install it from
# dcim.provider_packages in the install config, so nothing renders it and
# neither bundle would carry it otherwise.
# Missing it is not a bundle with one image fewer: NVCM installs the provider
# from that image, so without it the DCIM consumer starts and handles nothing.
# So both a missing profile and a profile with no `image:` in it are fatal here
# rather than an empty string quietly dropped at the end.
PROFILE="${PROFILE:-$REPO_ROOT/deploy/configs/local-netbox.yaml}"
if [[ ! -f "$PROFILE" ]]; then
    echo "No installer profile at $PROFILE" >&2
    echo "It names the provider wheelhouse image, which is in neither chart." >&2
    echo "Point PROFILE at the install config this bundle is for." >&2
    exit 1
fi
provider_image="$(grep -oE '^[[:space:]]*image:[[:space:]]*[^[:space:]]+' "$PROFILE" \
    | sed -E 's/^[[:space:]]*image:[[:space:]]*//; s/^"//; s/"$//' | head -1)"
if [[ -z "$provider_image" ]]; then
    echo "No dcim.provider_packages[].image in $PROFILE" >&2
    echo "Without it NVCM never installs the provider and the DCIM consumer" >&2
    echo "starts but handles nothing, so an incomplete bundle is not built." >&2
    exit 1
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

out+=("$provider_image")

[[ ${#out[@]} -gt 0 ]] || { echo "The chart rendered no images at all" >&2; exit 1; }

# Written with a redirect rather than piped through `tee`: in a pipeline a `tee`
# that cannot open its file exits 1, the `|| cat` fallback then drains an already
# consumed stdin, and the pipeline still reports success -- so a missing output
# directory looked like a bundle with no images in it. The directory is created
# here because the Makefile target writes into gitignored generated/.
if [[ -n "$OUTPUT" ]]; then
    mkdir -p "$(dirname "$OUTPUT")"
    printf '%s\n' "${out[@]}" | sort -u > "$OUTPUT"
    cat "$OUTPUT"
else
    printf '%s\n' "${out[@]}" | sort -u
fi
