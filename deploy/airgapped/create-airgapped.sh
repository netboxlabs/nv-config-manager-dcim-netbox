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
die()  { printf '\033[0;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

NVCM_REPO="$(cd "$NVCM_REPO" 2>/dev/null && pwd)" \
    || die "No nv-config-manager checkout at $NVCM_REPO. Set NVCM_REPO."
THEIRS="$NVCM_REPO/deploy/airgapped/create-airgapped.sh"
[[ -x "$THEIRS" ]] || die "Not found or not executable: $THEIRS"

mkdir -p "$OUTPUT_DIR"

log "Collecting the images the NetBox chart needs"
EXTRA_IMAGES="$OUTPUT_DIR/netbox-extra-images.txt"
"$SCRIPT_DIR/netbox-images.sh" -o "$EXTRA_IMAGES" >/dev/null
log "$(wc -l < "$EXTRA_IMAGES" | tr -d ' ') images -> $EXTRA_IMAGES"

log "Running NVCM's bundle script"
"$THEIRS" --output "$OUTPUT_DIR" --extra-images-file "$EXTRA_IMAGES" "$@"

# Their script names the tarball itself, so find what it just produced rather
# than trying to predict it.
TARBALL="$(find "$OUTPUT_DIR" -maxdepth 1 -name 'nv-config-manager-airgapped-*.tar.gz' \
    -type f -exec ls -t {} + | head -1)"
[[ -n "$TARBALL" ]] || die "Their script produced no tarball in $OUTPUT_DIR"
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

# The chart's dependency must be vendored, or the target tries to reach
# charts.netbox.oss.netboxlabs.com and there is no network there.
if ! compgen -G "$CHART/charts/netbox-*.tgz" >/dev/null; then
    helm dependency build "$CHART"
fi
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

log "Bundle: $OUTPUT_DIR/$OUT_NAME.tar.gz ($(du -h "$OUTPUT_DIR/$OUT_NAME.tar.gz" | cut -f1))"
