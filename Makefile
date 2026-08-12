# Simple test entrypoints for the NetBox DCIM provider.
#
# Override on the command line, e.g.:
#   make render SERVER=https://netbox.example TOKEN=abc DEVICE_ID=<id> ENTRYPOINT=<name>
#   make setup NVCM=/path/to/nv-config-manager   # branch: zb/pluggable-dcim

# Path to the nv-config-manager checkout. Only a sibling clone by default, so
# export NVCM_REPO once rather than passing NVCM= to every target. The
# `nvcm-netbox` CLI reads the same variable.
NVCM       ?= $(if $(NVCM_REPO),$(NVCM_REPO),../nv-config-manager)
PLUGIN     ?= ../netbox-nv-config-manager
SERVER     ?= http://localhost:8000
TOKEN      ?= REPLACE_ME
DEVICE_ID  ?= REPLACE_ME
ENTRYPOINT ?= REPLACE_ME
# Tag tracks the version declared in pyproject.toml (the single source of truth).
VERSION    := $(shell sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml)
IMAGE      ?= nv-config-manager-dcim-netbox:$(VERSION)
NETBOX_IMAGE ?= nv-config-manager-netbox:$(VERSION)
include build/versions.env

VENV     := .venv
PY       := $(VENV)/bin/python
PYTEST   := $(VENV)/bin/pytest
RUFF     := $(VENV)/bin/ruff
TEMPLATE := $(VENV)/bin/template-cli

.PHONY: help setup lint version-check test test-netbox discovery probe provider-config \
        cache-query render-cli test-sdk test-templates check clean image \
        netbox-base netbox-image images \
        inventory reference ipam hosts dhcp ztp firmware state secrets render \
        spectrumx ib infiniband spx lifecycle events

help:
	@echo "setup           create venv + install SDK, templates, provider, dev tools"
	@echo "lint            ruff check + format check"
	@echo "test            run this provider's unit tests (alias: test-netbox)"
	@echo "discovery       confirm the 'netbox' provider is discovered"
	@echo "probe           call get_device_metadata against SERVER/TOKEN/DEVICE_ID"
	@echo "cache-query     template-cli: query provider for render data -> rd.json"
	@echo "render-cli      template-cli: render from rd.json"
	@echo "test-sdk        run the SDK (components/dcim) tests"
	@echo "test-templates  run the templates library tests"
	@echo "check           lint + test"
	@echo "image           build the provider-package OCI image (IMAGE=..., SDK from NVCM=...)"
	@echo "netbox-image    build NetBox + our plugin (PLUGIN=..., pins in build/versions.env)"
	@echo "images          build both images (what 'nvcm-netbox up' expects)"
	@echo "clean           remove venv and caches"
	@echo ""
	@echo "per-area (implement one at a time):"
	@echo "  inventory reference ipam hosts dhcp ztp firmware state"
	@echo "  secrets render spectrumx ib(=infiniband) lifecycle events"

# --- setup ---
setup:
	uv venv --python 3.13
	uv pip install --python $(PY) \
	  $(NVCM)/components/dcim \
	  $(NVCM)/components/network-templates \
	  -e ".[installer]" pytest pytest-asyncio ruff

# Adds the DSX Air path. The installer is installed from the NVCM checkout
# (which `nvcm-netbox air` needs regardless, for the chart and the blueprints)
# rather than from a git ref, so a force-push cannot orphan the pin.
setup-air: setup
	uv pip install --python $(PY) -e $(NVCM)/installer

# --- Tier 0: lint + unit tests ---
lint:
	$(RUFF) check src/ tests/
	$(RUFF) format --check src/ tests/

# The chart ships its own netbox.image.tag and `nvcm-netbox up` leaves it alone,
# so it has to match the version `make netbox-image` builds.
version-check:
	@chart_tag=$$(sed -n 's/^ *tag: "\(.*\)"/\1/p' deploy/netbox/values.yaml | head -1); \
	if [ "$$chart_tag" != "$(VERSION)" ]; then \
	  echo "deploy/netbox/values.yaml tag ($$chart_tag) != pyproject version ($(VERSION))"; \
	  exit 1; \
	fi

test test-netbox:
	$(PYTEST) -q

check: lint version-check test

# --- images ---
# The SDK is not yet published, so it is passed as a named build context.
# Requires Docker Buildx (bundled with modern Docker). Override IMAGE / NVCM.
image:
	docker build --build-context sdk=$(NVCM)/components/dcim -t $(IMAGE) .

# NetBox + our plugin. netbox-base is a TEMPORARY step: it builds NetBox from a
# pinned `feature` commit because 4.7 (which the plugin's GraphQL extensions
# need) is unreleased. See build/versions.env.
netbox-base:
	build/netbox-image.sh

netbox-image: netbox-base
	docker build -f build/netbox.Dockerfile \
	  --build-arg NETBOX_BASE_IMAGE=$(NETBOX_BASE_IMAGE) \
	  --build-arg NETBOX_BGP_VERSION=$(NETBOX_BGP_VERSION) \
	  --build-context plugin=$(PLUGIN) \
	  -t $(NETBOX_IMAGE) .

images: image netbox-image

# --- Tier 1: provider dev loop ---
discovery:
	$(PY) -c "from nv_config_manager_dcim import discover_dcim_providers; print(discover_dcim_providers())"

probe:
	SERVER="$(SERVER)" TOKEN="$(TOKEN)" DEVICE_ID="$(DEVICE_ID)" $(PY) scripts/probe.py

provider-config:
	@printf '[provider]\nname = "netbox"\n[provider.settings]\nserver = "%s"\ntoken  = "%s"\n' \
	  "$(SERVER)" "$(TOKEN)" > netbox-provider.toml
	@echo "wrote netbox-provider.toml"

cache-query: provider-config
	$(TEMPLATE) cache-query --provider-config netbox-provider.toml \
	  --device-id $(DEVICE_ID) --output-render-data-file rd.json

render-cli:
	$(TEMPLATE) render --cached-render-data rd.json \
	  --entrypoint $(ENTRYPOINT) --output-file out.txt

# --- per-area test suites (run one functional area's tests) ---
inventory reference ipam hosts dhcp ztp firmware state secrets render spectrumx ib lifecycle events:
	$(PYTEST) -m $@ -q

# aliases
infiniband: ib
spx: spectrumx

# --- per-package test suites ---
test-sdk:
	cd $(NVCM)/components/dcim && uv run pytest

test-templates:
	cd $(NVCM)/components/network-templates && uv run pytest

# --- housekeeping ---
clean:
	rm -rf $(VENV) .pytest_cache .ruff_cache *.egg-info rd.json out.txt
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
