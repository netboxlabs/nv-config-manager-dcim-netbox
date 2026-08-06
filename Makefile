# Simple test entrypoints for the NetBox DCIM provider.
#
# Override on the command line, e.g.:
#   make render SERVER=https://netbox.example TOKEN=abc DEVICE_ID=<id> ENTRYPOINT=<name>
#   make setup NVCM=/path/to/nv-config-manager   # branch: zb/pluggable-dcim

NVCM       ?= ../nv-config-manager
SERVER     ?= http://localhost:8000
TOKEN      ?= REPLACE_ME
DEVICE_ID  ?= REPLACE_ME
ENTRYPOINT ?= REPLACE_ME
# Tag tracks the version declared in pyproject.toml (the single source of truth).
VERSION    := $(shell sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml)
IMAGE      ?= nv-config-manager-dcim-netbox:$(VERSION)

VENV     := .venv
PY       := $(VENV)/bin/python
PYTEST   := $(VENV)/bin/pytest
RUFF     := $(VENV)/bin/ruff
TEMPLATE := $(VENV)/bin/template-cli

.PHONY: help setup lint test test-netbox discovery probe provider-config \
        cache-query render-cli test-sdk test-templates check clean image \
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
	  -e . pytest pytest-asyncio ruff

# --- Tier 0: lint + unit tests ---
lint:
	$(RUFF) check src/ tests/
	$(RUFF) format --check src/ tests/

test test-netbox:
	$(PYTEST) -q

check: lint test

# --- provider-package image ---
# The SDK is not yet published, so it is passed as a named build context.
# Requires Docker Buildx (bundled with modern Docker). Override IMAGE / NVCM.
image:
	docker build --build-context sdk=$(NVCM)/components/dcim -t $(IMAGE) .

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
