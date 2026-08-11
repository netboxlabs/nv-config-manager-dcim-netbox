# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Cluster-side operations: helm, kubectl, and the NetBox/NATS wiring."""

from __future__ import annotations

import base64
import json
from pathlib import Path

from nv_config_manager_dcim_netbox.installer.shell import CommandError, run, stream

# Fixed by deploy/netbox/values.yaml (netbox.fullnameOverride), so these names do
# not move with the helm release name.
NETBOX_DEPLOYMENT = "netbox"
NETBOX_CONTAINER = "netbox"
NETBOX_SERVICE = "netbox"

# Created by NVCM's deployer for the bundled NATS. Recent upstream releases create
# these regardless of whether NVCM's bundled DCIM is deployed.
NVCM_NATS_SECRET = "nats-nv-config-manager"


def kubectl(*args: str, namespace: str | None = None, stdin: str | None = None) -> str:
    """Run kubectl, scoped to *namespace* when given."""
    argv = ["kubectl"]
    if namespace:
        argv += ["-n", namespace]
    return run([*argv, *args], stdin=stdin)


def helm_dependency_build(chart_dir: Path) -> None:
    """Fetch the upstream netbox chart if it is not already vendored."""
    if (chart_dir / "charts").is_dir() and any((chart_dir / "charts").glob("netbox-*.tgz")):
        return
    stream(["helm", "dependency", "build", str(chart_dir)])


def install_netbox(
    chart_dir: Path,
    *,
    release: str,
    namespace: str,
    api_token: str,
    image_repository: str,
    image_tag: str,
    nats_password: str | None = None,
    timeout: str = "10m",
) -> None:
    """helm upgrade --install the NetBox release.

    Called twice by `up`: once before NVCM exists (no NATS password, so the
    publisher stays on the `log` backend) and once after, which rolls the pods
    onto the real event bus.
    """
    argv = [
        "helm",
        "upgrade",
        "--install",
        release,
        str(chart_dir),
        "--namespace",
        namespace,
        "--create-namespace",
        "--set",
        f"netbox.superuser.apiToken={api_token}",
        "--set",
        f"netbox.image.repository={image_repository}",
        "--set",
        f"netbox.image.tag={image_tag}",
        "--wait",
        "--timeout",
        timeout,
    ]
    if nats_password:
        argv += ["--set", f"nvcm.natsPassword={nats_password}"]
    stream(argv)


def wait_for_netbox(namespace: str, timeout: str = "10m") -> None:
    """Block until the NetBox Deployment has rolled out."""
    run(
        [
            "kubectl",
            "-n",
            namespace,
            "rollout",
            "status",
            f"deploy/{NETBOX_DEPLOYMENT}",
            f"--timeout={timeout}",
        ]
    )


def run_nvcm_init(namespace: str, api_token: str, *, bootstrap: bool = True) -> str:
    """Run the plugin's nvcm_init inside the NetBox pod.

    Installs the API token and loads bootstrap data. Idempotent and guarded by a
    PostgreSQL advisory lock, so it is safe to re-run and safe to race with the
    pod's own startup migrations.

    The token reaches the pod as a script on stdin rather than as a command
    argument, so it never appears in the pod's process table. Single-quoting is
    safe because tokens are validated to be `nbt_` + alphanumerics + one dot.

    SUPERUSER_NAME is already in the pod's environment (the chart sets it from
    the superuser secret); nvcm_init needs both it and the token.
    """
    flag = "--bootstrap" if bootstrap else "--no-bootstrap"
    script = (
        "set -e\n"
        f"export SUPERUSER_API_TOKEN='{api_token}'\n"
        f"exec /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py nvcm_init {flag}\n"
    )
    return run(
        [
            "kubectl",
            "-n",
            namespace,
            "exec",
            "-i",
            f"deploy/{NETBOX_DEPLOYMENT}",
            "-c",
            NETBOX_CONTAINER,
            "--",
            "sh",
            "-s",
        ],
        stdin=script,
    )


def read_nats_password(namespace: str) -> str | None:
    """Return NVCM's NATS password, or None if NVCM has not been deployed yet."""
    try:
        encoded = kubectl(
            "get",
            "secret",
            NVCM_NATS_SECRET,
            "-o",
            "jsonpath={.data.password}",
            namespace=namespace,
        )
    except CommandError:
        return None
    if not encoded.strip():
        return None
    return base64.b64decode(encoded).decode()


def publish_test_event(namespace: str, subject: str, nats_password: str, device_id: str) -> str:
    """Publish a synthetic DCIMChangeEvent.

    Proves consumer -> provider -> render dispatch without needing a real NetBox
    edit, so it works even before any device exists.
    """
    envelope = json.dumps(
        {
            "provider": "netbox",
            "operation": "update",
            "object_type": "dcim.device",
            "object_id": str(device_id),
            "timestamp": "2026-01-01T00:00:00Z",
            "record": {"id": int(device_id)},
            "changed_fields": ["status"],
            "contract_version": "1.0",
        }
    )
    return run(
        [
            "kubectl",
            "-n",
            namespace,
            "run",
            "nvcm-netbox-verify",
            "--image=natsio/nats-box:latest",
            "--restart=Never",
            "--rm",
            "--attach",
            "--quiet",
            "--command",
            "--",
            "nats",
            "--server",
            "nats://nv-config-manager-nats:4222",
            "--user",
            "nv-config-manager",
            "--password",
            nats_password,
            "pub",
            subject,
            envelope,
        ]
    )


def dcim_consumer_logs(namespace: str, since: str = "60s") -> str:
    """Return recent DCIM render-consumer logs.

    The upstream label is `consumer-nautobot` for legacy reasons; the container it
    selects runs NATS_CONSUMER=dcim and is the DCIM consumer.
    """
    try:
        return kubectl(
            "logs",
            "-l",
            "component=consumer-nautobot",
            "-c",
            "consumer",
            f"--since={since}",
            namespace=namespace,
        )
    except CommandError as exc:
        return exc.output
