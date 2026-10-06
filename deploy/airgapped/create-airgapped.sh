#!/usr/bin/env bash
# Build an air-gapped bundle carrying both NVCM and NetBox.
#
#   deploy/airgapped/create-airgapped.sh --version v0.1.0 --arch amd64
#
# NVCM's own script does the heavy lifting. It is called, not modified, with our
# images passed through its `--extra-images-file` hook; the chart is added to the
# tarball afterwards, because its `charts.config` is read from its own directory
# with no override and adding ours would mean editing their repo.
#
# Every argument is forwarded to their script, so all of its flags work
# (--arch, --ngc-api-key, --skip-images, --runtime, ...).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
NVCM_REPO="${NVCM_REPO:-$REPO_ROOT/../nv-config-manager}"
OUTPUT_DIR="${OUTPUT_DIR:-$REPO_ROOT/generated}"
CHART="$REPO_ROOT/deploy/netbox"

log()  { printf '\033[0;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[0;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

NVCM_REPO="$(cd "$NVCM_REPO" 2>/dev/null && pwd)" \
    || die "No nv-config-manager checkout at $NVCM_REPO. Set NVCM_REPO."
THEIRS="$NVCM_REPO/deploy/airgapped/create-airgapped.sh"
[[ -x "$THEIRS" ]] || die "Not found or not executable: $THEIRS"

# --- what their script was asked to do ---------------------------------------
#
# Read, not consumed: every argument is still forwarded. Three answers change
# what has to be staged before they run.
ARCH=""
SKIP_IMAGES=false
HAS_LOCAL_FALLBACK=false
args=("$@")
for i in "${!args[@]}"; do
    case "${args[$i]}" in
        --arch) ARCH="${args[$((i + 1))]:-}" ;;
        --skip-images) SKIP_IMAGES=true ;;
        --local-image-fallback) HAS_LOCAL_FALLBACK=true ;;
    esac
done

# Their default is `both`, and the images this bundle adds are built locally by
# `make images`, one architecture at a time -- a Docker tag holds one. So an
# unasked-for `both` fails halfway through on whichever architecture was not
# built. Default to this host's instead, which is what `make images` produced.
FORWARD=("$@")
if [[ -z "$ARCH" ]]; then
    case "$(uname -m)" in
        x86_64|amd64) ARCH=amd64 ;;
        arm64|aarch64) ARCH=arm64 ;;
        *) die "Unknown host architecture $(uname -m); pass --arch amd64|arm64" ;;
    esac
    log "No --arch given; building for this host ($ARCH)"
    FORWARD+=(--arch "$ARCH")
fi
if [[ "$ARCH" == "both" && "$SKIP_IMAGES" != true ]]; then
    die "--arch both cannot work here: the NetBox and provider images are built one
       architecture at a time (a Docker tag holds one), so one half of the bundle
       would carry images the target cannot execute. Build one bundle per
       architecture, each after \`make images PLATFORM=linux/<arch>\`."
fi

mkdir -p "$OUTPUT_DIR"

log "Collecting the images the NetBox chart needs"
PINNED="$OUTPUT_DIR/netbox-extra-images.txt"
"$SCRIPT_DIR/netbox-images.sh" -o "$PINNED" >/dev/null
log "$(wc -l < "$PINNED" | tr -d ' ') images -> $PINNED"

# --- stage the images their script will save ---------------------------------
#
# Two kinds are in that list and neither can be pulled the way their script
# pulls the rest, so both are staged here and handed over with
# --local-image-fallback:
#
#   ours       (nv-config-manager-netbox, nv-config-manager-dcim-netbox) exist
#              in no registry at all; `make images` built them locally.
#
#   pinned     everything from a registry is pinned to its index digest, and a
#              digest reference survives neither step of their pipeline:
#              `docker save --platform` reports "no suitable export target found
#              for platform", and its Skopeo fallback -- and `upload-to-registry.sh`
#              reading the archive back -- reject "reference does not include a
#              tag". Pulled by digest here and tagged `<repo>:sha256-<digest>`,
#              which is exactly the tag upload-to-registry.sh pushes to and
#              install-netbox.sh asks the chart for, so the private registry
#              still holds an immutable tag rather than a floating `latest`.
EXTRA_IMAGES="$OUTPUT_DIR/netbox-extra-images.staged.txt"

is_registry_ref() {
    # A registry host is the first path component and always contains a dot or a
    # colon -- and is followed by one, so `nv-config-manager-netbox:0.1.0` is a
    # bare name carrying a tag, not a host carrying a port. Anything without a
    # host is one of ours, built locally.
    local first="${1%%/*}"
    [[ "$1" == */* ]] || return 1
    [[ "$first" == *.* || "$first" == *:* ]]
}

stage_images() {
    : > "$EXTRA_IMAGES"
    while read -r image; do
        [[ -n "$image" ]] || continue

        if ! is_registry_ref "$image"; then
            if ! docker image inspect "$image" >/dev/null 2>&1; then
                die "No local image $image. Build it first:
       make images${ARCH:+ PLATFORM=linux/$ARCH}"
            fi
            local got
            got="$(docker image inspect --format '{{.Architecture}}' "$image")"
            [[ "$got" == "$ARCH" ]] || die \
                "Local image $image is $got, but this bundle is for $ARCH.
       Rebuild it: make images PLATFORM=linux/$ARCH"
            echo "$image" >> "$EXTRA_IMAGES"
            continue
        fi

        case "$image" in
            *@sha256:*) ;;
            *) echo "$image" >> "$EXTRA_IMAGES"; continue ;;
        esac

        local repo="${image%@*}" digest="${image#*@}"
        local tagged="${repo}:${digest/:/-}"
        log "  staging $repo @ ${digest#sha256:}"
        docker pull --platform "linux/$ARCH" "$image" >/dev/null \
            || die "Could not pull $image for linux/$ARCH"
        docker tag "$image" "$tagged"
        echo "$tagged" >> "$EXTRA_IMAGES"
    done < "$PINNED"
}

if [[ "$SKIP_IMAGES" == true ]]; then
    log "Skipping image staging (--skip-images)"
    cp "$PINNED" "$EXTRA_IMAGES"
else
    log "Staging the images for linux/$ARCH"
    stage_images
    [[ "$HAS_LOCAL_FALLBACK" == true ]] || FORWARD+=(--local-image-fallback)
fi

# What was already there, so only what they produce now is merged. An
# interrupted earlier run can leave a tarball behind, and merging that one would
# ship a bundle built from a different version's images.
BEFORE="$(find "$OUTPUT_DIR" -maxdepth 1 -name 'nv-config-manager-airgapped-*.tar.gz' -type f | sort)"

log "Running NVCM's bundle script"
"$THEIRS" --output "$OUTPUT_DIR" --extra-images-file "$EXTRA_IMAGES" "${FORWARD[@]}"

# Their script names the tarballs itself, so find what it just produced rather
# than trying to predict it. All of them: --arch both makes two, and merging
# only the newest left the other one sitting in the output directory looking
# like a bundle that carries NetBox when it does not.
TARBALLS=()
while IFS= read -r t; do
    [[ -n "$t" ]] && TARBALLS+=("$t")
done < <(find "$OUTPUT_DIR" -maxdepth 1 -name 'nv-config-manager-airgapped-*.tar.gz' -type f \
    | sort | comm -13 <(printf '%s' "$BEFORE") -)
[[ ${#TARBALLS[@]} -gt 0 ]] || die "Their script produced no new tarball in $OUTPUT_DIR"

# The chart's dependency must be vendored, or the target tries to reach
# charts.netbox.oss.netboxlabs.com and there is no network there.
if ! compgen -G "$CHART/charts/netbox-*.tgz" >/dev/null; then
    helm dependency build "$CHART"
fi

for TARBALL in "${TARBALLS[@]}"; do
    log "Merging the NetBox chart into $(basename "$TARBALL")"

    WORK="$(mktemp -d)"
    trap 'rm -rf "$WORK"' EXIT
    tar -xzf "$TARBALL" -C "$WORK"
    ROOT="$(find "$WORK" -maxdepth 1 -mindepth 1 -type d | head -1)"
    [[ -n "$ROOT" ]] || die "Unexpected tarball layout: no top-level directory"

    # Assert the layout we are merging into. If they restructure the bundle, fail
    # here with something readable instead of producing a subtly broken tarball.
    for dir in helm images installer; do
        [[ -d "$ROOT/$dir" ]] || die "Expected $dir/ in the bundle; their layout has changed"
    done

    cp -R "$CHART" "$ROOT/netbox-chart"
    cp "$SCRIPT_DIR/install-netbox.sh" "$ROOT/install-netbox.sh"
    chmod +x "$ROOT/install-netbox.sh"

    cat >> "$ROOT/README.md" <<'BUNDLE_README'

## NetBox as the DCIM provider

This bundle also carries NetBox, its chart in `netbox-chart/` and its images in
`images/`. Install NetBox first: it issues the API token NVCM authenticates
with, so NVCM cannot come up correctly before it exists.

    ./install-netbox.sh --registry registry.example.com/nv-config-manager

That prints the API token. Put it in `install.yaml` under
`secrets.k8s.dcim.values.token`, then deploy NVCM as described above.

`install.yaml` must also keep `dcim.provider: netbox`, `services.nautobot: false`
and a `dcim.provider_packages` entry pointing at the provider wheelhouse image
in this bundle. Without that entry NVCM never installs the provider.
BUNDLE_README

    log "Repacking"
    OUT_NAME="$(basename "$TARBALL" .tar.gz | sed 's/^nv-config-manager-airgapped/nv-config-manager-netbox-airgapped/')"
    mv "$ROOT" "$WORK/$OUT_NAME"
    tar -czf "$OUTPUT_DIR/$OUT_NAME.tar.gz" -C "$WORK" "$OUT_NAME"
    rm -f "$TARBALL"

    # Theirs checksummed the tarball we just replaced. Left alone, the bundle
    # ships with a checksum naming a file that is not there and none of its own.
    rm -f "$TARBALL.sha256"
    if command -v shasum >/dev/null; then
        (cd "$OUTPUT_DIR" && shasum -a 256 "$OUT_NAME.tar.gz" > "$OUT_NAME.tar.gz.sha256")
    elif command -v sha256sum >/dev/null; then
        (cd "$OUTPUT_DIR" && sha256sum "$OUT_NAME.tar.gz" > "$OUT_NAME.tar.gz.sha256")
    else
        warn "No shasum/sha256sum; $OUT_NAME.tar.gz has no checksum file"
    fi

    rm -rf "$WORK"
    trap - EXIT

    log "Bundle: $OUTPUT_DIR/$OUT_NAME.tar.gz ($(du -h "$OUTPUT_DIR/$OUT_NAME.tar.gz" | cut -f1))"
done
