#!/usr/bin/env bash
# Install NetBox from an air-gapped bundle. Run this before deploying NVCM.
#
#   ./install-netbox.sh --registry registry.example.com/nv-config-manager
#
# NetBox issues the API token NVCM authenticates with, so it has to exist first.
# The token is printed at the end for install.yaml.
#
# A registry that needs credentials: pass --registry-username with either
# --registry-password or --registry-password-stdin and a pull secret is created
# for you, or name one you already have with --pull-secret.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART="$SCRIPT_DIR/netbox-chart"
REGISTRY=""
NAMESPACE="nv-config-manager"
RELEASE="netbox"
TOKEN=""
TIMEOUT="15m"
PULL_SECRET=""
REGISTRY_USERNAME=""
REGISTRY_PASSWORD=""
PASSWORD_STDIN=false

usage() { sed -n '2,12p' "$0"; exit "${1:-0}"; }

while [[ $# -gt 0 ]]; do
    case $1 in
        --registry)   REGISTRY="$2"; shift 2 ;;
        --namespace)  NAMESPACE="$2"; shift 2 ;;
        --release)    RELEASE="$2"; shift 2 ;;
        --token)      TOKEN="$2"; shift 2 ;;
        --timeout)    TIMEOUT="$2"; shift 2 ;;
        --pull-secret) PULL_SECRET="$2"; shift 2 ;;
        --registry-username) REGISTRY_USERNAME="$2"; shift 2 ;;
        --registry-password) REGISTRY_PASSWORD="$2"; shift 2 ;;
        --registry-password-stdin) PASSWORD_STDIN=true; shift ;;
        -h|--help)    usage ;;
        *) echo "Unknown argument: $1" >&2; usage 1 ;;
    esac
done

[[ -n "$REGISTRY" ]] || { echo "--registry is required" >&2; usage 1; }
[[ -d "$CHART" ]] || { echo "No chart at $CHART" >&2; exit 1; }
command -v helm >/dev/null    || { echo "helm is required" >&2; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl is required" >&2; exit 1; }

# `head -c` closes the pipe as soon as it has read enough, so `tr` -- still
# reading the endless /dev/urandom stream -- takes SIGPIPE and the pipeline
# exits 141. Under `pipefail` that aborts the script, silently, before anything
# is installed. `|| true` discards the signal status; the length check is what
# actually verifies the read.
rand_alnum() {
    local n="$1" out
    out="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c "$n" || true)"
    [[ ${#out} -eq "$n" ]] || { echo "Could not read $n random characters" >&2; return 1; }
    printf '%s' "$out"
}

if [[ -z "$TOKEN" ]]; then
    # NetBox v2 format: nbt_ + 12-char key + . + 40-char secret, all alphanumeric.
    # A malformed token is skipped silently by nvcm_init, so the shape matters.
    # Assigned separately so `set -e` sees a failure; two substitutions in one
    # assignment would report only the last one's status.
    key="$(rand_alnum 12)"
    secret="$(rand_alnum 40)"
    TOKEN="nbt_${key}.${secret}"
fi

# --- image tags as the private registry actually holds them ------------------
#
# NVCM's `upload-to-registry.sh` rewrites a digest reference into a *tag* when it
# pushes (`target_ref`: `foo@sha256:abc` -> `$REGISTRY/foo:sha256-abc`), and
# netbox-images.sh digest-pins every image in this bundle that came from a
# registry. So overriding `image.registry` alone leaves the chart asking for the
# original `:latest` / `:1.38.0` tags, which were never pushed -- every pod ends
# in ImagePullBackOff.
#
# Read back from the bundle's own image list rather than recomputed, so this
# cannot drift from what was uploaded. Our NetBox image is built locally and has
# no digest to resolve, so its chart tag is already correct.
IMAGE_LIST="$SCRIPT_DIR/images/image-list.txt"

mapped_tag() {
    # $1 = repository path as it appears in the bundle list, e.g. bitnami/valkey
    local repo="$1" line last
    [[ -f "$IMAGE_LIST" ]] || return 1
    line="$(grep -E "(^|/)${repo}[@:]" "$IMAGE_LIST" | head -1)"
    [[ -n "$line" ]] || return 1
    if [[ "$line" == *@sha256:* ]]; then
        printf 'sha256-%s' "${line#*@sha256:}"
        return 0
    fi
    last="${line##*/}"
    [[ "$last" == *:* ]] || return 1
    printf '%s' "${last##*:}"
}

TAG_SETS=()
for pair in \
    "netbox.postgresql.image.tag=bitnami/postgresql" \
    "netbox.valkey.image.tag=bitnami/valkey" \
    "netbox.init.image.tag=busybox" \
    "netbox.test.image.tag=busybox" \
    "netbox.worker.waitForBackend.image.tag=rancher/kubectl"
do
    chart_value="${pair%%=*}"
    repo_path="${pair#*=}"
    if tag="$(mapped_tag "$repo_path")"; then
        TAG_SETS+=(--set "${chart_value}=${tag}")
    else
        echo "Warning: no $repo_path in $IMAGE_LIST; leaving ${chart_value} as the chart" \
             "ships it. If that tag was not pushed to $REGISTRY the pod will not start." >&2
    fi
done

# The `helm test` pod is the one image the chart builds without its registry
# value: templates/tests/test-connection.yaml renders
# `{{ .Values.test.image.repository }}:{{ .Values.test.image.tag }}` and nothing
# else, so `test.image.registry` above is read by no template. Carrying the
# registry in the repository is what actually repoints it -- and the tag override
# alone would otherwise make it worse than untouched, asking Docker Hub for a
# `sha256-...` tag that only the private registry has.
if mapped_tag busybox >/dev/null; then
    TAG_SETS+=(--set "netbox.test.image.repository=$REGISTRY/busybox")
fi

# --- pull secret -------------------------------------------------------------
#
# The upload step authenticates (`upload-to-registry.sh --username ...
# --password-stdin`), so the registry this then pulls from usually needs
# credentials too. Without a pull secret every pod sits in ImagePullBackOff with
# "unauthorized", which reads like a missing image rather than a missing secret.
#
# One `netbox.global.imagePullSecrets` covers all six pod specs: Helm propagates
# a chart's `global` down through the bitnami subcharts, so NetBox, PostgreSQL,
# Valkey, the init container and waitForBackend all pick it up.
SECRET_SETS=()
if [[ -n "$REGISTRY_USERNAME" ]]; then
    if [[ "$PASSWORD_STDIN" == true ]]; then
        IFS= read -r REGISTRY_PASSWORD || true
    fi
    [[ -n "$REGISTRY_PASSWORD" ]] || {
        echo "--registry-username needs --registry-password or --registry-password-stdin" >&2
        exit 1
    }
    : "${PULL_SECRET:=netbox-registry-credentials}"
    # The secret is per-host, while --registry carries the project path too.
    REGISTRY_HOST="${REGISTRY%%/*}"
    echo "==> Creating pull secret $PULL_SECRET for $REGISTRY_HOST"
    kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
    # Recreated rather than created: a rerun after a failed install would
    # otherwise stop on AlreadyExists, and the credentials may have changed.
    kubectl -n "$NAMESPACE" create secret docker-registry "$PULL_SECRET" \
        --docker-server="$REGISTRY_HOST" \
        --docker-username="$REGISTRY_USERNAME" \
        --docker-password="$REGISTRY_PASSWORD" \
        --dry-run=client -o yaml | kubectl apply -f - >/dev/null
fi
if [[ -n "$PULL_SECRET" ]]; then
    SECRET_SETS+=(--set "netbox.global.imagePullSecrets[0].name=$PULL_SECRET")
fi

echo "==> Installing NetBox into $NAMESPACE from $CHART"
# Every image the chart references has to be repointed, not just the main one:
# init and waitForBackend default to docker.io and would be the only thing
# reaching for the internet.
#
# allowInsecureImages is Bitnami's guard against non-Bitnami registries. Pushing
# their images into a private registry is exactly what an air-gapped install
# does, so the chart refuses to render without it.
helm upgrade --install "$RELEASE" "$CHART" \
    --namespace "$NAMESPACE" --create-namespace \
    --set "netbox.global.security.allowInsecureImages=true" \
    --set "netbox.superuser.apiToken=$TOKEN" \
    --set "netbox.image.registry=$REGISTRY" \
    --set "netbox.postgresql.image.registry=$REGISTRY" \
    --set "netbox.valkey.image.registry=$REGISTRY" \
    --set "netbox.init.image.registry=$REGISTRY" \
    --set "netbox.test.image.registry=$REGISTRY" \
    --set "netbox.worker.waitForBackend.image.registry=$REGISTRY" \
    ${TAG_SETS[@]+"${TAG_SETS[@]}"} \
    ${SECRET_SETS[@]+"${SECRET_SETS[@]}"} \
    --wait --timeout "$TIMEOUT"

echo "==> Initialising NetBox"
kubectl -n "$NAMESPACE" exec -i "deploy/netbox" -c netbox -- sh -s <<EOF
export SUPERUSER_API_TOKEN='$TOKEN'
exec /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py nvcm_init --bootstrap
EOF

cat <<EOF

NetBox is up.

API token: $TOKEN

Put it in install.yaml before deploying NVCM:

  secrets:
    k8s:
      dcim:
        values:
          token: $TOKEN

Once NVCM is deployed, connect NetBox to its event bus:

  PW=\$(kubectl -n $NAMESPACE get secret nats-nv-config-manager \\
         -o jsonpath='{.data.password}' | base64 -d)
  helm upgrade $RELEASE $CHART -n $NAMESPACE --reuse-values \\
    --set nvcm.natsPassword="\$PW"
EOF
