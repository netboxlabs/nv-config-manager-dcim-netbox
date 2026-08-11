# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""DSX Air path: config, the install-config patch, and the bring-up sequence.

No simulation and no cluster. The parts that need one are covered by the
end-to-end steps in docs/air-sim.md.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from nv_config_manager_dcim_netbox.installer import config as base_config
from nv_config_manager_dcim_netbox.installer.tokens import generate_api_token, is_valid_api_token

pytestmark = pytest.mark.air

# Everything below the config patch needs nv-config-manager-installer, which
# comes from an NVCM checkout rather than an index (see `make setup-air`).
# exc_type is required: the modules exist, so importing one without the extra raises
# AirSupportUnavailable, not the ModuleNotFoundError importorskip defaults to.
air_config = pytest.importorskip(
    "nv_config_manager_dcim_netbox.installer.air.config",
    reason="the `air` extra is not installed",
    exc_type=ImportError,
)
air_callback = pytest.importorskip(
    "nv_config_manager_dcim_netbox.installer.air.callback",
    reason="the `air` extra is not installed",
    exc_type=ImportError,
)
NetBoxSimConfig = air_config.NetBoxSimConfig
NetBoxAirCallback = air_callback.NetBoxAirCallback


# -- config -------------------------------------------------------------------


def test_sim_clones_the_branch_that_has_the_dcim_framework():
    # SimConfig defaults this to `main`, and cloud-init clones it onto the sim
    # box. `main` carries none of the pluggable-DCIM framework, so a sim built
    # with the base default cannot deploy this provider at all.
    assert NetBoxSimConfig().config_manager_ref == base_config.NVCM_BRANCH
    assert base_config.NVCM_BRANCH != "main"


def test_extra_keys_survive_from_yaml(tmp_path):
    # SimConfig.from_yaml filters on dataclasses.fields(cls), so the subclass's
    # fields load only because it resolves through cls.
    path = tmp_path / "sim.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "mock_blueprint": "air_trial",
                "dcim_server": "http://netbox",
                "netbox_image_tag": "9.9.9",
                "load_blueprint": False,
            }
        )
    )
    cfg = NetBoxSimConfig.from_yaml(path)
    assert cfg.mock_blueprint == "air_trial"
    assert cfg.dcim_server == "http://netbox"
    assert cfg.netbox_image_tag == "9.9.9"
    assert cfg.load_blueprint is False


def test_blueprint_dir_points_into_the_clone_on_the_box():
    cfg = NetBoxSimConfig(mock_blueprint="air_trial")
    assert cfg.blueprint_dir("/home/nvcm/nv-config-manager").endswith(
        "/development/mock_topology/context/air_trial"
    )


# -- the install-config patch --------------------------------------------------


def _generated_config() -> dict:
    """The shape NVCM's own generator emits: no DCIM block, bundled DCIM on."""
    return {
        "version": "1",
        "cluster": {"namespace": "nv-config-manager", "size": "medium"},
        "services": {"render": True, "nautobot": True},
        "sites": [{"name": "air-trial"}],
    }


def test_apply_dcim_selects_netbox():
    token = generate_api_token()
    patched = base_config.apply_dcim(
        _generated_config(),
        base_config.ConfigOptions(
            api_token=token,
            provider_image="reg.example/provider:1.2",
            netbox_server="http://netbox",
            netbox_public_url="http://netbox",
        ),
    )
    assert patched["dcim"]["provider"] == base_config.PROVIDER_NAME
    assert patched["dcim"]["server"] == "http://netbox"
    # Without this block the provider is never installed into NVCM's pods.
    assert patched["dcim"]["provider_packages"][0]["image"] == "reg.example/provider:1.2"
    assert patched["secrets"]["k8s"]["dcim"]["values"]["token"] == token
    assert patched["services"]["nautobot"] is False


def test_apply_dcim_leaves_the_generators_own_fields_alone():
    # cluster.* is theirs on the Air path; only the kind path sets it.
    patched = base_config.apply_dcim(
        _generated_config(), base_config.ConfigOptions(api_token=generate_api_token())
    )
    assert patched["cluster"] == {"namespace": "nv-config-manager", "size": "medium"}
    assert patched["sites"] == [{"name": "air-trial"}]
    assert patched["services"]["render"] is True


# -- the bring-up sequence -----------------------------------------------------


class _FakeManager:
    """Stands in for AirSimulationManager: records commands, uploads nothing."""

    def __init__(self, files: dict[str, str] | None = None) -> None:
        self.commands: list[str] = []
        self.uploads: list[tuple[str, str]] = []
        self.files = files or {}

    def _ssh_cmd(self, host, port, command):
        self.commands.append(command)
        # The patch step reads the uploaded config back with `cat`.
        if command.startswith("cat "):
            return ["printf", "%s", self.files.get(command.split()[-1], "")]
        if "jsonpath" in command:
            return ["printf", "%s", "netbox-abc123"]
        return ["true"]

    def upload_to_server(self, host, port, local, remote, **kwargs):
        self.uploads.append((str(local), remote))
        self.files[remote] = Path(local).read_text()
        return True


class _Recorder:
    def __init__(self) -> None:
        self.events: list[str] = []

    def on_step(self, step_id, status, message="") -> None:
        self.events.append(f"step:{step_id}")

    def on_log(self, line) -> None:
        self.events.append(f"log:{line}")

    def on_ssh_ready(self, host, port) -> None:
        self.events.append("ssh_ready")

    def on_deploy_started(self, host, port) -> None:
        self.events.append("deploy_started")

    def on_complete(self, success, host="", port=0) -> None:
        self.events.append(f"complete:{success}")


def test_bring_up_runs_before_the_deploy_is_reported(monkeypatch):
    # on_deploy_started is the only window between "install config uploaded" and
    # "deploy runs", so the NetBox work has to finish before it is passed on.
    order: list[str] = []
    cfg = NetBoxSimConfig(mock_blueprint="air_trial")
    callback = NetBoxAirCallback(_Recorder(), cfg, manager=_FakeManager())
    monkeypatch.setattr(callback, "bring_up_netbox", lambda h, p: order.append("bring_up"))
    monkeypatch.setattr(callback._inner, "on_deploy_started", lambda h, p: order.append("inner"))

    callback.on_deploy_started("host", 22)
    assert order == ["bring_up", "inner"]


def test_inner_callback_still_runs_when_bring_up_fails(monkeypatch):
    # A failed bring-up must not swallow the orchestrator's own reporting.
    recorder = _Recorder()
    callback = NetBoxAirCallback(recorder, NetBoxSimConfig(), manager=_FakeManager())

    def _boom(host, port):
        raise RuntimeError("no cluster")

    monkeypatch.setattr(callback, "bring_up_netbox", _boom)
    with pytest.raises(RuntimeError):
        callback.on_deploy_started("host", 22)
    assert "deploy_started" in recorder.events


def test_a_generated_token_is_valid_and_reused():
    cfg = NetBoxSimConfig()
    callback = NetBoxAirCallback(_Recorder(), cfg, manager=_FakeManager())
    assert is_valid_api_token(callback.api_token)
    # Supplied tokens win, so both sides can be given a known value.
    supplied = generate_api_token()
    other = NetBoxAirCallback(
        _Recorder(), NetBoxSimConfig(dcim_token=supplied), manager=_FakeManager()
    )
    assert other.api_token == supplied


def test_a_failed_event_bus_hookup_does_not_fail_a_good_deploy(monkeypatch):
    # NVCM is deployed by this point; losing the publisher is recoverable and
    # must not turn a working sim into a reported failure.
    recorder = _Recorder()
    callback = NetBoxAirCallback(recorder, NetBoxSimConfig(), manager=_FakeManager())
    monkeypatch.setattr(
        callback, "connect_event_bus", lambda h, p: (_ for _ in ()).throw(RuntimeError("nope"))
    )
    callback.on_complete(True, "host", 22)
    assert "complete:True" in recorder.events
    assert any("Could not connect NetBox to the event bus" in e for e in recorder.events)
