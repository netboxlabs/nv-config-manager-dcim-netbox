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
# Target architecture for the images. Defaults to this host, which is right for a
# local kind cluster. A DSX Air sim box is x86_64, so building on Apple Silicon
# without PLATFORM=linux/amd64 ships images its nodes cannot execute
# ("exec format error").
PLATFORM   ?=
PLATFORM_ARG := $(if $(PLATFORM),--platform $(PLATFORM),)
NETBOX_IMAGE ?= nv-config-manager-netbox:$(VERSION)
include build/versions.env

VENV     := .venv
PY       := $(VENV)/bin/python
PYTEST   := $(VENV)/bin/pytest
RUFF     := $(VENV)/bin/ruff
TEMPLATE := $(VENV)/bin/template-cli

.PHONY: help setup setup-air lint version-check test test-netbox test-integration discovery probe provider-config \
        cache-query render-cli test-sdk test-templates check clean image check-image \
        netbox-base netbox-image images kind-up kind-down topology tunnel airgapped airgapped-images \
        inventory reference ipam hosts dhcp ztp firmware state secrets render \
        spectrumx ib infiniband spx lifecycle events

help:
	@echo "setup           create venv + install SDK, templates, provider, dev tools"
	@echo "lint            ruff check + format check"
	@echo "test            run this provider's unit tests (alias: test-netbox)"
	@echo "test-integration run NVCM's shared cluster suite with the NetBox adapter"
	@echo "discovery       confirm the 'netbox' provider is discovered"
	@echo "probe           call get_device_metadata against SERVER/TOKEN/DEVICE_ID"
	@echo "cache-query     template-cli: query provider for render data -> rd.json"
	@echo "render-cli      template-cli: render from rd.json"
	@echo "test-sdk        run the SDK (components/dcim) tests"
	@echo "test-templates  run the templates library tests"
	@echo "check           lint + test"
	@echo "image           build the provider-package OCI image (IMAGE=..., SDK from NVCM=...)"
	@echo "netbox-image    build NetBox + our plugin (PLUGIN=..., pins in build/versions.env)"
	@echo "images          build both images (PLATFORM=linux/amd64 for a DSX Air sim)"
	@echo "kind-up         create the Kind cluster and deploy NetBox + NVCM"
	@echo "kind-down       delete the Kind cluster"
	@echo "topology        load mock topology data into NetBox (BLUEPRINT=air_trial)"
	@echo "tunnel          SOCKS tunnel to a DSX Air sim (PRINT=1, BROWSER=1)"
	@echo "airgapped       build an offline bundle carrying NVCM + NetBox"
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
	  -e $(NVCM)/installer \
	  -e ".[installer]" pytest pytest-asyncio ruff

# Compatibility alias retained for the documented DSX Air setup command. The
# common setup now installs NVCM's installer because both deployment paths derive
# from it.
setup-air: setup

# --- Tier 0: lint + unit tests ---
lint:
	$(RUFF) check src/ tests/
	$(RUFF) format --check src/ tests/

# Two pairings that silently rot:
#   chart netbox.image.tag == pyproject version (`nvcm-netbox up` leaves the tag
#   alone), and NETBOX_VERSION within the plugin's min/max_version. The second is
#   skipped when there is no plugin checkout at PLUGIN.
version-check:
	@chart_tag=$$(sed -n 's/^ *tag: "\(.*\)"/\1/p' deploy/netbox/values.yaml | head -1); \
	if [ "$$chart_tag" != "$(VERSION)" ]; then \
	  echo "deploy/netbox/values.yaml tag ($$chart_tag) != pyproject version ($(VERSION))"; \
	  exit 1; \
	fi
	@init=$(PLUGIN)/netbox_nv_config_manager/__init__.py; \
	if [ ! -f "$$init" ]; then \
	  echo "version-check: no plugin checkout at $(PLUGIN), skipping the min_version pairing"; \
	else \
	  floor=$$(sed -n "s/^ *min_version *= *'\(.*\)'/\1/p" "$$init" | head -1); \
	  ceil=$$(sed -n "s/^ *max_version *= *'\(.*\)'/\1/p" "$$init" | head -1); \
	  NETBOX_VERSION='$(NETBOX_VERSION)' FLOOR="$$floor" CEIL="$$ceil" \
	    $(PY) build/check-netbox-version.py; \
	fi

test test-netbox:
	$(PYTEST) -q

# The common suite remains owned by NVCM. This provider contributes only its
# lifecycle-inspection adapter and selects it through the public pytest option.
# INTEGRATION_ARGS can add --use-port-forward, -k, or environment-specific flags.
INTEGRATION_ARGS ?=
INTEGRATION_TESTS ?= $(NVCM)/src/tests/integration/

test-integration:
	uv run --project $(NVCM) \
	  --with-editable $(NVCM)/components/dcim --with-editable . pytest \
	  $(INTEGRATION_TESTS) -v --timeout=900 \
	  --dcim-provider netbox \
	  --dcim-integration-adapter \
	  nv_config_manager_dcim_netbox.integration_adapter:NetBoxIntegrationAdapter \
	  $(INTEGRATION_ARGS)

check: lint version-check test

# --- images ---
# The SDK is not yet published, so it is passed as a named build context.
# Requires Docker Buildx (bundled with modern Docker). Override IMAGE / NVCM.
image:
	docker build $(PLATFORM_ARG) --build-context sdk=$(NVCM)/components/dcim -t $(IMAGE) .

# The offline /plugin-wheels contract NVCM's init container relies on. Defaults
# to this host's architecture, matching what `make image` just built.
check-image:
	build/check-provider-image.sh $(IMAGE) $(if $(PLATFORM),$(PLATFORM),linux/$(shell uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/'))

# NetBox + our plugin. netbox-base is a TEMPORARY step: it builds NetBox from a
# pinned `feature` commit because 4.7 (which the plugin's GraphQL extensions
# need) is unreleased. See build/versions.env.
netbox-base:
	$(if $(PLATFORM),BUILDX_PLATFORM=$(PLATFORM)) build/netbox-image.sh

netbox-image: netbox-base
	docker build $(PLATFORM_ARG) -f build/netbox.Dockerfile \
	  --build-arg NETBOX_BASE_IMAGE=$(NETBOX_BASE_IMAGE) \
	  --build-arg NETBOX_BGP_REF=$(NETBOX_BGP_REF) \
	  --build-context plugin=$(PLUGIN) \
	  -t $(NETBOX_IMAGE) .

images: image netbox-image

# --- deployment ---
# Named after NVCM's own targets. Theirs creates the cluster then deploys NVCM;
# ours deploys NetBox first, because NetBox owns the API token NVCM uses.
KIND_CLUSTER ?= nv-config-manager

# Depends on `images` so this is one command like NVCM's. Our two images are not
# built by the installer -- it builds NVCM's -- and a cached rebuild is seconds.
kind-up: images
	NVCM_REPO=$(NVCM) $(VENV)/bin/nvcm-netbox up --kind-cluster $(KIND_CLUSTER)

kind-down:
	kind delete cluster --name $(KIND_CLUSTER)

# NVCM's target of the same name, for NetBox. A fresh NetBox is empty, and an
# empty DCIM renders nothing.
BLUEPRINT ?= air_trial

topology:
	NVCM_REPO=$(NVCM) $(VENV)/bin/nvcm-netbox netbox load-topology --blueprint $(BLUEPRINT)

# --- DSX Air ---
# A SOCKS tunnel to the running sim, which is the only way to reach its
# `*.nvcm.air` hostnames: they resolve on the box, not here. Takes no arguments --
# the sim's SSH endpoint is looked up from simulation_name in the sim config, and
# the password comes from the same file. `make tunnel PRINT=1` shows the command
# instead, `BROWSER=1` also opens one.
tunnel:
	$(VENV)/bin/nvcm-netbox air tunnel $(if $(PRINT),--print,) $(if $(BROWSER),--browser,) \
	  $(if $(SOCKS_PORT),--socks-port $(SOCKS_PORT),)

# --- air-gapped ---
airgapped-images:
	deploy/airgapped/netbox-images.sh -o generated/netbox-extra-images.txt

airgapped:
	NVCM_REPO=$(NVCM) deploy/airgapped/create-airgapped.sh --version v$(VERSION)

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
