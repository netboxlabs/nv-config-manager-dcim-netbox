# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Cluster-side operations: helm, kubectl, and the NetBox/NATS wiring."""

from __future__ import annotations

import base64
import json
import shlex
from pathlib import Path

from nv_config_manager_dcim_netbox.installer.remote import LocalRunner, Runner
from nv_config_manager_dcim_netbox.installer.shell import CommandError, run, stream

# Fixed by deploy/netbox/values.yaml (netbox.fullnameOverride), so these names do
# not move with the helm release name.
NETBOX_DEPLOYMENT = "netbox"
NETBOX_CONTAINER = "netbox"
NETBOX_SERVICE = "netbox"

# Created by NVCM's deployer for the bundled NATS. Recent upstream releases create
# these regardless of whether NVCM's bundled DCIM is deployed.
NVCM_NATS_SECRET = "nats-nv-config-manager"

# NVCM's nats-box, used to publish the verification event. Both the Deployment
# and its container carry this name.
NATS_BOX_DEPLOYMENT = "nv-config-manager-nats-box"

# Postgres and Valkey passwords, generated on first install and kept across
# uninstall so they stay in step with the data on the PVCs.
# See deploy/netbox/templates/datastore-credentials.yaml.
DATASTORE_SECRET = "netbox-datastores"


def kubectl(
    *args: str,
    namespace: str | None = None,
    stdin: str | None = None,
    runner: Runner | None = None,
) -> str:
    """Run kubectl, scoped to *namespace* when given.

    ``runner`` selects where it runs: locally by default, or on the Air sim's
    NVCM box when an SshRunner is passed.
    """
    argv = ["kubectl"]
    if namespace:
        argv += ["-n", namespace]
    return (runner or LocalRunner()).run([*argv, *args], stdin=stdin)


def helm_dependency_build(chart_dir: Path) -> None:
    """Fetch the upstream netbox chart if it is not already vendored."""
    if (chart_dir / "charts").is_dir() and any((chart_dir / "charts").glob("netbox-*.tgz")):
        return
    stream(["helm", "dependency", "build", str(chart_dir)])


def kind_cluster_exists(name: str) -> bool:
    """Return whether a kind cluster of this name is already running."""
    try:
        return name in run(["kind", "get", "clusters"]).split()
    except CommandError:
        return False


def create_kind_cluster(name: str, config: Path, timeout: str = "5m") -> None:
    """Create the kind cluster NVCM expects.

    Uses NVCM's own `deploy/kind-config.yaml`, which maps the Gateway NodePorts
    (30443/30080) onto host 443/80. A default `kind create cluster` omits those,
    and the deployed Gateway is then unreachable from the host.
    """
    stream(
        ["kind", "create", "cluster", "--name", name, "--config", str(config), "--wait", timeout]
    )


def load_images_into_kind(images: list[str], cluster: str) -> None:
    """Make locally-built images visible to a kind node.

    A kind node has its own image store, and both the NetBox chart and NVCM's
    provider init container pull IfNotPresent. Neither image is in a registry, so
    without this they resolve to a registry pull and fail.
    """
    for image in images:
        stream(["kind", "load", "docker-image", image, "--name", cluster])


def pending_release_status(
    release: str, namespace: str, runner: Runner | None = None
) -> str | None:
    """Return the release's status if helm has it stuck mid-operation.

    A run interrupted during `helm upgrade --install` leaves the release in
    `pending-install` / `pending-upgrade`, and every later attempt fails with
    "another operation (install/upgrade/rollback) is in progress". Detecting it
    lets the caller say which command clears it.
    """
    argv = ["helm", "status", release, "--namespace", namespace, "-o", "json"]
    try:
        payload = json.loads((runner or LocalRunner()).run(argv))
    except (CommandError, ValueError):
        return None
    status = str((payload.get("info") or {}).get("status") or "")
    return status if status.startswith("pending-") else None


def orphaned_data_volumes(release: str, namespace: str, runner: Runner | None = None) -> list[str]:
    """Return datastore PVCs whose credentials Secret is gone.

    The chart keeps `netbox-datastores` across uninstall precisely so the
    passwords keep matching the data on the PVCs. If the volumes are still there
    but that Secret is not, the databases on them were initialised with
    passwords nobody holds any more, and NetBox would crashloop on "password
    authentication failed" until the volumes are deleted.
    """
    try:
        names = kubectl(
            "get",
            "pvc",
            "-o",
            'jsonpath={range .items[*]}{.metadata.name}{"\\n"}{end}',
            namespace=namespace,
            runner=runner,
        ).split()
    except CommandError:
        return []
    volumes = [n for n in names if n.startswith((f"data-{release}-", f"valkey-data-{release}-"))]
    if not volumes:
        return []
    try:
        kubectl("get", "secret", DATASTORE_SECRET, namespace=namespace, runner=runner)
    except CommandError:
        return sorted(volumes)
    return []


def install_netbox(
    chart_ref: str,
    *,
    release: str,
    namespace: str,
    api_token: str,
    image_repository: str,
    image_tag: str | None = None,
    nats_password: str | None = None,
    extra_set: list[str] | None = None,
    timeout: str = "10m",
    runner: Runner | None = None,
) -> None:
    """helm upgrade --install the NetBox release.

    Called twice: once before NVCM exists (no NATS password, so the publisher
    stays on the `log` backend) and once after, which rolls the pods onto the
    real event bus.

    ``chart_ref`` is a path as the *runner* sees it, so the Air path passes the
    chart's location on the sim box rather than a local one.

    An empty ``image_tag`` omits the flag, leaving the chart's own
    ``netbox.image.tag``. Passing ``--set netbox.image.tag=`` would override it with
    an empty string, and the subchart then falls back to its appVersion — a tag that
    was never built.

    ``extra_set`` carries caller-supplied ``key=value`` overrides, appended last so
    they win over the ones built here.
    """
    argv = [
        "helm",
        "upgrade",
        "--install",
        release,
        chart_ref,
        "--namespace",
        namespace,
        "--create-namespace",
        "--set",
        f"netbox.superuser.apiToken={api_token}",
        "--set",
        f"netbox.image.repository={image_repository}",
        "--wait",
        "--timeout",
        timeout,
    ]
    if image_tag:
        argv += ["--set", f"netbox.image.tag={image_tag}"]
    if nats_password:
        argv += ["--set", f"nvcm.natsPassword={nats_password}"]
    for override in extra_set or ():
        argv += ["--set", override]
    (runner or LocalRunner()).stream(argv)


def wait_for_netbox(namespace: str, timeout: str = "10m", runner: Runner | None = None) -> None:
    """Block until the NetBox Deployment has rolled out."""
    kubectl(
        "rollout",
        "status",
        f"deploy/{NETBOX_DEPLOYMENT}",
        f"--timeout={timeout}",
        namespace=namespace,
        runner=runner,
    )


def load_topology(
    blueprint: str,
    namespace: str,
    *,
    deployment_name: str,
    runner: Runner | None = None,
) -> str:
    """Load a mock-topology blueprint into NetBox with the plugin's loader.

    ``blueprint`` is a directory as the *runner* sees it: a local path on the
    kind path, and a path on the sim box on the Air path, where cloud-init has
    already cloned the NVCM repo that holds it.

    This is NVCM's `make topology` for NetBox. Without it NetBox comes up empty,
    and an empty DCIM renders nothing.

    The blueprint is streamed as a tar over ``exec`` rather than copied with
    ``kubectl cp``, because cp needs a pod *name*: resolving one races a rollout
    and can land on a pod that is terminating, which surfaces as a bare exit 137.
    ``exec deploy/...`` always picks a ready pod. It also keeps this to two
    commands with no intermediate value, so the Air path can render it into a
    script instead of a series of round trips.
    """
    remote_dir = "/tmp/blueprint"
    unpack = f"rm -rf {remote_dir} && mkdir -p {remote_dir} && tar -xf - -C {remote_dir}"
    pipeline = (
        f"tar -cf - -C {shlex.quote(blueprint)} . | "
        f"kubectl -n {shlex.quote(namespace)} exec -i deploy/{NETBOX_DEPLOYMENT} "
        f"-c {NETBOX_CONTAINER} -- sh -c {shlex.quote(unpack)}"
    )
    (runner or LocalRunner()).run(["sh", "-c", pipeline])
    return kubectl(
        "exec",
        f"deploy/{NETBOX_DEPLOYMENT}",
        "-c",
        NETBOX_CONTAINER,
        "--",
        "/opt/netbox/venv/bin/python",
        "/opt/netbox/netbox/manage.py",
        "nvcm_load_dcim",
        "/tmp/blueprint",
        "--deployment-name",
        deployment_name,
        # A bulk ingest should not fire one change event per object.
        "--no-events",
        namespace=namespace,
        runner=runner,
    )


def netbox_publisher_status(namespace: str, runner: Runner | None = None) -> tuple[str, bool]:
    """Return NetBox's configured event backend and whether it can actually run.

    The `nats` backend imports nats-py lazily, inside publish(). A NetBox image
    without that package therefore starts, loads the config and looks healthy,
    then loses every change event with ModuleNotFoundError at publish time.

    `verify` publishes its synthetic event through NVCM's nats-box, so it proves
    the consumer and provider work but never touches this path. Checked
    separately for that reason.
    """
    try:
        out = kubectl(
            "exec",
            "-i",
            f"deploy/{NETBOX_DEPLOYMENT}",
            "-c",
            NETBOX_CONTAINER,
            "--",
            "/opt/netbox/venv/bin/python",
            "/opt/netbox/netbox/manage.py",
            "shell",
            "-c",
            "import json;from django.conf import settings;"
            "cfg=settings.PLUGINS_CONFIG.get('netbox_nv_config_manager',{});"
            "ok=True\ntry:\n import nats\nexcept ImportError:\n ok=False\n"
            "print(json.dumps({'backend':cfg.get('event_publisher','log'),'importable':ok}))",
            namespace=namespace,
            runner=runner,
        )
    except CommandError:
        return ("unknown", True)
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            return (str(payload.get("backend", "unknown")), bool(payload.get("importable")))
    return ("unknown", True)


def netbox_service_exists(namespace: str, runner: Runner | None = None) -> bool:
    """Return whether NetBox is reachable in-cluster under its fixed Service name."""
    try:
        kubectl("get", "service", NETBOX_SERVICE, namespace=namespace, runner=runner)
    except CommandError:
        return False
    return True


def run_nvcm_init(
    namespace: str, api_token: str, *, bootstrap: bool = True, runner: Runner | None = None
) -> str:
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
    return kubectl(
        "exec",
        "-i",
        f"deploy/{NETBOX_DEPLOYMENT}",
        "-c",
        NETBOX_CONTAINER,
        "--",
        "sh",
        "-s",
        namespace=namespace,
        stdin=script,
        runner=runner,
    )


def read_nats_password(namespace: str, runner: Runner | None = None) -> str | None:
    """Return NVCM's NATS password, or None if NVCM has not been deployed yet."""
    try:
        encoded = kubectl(
            "get",
            "secret",
            NVCM_NATS_SECRET,
            "-o",
            "jsonpath={.data.password}",
            namespace=namespace,
            runner=runner,
        )
    except CommandError:
        return None
    if not encoded.strip():
        return None
    return base64.b64decode(encoded).decode()


def publish_test_event(
    namespace: str,
    subject: str,
    nats_password: str,
    device_id: str,
    runner: Runner | None = None,
) -> str:
    """Publish a synthetic DCIMChangeEvent.

    Proves consumer -> provider -> render dispatch without needing a real NetBox
    edit, so it works even before any device exists.

    Runs in NVCM's own nats-box, which is already in the namespace with the
    `nats` CLI on it. Spawning a pod from `natsio/nats-box:latest` instead means
    a Docker Hub pull, which fails on an air-gapped cluster and on any host that
    has hit Hub's anonymous rate limit.

    The password reaches the pod on stdin for the same reason ``run_nvcm_init``
    does it: as a command argument it is persisted in the pod spec, where anyone
    with `kubectl get pod -o yaml` can read it.
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
    script = (
        "set -e\n"
        f"export NATS_PASSWORD={shlex.quote(nats_password)}\n"
        "exec nats --server nats://nv-config-manager-nats:4222 "
        f'--user nv-config-manager --password "$NATS_PASSWORD" pub {shlex.quote(subject)} '
        f"{shlex.quote(envelope)}\n"
    )
    return kubectl(
        "exec",
        "-i",
        f"deploy/{NATS_BOX_DEPLOYMENT}",
        "-c",
        NATS_BOX_DEPLOYMENT,
        "--",
        "sh",
        "-s",
        namespace=namespace,
        stdin=script,
        runner=runner,
    )


def dcim_consumer_logs(namespace: str, since: str = "60s", runner: Runner | None = None) -> str:
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
            runner=runner,
        )
    except CommandError as exc:
        return exc.output
