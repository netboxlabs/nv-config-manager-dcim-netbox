#!/usr/bin/env bash
# Install NetBox from an air-gapped bundle. Run this before deploying NVCM.
#
#   ./install-netbox.sh --registry registry.example.com/nv-config-manager
#
# NetBox issues the API token NVCM authenticates with, so it has to exist first.
# The token is printed at the end for install.yaml.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART="$SCRIPT_DIR/netbox-chart"
REGISTRY=""
NAMESPACE="nv-config-manager"
RELEASE="netbox"
TOKEN=""
TIMEOUT="15m"

usage() { sed -n '2,8p' "$0"; exit "${1:-0}"; }

while [[ $# -gt 0 ]]; do
    case $1 in
        --registry)   REGISTRY="$2"; shift 2 ;;
        --namespace)  NAMESPACE="$2"; shift 2 ;;
        --release)    RELEASE="$2"; shift 2 ;;
        --token)      TOKEN="$2"; shift 2 ;;
        --timeout)    TIMEOUT="$2"; shift 2 ;;
        -h|--help)    usage ;;
        *) echo "Unknown argument: $1" >&2; usage 1 ;;
    esac
done

[[ -n "$REGISTRY" ]] || { echo "--registry is required" >&2; usage 1; }
[[ -d "$CHART" ]] || { echo "No chart at $CHART" >&2; exit 1; }
command -v helm >/dev/null    || { echo "helm is required" >&2; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl is required" >&2; exit 1; }

if [[ -z "$TOKEN" ]]; then
    # NetBox v2 format: nbt_ + 12-char key + . + 40-char secret, all alphanumeric.
    # A malformed token is skipped silently by nvcm_init, so the shape matters.
    key="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 12)"
    secret="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 40)"
    TOKEN="nbt_${key}.${secret}"
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
