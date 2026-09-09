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
SKIP_BUILD_ENV = "NVCM_NETBOX_SKIP_IMAGE_BUILD"
MIRROR_ENV = "NVCM_NETBOX_BASE_MIRROR"
DEFAULT_MIRROR = "mirror.gcr.io/library"

# Docker Hub bases the build pulls. Only needed on a 429 retry, where each is
# re-pointed at a mirror through BuildKit's FROM override.
_HUB_BASE_IMAGES = ("python:3.13-slim", "alpine:3.20")
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

    if not (candidate / "packages" / "dcim" / "pyproject.toml").is_file():
        raise RuntimeError(
            f"Could not find the NVCM source checkout at {candidate}. "
            "Set NVCM_REPO to the checkout used to install the AIR TUI."
        )
    return candidate


def _resolve_plugin_repo(log: Callable[[str], None], ref: str) -> Path:
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
    if (destination / ".git").is_dir():
        log(f"Checking out the NetBox plugin at {ref}...")
        # By ref, not by branch, so a SHA in versions.env pins the build. GitHub
        # serves a shallow fetch of either.
        _stream(
            ["git", "fetch", "--depth", "1", "origin", ref],
            cwd=destination,
            log=log,
        )
        _stream(["git", "checkout", "--detach", "FETCH_HEAD"], cwd=destination, log=log)
    if not (destination / "pyproject.toml").is_file():
        raise RuntimeError(f"The plugin build context at {destination} is incomplete.")
    return destination


def _load_versions() -> dict[str, str]:
    """Read the checked-in image pins from build/versions.env."""
    values: dict[str, str] = {}
    for line in (_REPO_ROOT / "build" / "versions.env").read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() and not key.lstrip().startswith("#"):
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


class _RateLimited(CommandError):
    """Docker Hub answered a base-image lookup with 429."""


def _mirror_contexts() -> list[str]:
    """BuildKit FROM overrides pointing the Hub bases at a mirror."""
    mirror = os.environ.get(MIRROR_ENV, DEFAULT_MIRROR).rstrip("/")
    args = []
    for image in _HUB_BASE_IMAGES:
        args += ["--build-context", f"{image}=docker-image://{mirror}/{image}"]
    return args


def _build(argv: list[str], *, cwd: Path, log: Callable[[str], None], env: dict[str, str]) -> None:
    """Run a docker build, retrying through a mirror if Hub rate-limits us.

    The bases are usually cached locally already, but BuildKit still resolves
    each tag against the registry, so a 429 fails the build before the layer
    cache is reached.
    """
    try:
        _stream(argv, cwd=cwd, log=log, env=env)
    except _RateLimited:
        mirror = os.environ.get(MIRROR_ENV, DEFAULT_MIRROR)
        log(f"Docker Hub rate-limited the base images; retrying through {mirror}.")
        _stream(argv[:-1] + _mirror_contexts() + [argv[-1]], cwd=cwd, log=log, env=env)


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
    throttled = False
    for line in process.stdout:
        line = line.rstrip()
        if "429 Too Many Requests" in line:
            throttled = True
        log(line)
    returncode = process.wait()
    if returncode:
        if throttled:
            raise _RateLimited(argv, returncode)
        raise CommandError(argv, returncode)


def _image_is_amd64(image: str) -> bool:
    """Whether ``image`` exists locally and matches the sim's architecture."""
    result = subprocess.run(  # noqa: S603 - fixed argv, image name from config
        ["docker", "image", "inspect", image, "--format", "{{.Architecture}}"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and result.stdout.strip() == TARGET_PLATFORM.split("/")[-1]


def ensure_air_images(
    provider_image: str,
    netbox_image: str,
    log: Callable[[str], None],
) -> None:
    """Build the NetBox images for a DSX Air host, using Docker's layer cache."""
    if os.environ.get(SKIP_BUILD_ENV):
        missing = [image for image in (provider_image, netbox_image) if not _image_is_amd64(image)]
        if missing:
            raise RuntimeError(
                f"{SKIP_BUILD_ENV} is set, but these are absent or not "
                f"{TARGET_PLATFORM}: {', '.join(missing)}"
            )
        log(f"{SKIP_BUILD_ENV} set; shipping the images already built.")
        return

    require_tools("docker", "git")
    if not (_REPO_ROOT / "Dockerfile").is_file():
        raise RuntimeError(
            "The NetBox AIR image builder needs this repository's Dockerfile and "
            f"build/ directory, which are not in the wheel. Expected them under {_REPO_ROOT}; "
            "run `nvcm-netbox air` from a source checkout."
        )

    nvcm_repo = _resolve_nvcm_repo()
    versions = _load_versions()
    plugin_repo = _resolve_plugin_repo(log, versions.get("NETBOX_PLUGIN_REF", "main"))
    build_env = {**os.environ, "DOCKER_BUILDKIT": "1"}

    log("Building the NetBox provider image for linux/amd64...")
    _build(
        [
            "docker",
            "build",
            "--platform",
            TARGET_PLATFORM,
            "--build-context",
            f"sdk={nvcm_repo / 'packages' / 'dcim'}",
            "-t",
            provider_image,
            ".",
        ],
        cwd=_REPO_ROOT,
        log=log,
        env=build_env,
    )

    log("Building the NetBox plugin image for linux/amd64...")
    _build(
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
