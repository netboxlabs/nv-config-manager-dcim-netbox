#!/usr/bin/env bash
# Stand up a disposable NetBox with the plugin, bootstrap data and a topology,
# for the provider to run against.
#
# Everything the provider reads -- GraphQL shapes, custom fields, config
# contexts, prefixes, BGP -- comes from NetBox and the plugin, so unit tests with
# fixtures cannot catch upstream schema drift. This gives the live suite
# (tests/live/) something real to query.
#
# Expects NetBox and the plugin already installed in the active environment, and
# PostgreSQL and Redis already reachable. Installing them is the caller's job:
#
#   pip install netbox==4.7.0b1 \
#     https://github.com/netbox-community/netbox-bgp/archive/refs/heads/0.20.0.tar.gz
#   pip install -e ../netbox-nv-config-manager
#
# Usage: scripts/live-netbox.sh <instance-dir> <blueprint-dir>
#
# Env (all optional):
#   DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD   default localhost/5432/netbox/netbox/netbox
#   REDIS_HOST/REDIS_PORT                         default localhost/6379
#   NETBOX_PORT                                   default 8000
#   NVCM_LIVE_TOKEN                               default a fixed dev token
set -euo pipefail

instance="${1:?usage: live-netbox.sh <instance-dir> <blueprint-dir>}"
blueprint="${2:?usage: live-netbox.sh <instance-dir> <blueprint-dir>}"

DB_HOST="${DB_HOST:-localhost}"; DB_PORT="${DB_PORT:-5432}"
DB_NAME="${DB_NAME:-netbox}"; DB_USER="${DB_USER:-netbox}"; DB_PASSWORD="${DB_PASSWORD:-netbox}"
REDIS_HOST="${REDIS_HOST:-localhost}"; REDIS_PORT="${REDIS_PORT:-6379}"
NETBOX_PORT="${NETBOX_PORT:-8000}"
# Fixed rather than generated: the live tests need it, and a disposable instance
# has nothing worth protecting.
NVCM_LIVE_TOKEN="${NVCM_LIVE_TOKEN:-nbt_liveCItoken1.$(printf 'a%.0s' {1..40})}"

mkdir -p "${instance}"
instance="$(cd "${instance}" && pwd)"
blueprint="$(cd "${blueprint}" && pwd)"

# FIELD_CHOICES has to be in the configuration, not the plugin: NetBox reads it
# before plugins load. Same reason build/netbox-field-choices.py exists for the
# image.
cat > "${instance}/configuration.py" <<PY
from netbox_nv_config_manager.field_choices import FIELD_CHOICES  # noqa: F401

ALLOWED_HOSTS = ['*']
DATABASE = {
    'NAME': '${DB_NAME}', 'USER': '${DB_USER}', 'PASSWORD': '${DB_PASSWORD}',
    'HOST': '${DB_HOST}', 'PORT': '${DB_PORT}', 'CONN_MAX_AGE': 300,
}
PLUGINS = ['netbox_nv_config_manager', 'netbox_bgp']
REDIS = {
    'tasks': {'HOST': '${REDIS_HOST}', 'PORT': ${REDIS_PORT}, 'PASSWORD': '', 'DATABASE': 0, 'SSL': False},
    'caching': {'HOST': '${REDIS_HOST}', 'PORT': ${REDIS_PORT}, 'PASSWORD': '', 'DATABASE': 1, 'SSL': False},
}
SECRET_KEY = 'liveCIsecretkeyliveCIsecretkeyliveCIsecretkey12345'
# v2 tokens are peppered; nvcm_init skips token creation without this.
API_TOKEN_PEPPERS = {1: 'liveCIpepperliveCIpepperliveCIpepperliveCIpepper01'}
PY

export NETBOX_CONFIGURATION=configuration
export PYTHONPATH="${instance}${PYTHONPATH:+:${PYTHONPATH}}"
export SUPERUSER_NAME=admin
export SUPERUSER_PASSWORD=admin
export SUPERUSER_EMAIL=admin@example.com
export SUPERUSER_API_TOKEN="${NVCM_LIVE_TOKEN}"

# migrate + superuser + v2 token + custom fields + bootstrap data, in one step.
echo "==> nvcm_init"
netbox nvcm_init

echo "==> loading blueprint $(basename "${blueprint}")"
netbox nvcm_load_dcim "${blueprint}"

echo "==> serving on :${NETBOX_PORT}"
netbox runserver "0.0.0.0:${NETBOX_PORT}" --noreload --skip-checks \
  > "${instance}/netbox.log" 2>&1 &
echo $! > "${instance}/netbox.pid"

for _ in $(seq 1 60); do
  if curl -fsS -o /dev/null "http://localhost:${NETBOX_PORT}/api/status/" \
       -H "Authorization: Token ${NVCM_LIVE_TOKEN}"; then
    echo "==> ready: http://localhost:${NETBOX_PORT}"
    echo "    token: ${NVCM_LIVE_TOKEN}"
    exit 0
  fi
  sleep 2
done

echo "NetBox did not answer on :${NETBOX_PORT}" >&2
tail -40 "${instance}/netbox.log" >&2
exit 1
