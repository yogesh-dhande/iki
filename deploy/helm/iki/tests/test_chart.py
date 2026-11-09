"""Policy tests for the Helm chart: render it with `helm template` and check
the security properties every Iki workload must have.
"""

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

CHART = Path(__file__).resolve().parents[1]

REQUIRED = {
    "environment": "prod",
    "publicUrl": "https://iki.example.com",
    "allowedHosts": ["iki.example.com"],
    "secretKey": {"existingSecret": "iki-secret-key"},
    "encryptionKeys": {"existingSecret": "iki-encryption-keys"},
    "database": {"existingSecret": "iki-db"},
}

Manifest = dict[str, Any]


def render(values: dict[str, Any] | None = None, *, ok: bool = True) -> list[Manifest] | str:
    helm = shutil.which("helm")
    assert helm, "helm is required for chart tests (brew install helm)"
    values_yaml = yaml.safe_dump({**REQUIRED, **(values or {})})
    result = subprocess.run(  # noqa: S603 - fixed argv
        [helm, "template", "iki", str(CHART), "--namespace", "iki", "-f", "-"],
        input=values_yaml,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if not ok:
        assert result.returncode != 0, "expected helm template to fail"
        return result.stderr
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def manifests(values: dict[str, Any] | None = None) -> list[Manifest]:
    docs = render(values)
    assert isinstance(docs, list)
    return docs


def pod_specs(docs: list[Manifest]) -> Iterator[tuple[str, Manifest]]:
    for doc in docs:
        if doc["kind"] in ("Deployment", "Job", "StatefulSet", "DaemonSet"):
            yield f"{doc['kind']}/{doc['metadata']['name']}", doc["spec"]["template"]["spec"]
        elif doc["kind"] == "CronJob":
            template = doc["spec"]["jobTemplate"]["spec"]["template"]
            yield f"CronJob/{doc['metadata']['name']}", template["spec"]


def by_kind(docs: list[Manifest], kind: str) -> list[Manifest]:
    return [d for d in docs if d["kind"] == kind]


def test_chart_renders_expected_resources() -> None:
    kinds = sorted(f"{d['kind']}/{d['metadata']['name']}" for d in manifests())
    assert kinds == [
        "CronJob/iki-maintenance",
        "Deployment/iki-web",
        "Job/iki-migrate",
        "NetworkPolicy/iki-default-deny",
        "NetworkPolicy/iki-egress",
        "NetworkPolicy/iki-web-identity-provider",
        "PodDisruptionBudget/iki-web",
        "Service/iki-web",
        "ServiceAccount/iki-web",
    ]


def test_every_pod_meets_pod_security_restricted() -> None:
    specs = list(pod_specs(manifests()))
    assert specs
    for name, spec in specs:
        psc = spec["securityContext"]
        assert psc["runAsNonRoot"] is True, name
        assert psc["runAsUser"] >= 10000, name
        assert psc["seccompProfile"]["type"] == "RuntimeDefault", name
        assert spec["automountServiceAccountToken"] is False, name
        assert spec["enableServiceLinks"] is False, name
        for container in [*spec.get("initContainers", []), *spec["containers"]]:
            csc = container["securityContext"]
            where = f"{name}:{container['name']}"
            assert csc["allowPrivilegeEscalation"] is False, where
            assert csc["readOnlyRootFilesystem"] is True, where
            assert csc["privileged"] is False, where
            assert csc["capabilities"]["drop"] == ["ALL"], where
            assert "add" not in csc["capabilities"], where
            assert container["resources"]["requests"]["cpu"], where
            assert container["resources"]["limits"]["memory"], where


def test_secrets_are_only_referenced_never_inlined() -> None:
    docs = manifests()
    assert by_kind(docs, "Secret") == []
    for name, spec in pod_specs(docs):
        env = {e["name"]: e for e in spec["containers"][0]["env"]}
        for var in ("IKI_SECRET_KEY", "IKI_DATABASE_URL"):
            assert "value" not in env[var], f"{name}: {var} must come from a Secret"
            assert env[var]["valueFrom"]["secretKeyRef"]["name"], name


def test_image_digest_takes_precedence_over_tag() -> None:
    digest = "sha256:" + "a" * 64
    docs = manifests(
        {"image": {"repository": "ghcr.io/ikisci/iki-server", "tag": "1.0.0", "digest": digest}}
    )
    for name, spec in pod_specs(docs):
        assert spec["containers"][0]["image"] == f"ghcr.io/ikisci/iki-server@{digest}", name


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"environment": ""}, "/environment"),
        ({"allowedHosts": []}, "/allowedHosts"),
        ({"secretKey": {"existingSecret": ""}}, "/secretKey/existingSecret"),
        ({"database": {"existingSecret": ""}}, "/database/existingSecret"),
        ({"encryptionKeys": {"existingSecret": ""}}, "/encryptionKeys/existingSecret"),
        ({"publicUrl": ""}, "/publicUrl"),
        ({"publicUrl": "https://iki.example.com/path?x"}, "/publicUrl"),
    ],
)
def test_required_values_are_enforced_by_the_schema(override: dict[str, Any], message: str) -> None:
    stderr = render(override, ok=False)
    assert message in str(stderr)


def test_database_tls_cannot_be_disabled() -> None:
    stderr = render({"database": {"existingSecret": "iki-db", "sslmode": "disable"}}, ok=False)
    assert "/database/sslmode" in str(stderr)


def test_migrations_run_as_a_pre_upgrade_hook_that_never_retries() -> None:
    (job,) = by_kind(manifests(), "Job")
    hooks = job["metadata"]["annotations"]["helm.sh/hook"].split(",")
    assert hooks == ["pre-install", "pre-upgrade"]
    assert job["spec"]["backoffLimit"] == 0
    pod = job["spec"]["template"]["spec"]
    assert pod["containers"][0]["args"] == ["migrate", "--no-input"]
    (wait,) = pod["initContainers"]
    assert wait["args"] == ["wait_for_database", "--timeout", "300"]


def test_network_policy_denies_by_default_and_allows_only_declared_egress() -> None:
    docs = manifests(
        {
            "otel": {"endpoint": "http://collector:4318"},
            "networkPolicy": {
                "egress": {
                    "database": {"to": [{"ipBlock": {"cidr": "10.0.0.0/24"}}]},
                    "otel": {"to": [{"podSelector": {"matchLabels": {"app": "collector"}}}]},
                }
            },
        }
    )
    policies = {p["metadata"]["name"]: p["spec"] for p in by_kind(docs, "NetworkPolicy")}
    deny = policies["iki-default-deny"]
    assert deny["policyTypes"] == ["Ingress", "Egress"]
    assert "ingress" not in deny
    assert "egress" not in deny

    egress = policies["iki-egress"]["egress"]
    ports = [[p["port"] for p in rule["ports"]] for rule in egress]
    assert ports == [[53, 53], [5432], [4318]]
    assert egress[1]["to"] == [{"ipBlock": {"cidr": "10.0.0.0/24"}}]


def test_no_egress_rules_beyond_dns_unless_configured() -> None:
    (egress,) = [
        p for p in by_kind(manifests(), "NetworkPolicy") if p["metadata"]["name"] == "iki-egress"
    ]
    assert len(egress["spec"]["egress"]) == 1


def test_web_ingress_is_closed_unless_sources_are_listed() -> None:
    names = [p["metadata"]["name"] for p in by_kind(manifests(), "NetworkPolicy")]
    assert "iki-web-ingress" not in names
    docs = manifests(
        {"networkPolicy": {"ingressFrom": [{"namespaceSelector": {"matchLabels": {"x": "y"}}}]}}
    )
    (ingress,) = [
        p for p in by_kind(docs, "NetworkPolicy") if p["metadata"]["name"] == "iki-web-ingress"
    ]
    assert ingress["spec"]["ingress"][0]["ports"] == [{"protocol": "TCP", "port": 8000}]


def test_autoscaling_replaces_fixed_replicas() -> None:
    docs = manifests({"web": {"autoscaling": {"enabled": True}}})
    (deploy,) = by_kind(docs, "Deployment")
    assert "replicas" not in deploy["spec"]
    assert len(by_kind(docs, "HorizontalPodAutoscaler")) == 1


def test_rollouts_never_drop_below_capacity() -> None:
    (deploy,) = by_kind(manifests(), "Deployment")
    assert deploy["spec"]["strategy"]["rollingUpdate"]["maxUnavailable"] == 0
    container = deploy["spec"]["template"]["spec"]["containers"][0]
    assert container["readinessProbe"]["httpGet"]["path"] == "/healthz/ready"
    assert container["livenessProbe"]["httpGet"]["path"] == "/healthz/live"
    assert container["lifecycle"]["preStop"] == {"sleep": {"seconds": 5}}


def test_database_ca_is_mounted_when_configured() -> None:
    docs = manifests(
        {"database": {"existingSecret": "iki-db", "caSecret": {"name": "db-ca", "key": "ca.crt"}}}
    )
    for name, spec in pod_specs(docs):
        env = {e["name"]: e.get("value") for e in spec["containers"][0]["env"]}
        assert env["IKI_DB_SSLROOTCERT"] == "/etc/iki/db-ca/ca.crt", name
        mounts = {m["mountPath"] for m in spec["containers"][0]["volumeMounts"]}
        assert "/etc/iki/db-ca" in mounts, name


def test_maintenance_cronjob_clears_expired_sessions_daily() -> None:
    (cron,) = by_kind(manifests(), "CronJob")
    assert cron["spec"]["concurrencyPolicy"] == "Forbid"
    (container,) = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"]
    assert container["args"] == ["clear_expired_sessions"]
    assert not by_kind(manifests({"maintenance": {"enabled": False}}), "CronJob")


def test_encryption_keys_and_public_url_reach_every_container() -> None:
    for name, pod in pod_specs(manifests()):
        for container in pod.get("initContainers", []) + pod["containers"]:
            env = {e["name"]: e for e in container["env"]}
            assert env["IKI_ENCRYPTION_KEYS"]["valueFrom"]["secretKeyRef"] == {
                "name": "iki-encryption-keys",
                "key": "keys",
            }, name
            assert env["IKI_PUBLIC_URL"]["value"] == "https://iki.example.com", name


def test_only_web_pods_reach_the_identity_provider_and_only_public_https() -> None:
    (policy,) = [
        p
        for p in by_kind(manifests(), "NetworkPolicy")
        if p["metadata"]["name"] == "iki-web-identity-provider"
    ]
    spec = policy["spec"]
    assert spec["podSelector"]["matchLabels"]["app.kubernetes.io/component"] == "web"
    (rule,) = spec["egress"]
    assert rule["ports"] == [{"protocol": "TCP", "port": 443}]
    v4, v6 = (peer["ipBlock"] for peer in rule["to"])
    assert v4["cidr"] == "0.0.0.0/0"
    for private in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"):
        assert private in v4["except"]
    assert v6["cidr"] == "::/0"
    assert "fc00::/7" in v6["except"]
    assert "fe80::/10" in v6["except"]
