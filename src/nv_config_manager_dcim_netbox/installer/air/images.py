# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Build the provider-owned images needed by a NetBox DSX Air simulation."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from importlib.util import find_spec
from pathlib import Path

from nv_config_manager_dcim_netbox.installer.shell import CommandError, require_tools

PLUGIN_REPO_ENV = "NETBOX_NVCM_PLUGIN_REPO"
PLUGIN_REPO_URL = "https://github.com/netboxlabs/netbox_nv_config_manager.git"
TARGET_PLATFORM = "linux/amd64"

_REPO_ROOT = Path(__file__).resolve().parents[4]


def _resolve_nvcm_repo() -> Path:
    """Find the NVCM checkout that supplied the installed AIR orchestrator."""
    configured = os.environ.get("NVCM_REPO")
    if configured:
        candidate = Path(configured).expanduser().resolve()
    else:
        package = find_spec("nv_config_manager_installer")
        if package is None or package.origin is None:
            raise RuntimeError(
                "Could not locate the installed NVCM AIR support. Set NVCM_REPO to its "
                "source checkout."
            )
        candidate = Path(package.origin).resolve().parents[3]

    if not (candidate / "components" / "dcim" / "pyproject.toml").is_file():
        raise RuntimeError(
            f"Could not find the NVCM source checkout at {candidate}. "
            "Set NVCM_REPO to the checkout used to install the AIR TUI."
        )
    return candidate


def _resolve_plugin_repo(log: Callable[[str], None]) -> Path:
    """Find the plugin checkout, cloning it into the build cache if necessary."""
    configured = os.environ.get(PLUGIN_REPO_ENV)
    if configured:
        candidates = [Path(configured).expanduser().resolve()]
    else:
        candidates = [
            _REPO_ROOT.parent / "netbox_nv_config_manager",
            _REPO_ROOT.parent / "netbox-nv-config-manager",
        ]
    for candidate in candidates:
        if (candidate / "pyproject.toml").is_file():
            return candidate

    if configured:
        raise RuntimeError(
            f"{PLUGIN_REPO_ENV} points at {candidates[0]}, which is not a plugin checkout."
        )

    require_tools("git")
    destination = _REPO_ROOT / ".build" / "netbox-nv-config-manager"
    if not destination.exists():
        log("Fetching the NetBox plugin build context...")
        _stream(
            ["git", "clone", "--depth", "1", PLUGIN_REPO_URL, str(destination)],
            cwd=_REPO_ROOT,
            log=log,
        )
    elif (destination / ".git").is_dir():
        log("Refreshing the cached NetBox plugin build context...")
        _stream(
            ["git", "fetch", "--depth", "1", "origin", "main"],
            cwd=destination,
            log=log,
        )
        _stream(["git", "checkout", "--detach", "FETCH_HEAD"], cwd=destination, log=log)
    if not (destination / "pyproject.toml").is_file():
        raise RuntimeError(f"The plugin build context at {destination} is incomplete.")
    return destination


def _load_versions() -> dict[str, str]:
    """Read the checked-in image pins used by the existing build scripts."""
    values: dict[str, str] = {}
    for line in (_REPO_ROOT / "build" / "versions.env").read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() and not key.lstrip().startswith("#"):
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _stream(
    argv: list[str],
    *,
    cwd: Path,
    log: Callable[[str], None],
    env: dict[str, str] | None = None,
) -> None:
    """Run a build command and feed combined output to the CLI or TUI log."""
    process = subprocess.Popen(  # noqa: S603 - argv is assembled from trusted paths and pins
        argv,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert process.stdout is not None
    for line in process.stdout:
        log(line.rstrip())
    returncode = process.wait()
    if returncode:
        raise CommandError(argv, returncode)


def ensure_air_images(
    provider_image: str,
    netbox_image: str,
    log: Callable[[str], None],
) -> None:
    """Build the NetBox images for a DSX Air host, using Docker's layer cache."""
    require_tools("docker", "git")
    if not (_REPO_ROOT / "Dockerfile").is_file():
        raise RuntimeError(
            "The NetBox AIR image builder is missing its packaged build assets. "
            "Reinstall nv-config-manager-dcim-netbox with its AIR support."
        )

    nvcm_repo = _resolve_nvcm_repo()
    plugin_repo = _resolve_plugin_repo(log)
    versions = _load_versions()
    build_env = {**os.environ, "DOCKER_BUILDKIT": "1"}

    log("Building the NetBox provider image for linux/amd64...")
    _stream(
        [
            "docker",
            "build",
            "--platform",
            TARGET_PLATFORM,
            "--build-context",
            f"sdk={nvcm_repo / 'components' / 'dcim'}",
            "-t",
            provider_image,
            ".",
        ],
        cwd=_REPO_ROOT,
        log=log,
        env=build_env,
    )

    log("Building the pinned NetBox base image for linux/amd64...")
    _stream(
        [str(_REPO_ROOT / "build" / "netbox-image.sh")],
        cwd=_REPO_ROOT,
        log=log,
        env={**build_env, "BUILDX_PLATFORM": TARGET_PLATFORM},
    )

    log("Building the NetBox plugin image for linux/amd64...")
    _stream(
        [
            "docker",
            "build",
            "--platform",
            TARGET_PLATFORM,
            "-f",
            "build/netbox.Dockerfile",
            "--build-arg",
            f"NETBOX_BASE_IMAGE={versions['NETBOX_BASE_IMAGE']}",
            "--build-arg",
            f"NETBOX_BGP_REF={versions['NETBOX_BGP_REF']}",
            "--build-context",
            f"plugin={plugin_repo}",
            "-t",
            netbox_image,
            ".",
        ],
        cwd=_REPO_ROOT,
        log=log,
        env=build_env,
    )
