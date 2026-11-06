# SPDX-FileCopyrightText: Copyright (c) 2026 NetBox Labs. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""deploy/netbox renders: CNPG cluster and backups, external and bundled
PostgreSQL, remote auth, SPIFFE mounts and the ServiceMonitor gate.

`helm template` only; skipped without helm or the vendored dependency.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = [
    pytest.mark.installer,
    pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed"),
]

_CHART = Path(__file__).resolve().parents[1] / "deploy" / "netbox"

if not (_CHART / "charts").is_dir():  # pragma: no cover - depends on the checkout
    pytestmark.append(pytest.mark.skip(reason="chart deps not fetched (helm dependency build)"))


def _render(*args: str, api_versions: tuple[str, ...] = ()) -> list[dict]:
    argv = ["helm", "template", "netbox", str(_CHART), "--set", "nvcm.natsPassword=pw"]
    for version in api_versions:
        argv += ["--api-versions", version]
    argv += list(args)
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _render_error(*args: str) -> str:
    argv = ["helm", "template", "netbox", str(_CHART), *args]
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    return result.stderr


def _one(docs: list[dict], kind: str, name: str | None = None) -> dict:
    found = [
        d for d in docs if d.get("kind") == kind and (name is None or d["metadata"]["name"] == name)
    ]
    assert len(found) == 1, f"{kind}/{name}: {len(found)} matches"
    return found[0]


def _none(docs: list[dict], kind: str) -> None:
    assert not [d for d in docs if d.get("kind") == kind]


def _netbox_config(docs: list[dict]) -> dict:
    """The upstream chart's rendered configuration ConfigMap, parsed."""
    cm = _one(docs, "ConfigMap", "netbox")
    return yaml.safe_load(cm["data"]["netbox.yaml"])


def _netbox_deployment(docs: list[dict]) -> dict:
    return _one(docs, "Deployment", "netbox")


# -- database ------------------------------------------------------------------------


def test_defaults_render_a_cnpg_cluster_bootstrapped_from_the_shared_secret():
    docs = _render()
    cluster = _one(docs, "Cluster", "cluster-dcim")
    spec = cluster["spec"]
    assert cluster["apiVersion"] == "postgresql.cnpg.io/v1"
    assert spec["instances"] == 1
    assert spec["bootstrap"]["initdb"] == {
        "database": "dcim",
        "owner": "dcim",
        "secret": {"name": "cluster-dcim-app"},
    }
    assert spec["enablePDB"] is False
    assert "backup" not in spec
    _none(docs, "ScheduledBackup")
    # No bundled PostgreSQL alongside it.
    assert not [
        d for d in docs if d.get("kind") == "StatefulSet" and "postgresql" in d["metadata"]["name"]
    ]

    config = _netbox_config(docs)
    db = config["DATABASES"]["default"]
    assert db["HOST"] == "cluster-dcim-rw"
    assert db["NAME"] == "dcim"
    assert db["USER"] == "dcim"
    assert db["PORT"] == 5432
    # The password is read from the Secret's `password` key at startup.
    volumes = {
        v["name"]: v for v in _netbox_deployment(docs)["spec"]["template"]["spec"]["volumes"]
    }
    sources = volumes["secrets"]["projected"]["sources"]
    db_source = next(s for s in sources if s["secret"]["name"] == "cluster-dcim-app")
    assert db_source["secret"]["items"] == [{"key": "password", "path": "db_password"}]


def test_backups_follow_the_shared_cnpg_settings():
    docs = _render(
        "--set",
        "nvcm.database.cnpg.backup.enabled=true",
        "--set",
        "nvcm.database.cnpg.backup.s3.endpoint=https://s3.example.com",
        "--set",
        "nvcm.database.cnpg.backup.s3.bucket=nvcm",
        "--set",
        "nvcm.database.cnpg.backup.s3.path=backups",
    )
    backup = _one(docs, "Cluster", "cluster-dcim")["spec"]["backup"]
    store = backup["barmanObjectStore"]
    assert store["destinationPath"] == "s3://nvcm/backups/cluster-dcim"
    assert store["endpointURL"] == "https://s3.example.com"
    assert store["s3Credentials"]["accessKeyId"] == {
        "name": "cnpg-backup-credentials",
        "key": "ACCESS_KEY_ID",
    }
    assert store["s3Credentials"]["secretAccessKey"]["key"] == "ACCESS_SECRET_KEY"
    assert store["wal"] == {"compression": "gzip"}
    assert backup["retentionPolicy"] == "30d"

    scheduled = _one(docs, "ScheduledBackup", "cluster-dcim-backup")
    assert scheduled["spec"] == {
        "schedule": "0 0 0 * * *",
        "backupOwnerReference": "self",
        "cluster": {"name": "cluster-dcim"},
    }


def test_cluster_sizing_and_labels_are_values():
    docs = _render(
        "--set",
        "nvcm.database.cnpg.instances=3",
        "--set",
        "nvcm.database.cnpg.storage.size=50Gi",
        "--set",
        "nvcm.database.cnpg.storage.storageClass=fast",
        "--set",
        "nvcm.database.cnpg.imageName=registry.example/postgresql:16",
        "--set",
        "nvcm.database.cnpg.inheritedLabels.site=lab1",
    )
    spec = _one(docs, "Cluster", "cluster-dcim")["spec"]
    assert spec["instances"] == 3
    assert spec["storage"] == {"size": "50Gi", "storageClass": "fast"}
    assert spec["imageName"] == "registry.example/postgresql:16"
    assert spec["inheritedMetadata"] == {"labels": {"site": "lab1"}}


def test_external_postgres_renders_no_cluster_and_keeps_the_shared_secret():
    docs = _render(
        "--set",
        "nvcm.database.cnpg.enabled=false",
        "--set",
        "nvcm.database.external.host=pg.example.com",
        "--set",
        "nvcm.database.external.port=5433",
        "--set",
        "netbox.externalDatabase.host=pg.example.com",
        "--set",
        "netbox.externalDatabase.port=5433",
    )
    _none(docs, "Cluster")
    db = _netbox_config(docs)["DATABASES"]["default"]
    assert db["HOST"] == "pg.example.com"
    assert db["PORT"] == 5433
    volumes = {
        v["name"]: v for v in _netbox_deployment(docs)["spec"]["template"]["spec"]["volumes"]
    }
    names = [s["secret"]["name"] for s in volumes["secrets"]["projected"]["sources"]]
    assert "cluster-dcim-app" in names


def test_bundled_postgres_is_available_for_standalone_installs():
    docs = _render(
        "--set", "nvcm.database.cnpg.enabled=false", "--set", "netbox.postgresql.enabled=true"
    )
    _none(docs, "Cluster")
    assert _one(docs, "StatefulSet", "netbox-postgresql")
    assert _netbox_config(docs)["DATABASES"]["default"]["HOST"] == "netbox-postgresql"


def test_conflicting_database_sources_are_rejected():
    assert "both true" in _render_error("--set", "netbox.postgresql.enabled=true")
    assert "No PostgreSQL source" in _render_error("--set", "nvcm.database.cnpg.enabled=false")
    assert "external.host is set" in _render_error(
        "--set", "nvcm.database.external.host=pg.example.com"
    )


# -- routes, auth, spiffe, monitoring --------------------------------------------------


def test_no_httproute_even_with_the_gateway_api():
    docs = _render(api_versions=("gateway.networking.k8s.io/v1",))
    _none(docs, "HTTPRoute")


def test_remote_auth_renders_the_gateway_identity_mapping():
    groups = json.dumps(["nvcm-admins"])
    docs = _render(
        "--set",
        "netbox.remoteAuth.enabled=true",
        "--set",
        "netbox.remoteAuth.header=HTTP_X_AUTH_REQUEST_EMAIL",
        "--set",
        "netbox.remoteAuth.groupHeader=HTTP_X_AUTH_REQUEST_GROUPS",
        "--set",
        "netbox.remoteAuth.groupSeparator=\\,",
        "--set-json",
        f"netbox.remoteAuth.superuserGroups={groups}",
        "--set",
        "netbox.remoteAuth.autoCreateUser=true",
        "--set",
        "netbox.remoteAuth.groupSyncEnabled=true",
        "--set",
        "netbox.logoutRedirectUrl=/oauth2/sign_out",
    )
    config = _netbox_config(docs)
    assert config["REMOTE_AUTH_ENABLED"] is True
    assert config["REMOTE_AUTH_BACKEND"] == ["netbox.authentication.RemoteUserBackend"]
    assert config["REMOTE_AUTH_HEADER"] == "HTTP_X_AUTH_REQUEST_EMAIL"
    assert config["REMOTE_AUTH_GROUP_HEADER"] == "HTTP_X_AUTH_REQUEST_GROUPS"
    assert config["REMOTE_AUTH_GROUP_SEPARATOR"] == ","
    assert config["REMOTE_AUTH_SUPERUSER_GROUPS"] == ["nvcm-admins"]
    assert config["REMOTE_AUTH_AUTO_CREATE_USER"] is True
    assert config["REMOTE_AUTH_GROUP_SYNC_ENABLED"] is True
    assert config["LOGOUT_REDIRECT_URL"] == "/oauth2/sign_out"


def test_remote_auth_is_off_by_default():
    assert _netbox_config(_render())["REMOTE_AUTH_ENABLED"] is False


def test_spiffe_volumes_reach_netbox_and_the_worker():
    volume = json.dumps(
        [{"name": "spiffe-workload-api", "csi": {"driver": "csi.spiffe.io", "readOnly": True}}]
    )
    mount = json.dumps(
        [{"name": "spiffe-workload-api", "mountPath": "/spiffe-workload-api", "readOnly": True}]
    )
    docs = _render(
        "--set-json",
        f"netbox.extraVolumes={volume}",
        "--set-json",
        f"netbox.extraVolumeMounts={mount}",
        "--set-json",
        f"netbox.worker.extraVolumes={volume}",
        "--set-json",
        f"netbox.worker.extraVolumeMounts={mount}",
    )
    for name in ("netbox", "netbox-worker"):
        pod = _one(docs, "Deployment", name)["spec"]["template"]["spec"]
        volumes = {v["name"]: v for v in pod["volumes"]}
        assert volumes["spiffe-workload-api"]["csi"] == {
            "driver": "csi.spiffe.io",
            "readOnly": True,
        }
        mounts = {m["name"]: m for m in pod["containers"][0]["volumeMounts"]}
        assert mounts["spiffe-workload-api"]["mountPath"] == "/spiffe-workload-api"
        assert mounts["spiffe-workload-api"]["readOnly"] is True


def test_servicemonitor_waits_for_the_monitoring_api():
    flags = ("--set", "nvcm.monitoring.enabled=true", "--set", "netbox.metrics.enabled=true")
    without = _render(*flags)
    _none(without, "ServiceMonitor")
    assert _netbox_config(without)["METRICS_ENABLED"] is True

    docs = _render(
        *flags,
        "--set",
        "nvcm.monitoring.labels.release=prom",
        api_versions=("monitoring.coreos.com/v1",),
    )
    monitor = _one(docs, "ServiceMonitor", "netbox")
    assert monitor["metadata"]["labels"]["release"] == "prom"
    assert monitor["spec"]["selector"]["matchLabels"] == {
        "app.kubernetes.io/name": "netbox",
        "app.kubernetes.io/instance": "netbox",
    }
    assert monitor["spec"]["endpoints"][0] == {
        "port": "http",
        "path": "/metrics",
        "interval": "30s",
        "scrapeTimeout": "10s",
    }
    # Matches the labels the upstream Service carries.
    service = _one(docs, "Service", "netbox")
    for key, value in monitor["spec"]["selector"]["matchLabels"].items():
        assert service["metadata"]["labels"][key] == value

    # Off unless both the chart flag and the upstream metrics flag are set.
    _none(
        _render(
            "--set", "nvcm.monitoring.enabled=true", api_versions=("monitoring.coreos.com/v1",)
        ),
        "ServiceMonitor",
    )
