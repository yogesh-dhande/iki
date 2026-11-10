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

REQUIRED: dict[str, Any] = {
    "environment": "prod",
    "publicUrl": "https://iki.example.com",
    "allowedHosts": ["iki.example.com"],
    "secretKey": {"existingSecret": "iki-secret-key"},
    "encryptionKeys": {"existingSecret": "iki-encryption-keys"},
    "database": {"existingSecret": "iki-db-app", "migrator": {"existingSecret": "iki-db-owner"}},
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


def named(docs: list[Manifest], kind: str, name: str) -> Manifest:
    (found,) = [d for d in by_kind(docs, kind) if d["metadata"]["name"] == name]
    return found


def test_chart_renders_expected_resources() -> None:
    kinds = sorted(f"{d['kind']}/{d['metadata']['name']}" for d in manifests())
    assert kinds == [
        "CronJob/iki-maintenance",
        "Deployment/iki-tasks",
        "Deployment/iki-web",
        "Job/iki-migrate",
        "NetworkPolicy/iki-default-deny",
        "NetworkPolicy/iki-egress",
        "NetworkPolicy/iki-tasks-webhooks",
        "NetworkPolicy/iki-web-identity-provider",
        "PodDisruptionBudget/iki-web",
        "Service/iki-web",
        "ServiceAccount/iki-tasks",
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
        ({"database": {**REQUIRED["database"], "existingSecret": ""}}, "/database/existingSecret"),
        ({"database": {"existingSecret": "iki-db-app"}}, "/database"),
        (
            {"database": {"existingSecret": "x", "migrator": {"existingSecret": ""}}},
            "/database/migrator/existingSecret",
        ),
        ({"encryptionKeys": {"existingSecret": ""}}, "/encryptionKeys/existingSecret"),
        ({"publicUrl": ""}, "/publicUrl"),
        ({"publicUrl": "https://iki.example.com/path?x"}, "/publicUrl"),
    ],
)
def test_required_values_are_enforced_by_the_schema(override: dict[str, Any], message: str) -> None:
    stderr = render(override, ok=False)
    assert message in str(stderr)


def test_database_tls_cannot_be_disabled() -> None:
    stderr = render({"database": {**REQUIRED["database"], "sslmode": "disable"}}, ok=False)
    assert "/database/sslmode" in str(stderr)


def test_migrations_run_as_a_pre_upgrade_hook_that_never_retries() -> None:
    (job,) = by_kind(manifests(), "Job")
    hooks = job["metadata"]["annotations"]["helm.sh/hook"].split(",")
    assert hooks == ["pre-install", "pre-upgrade", "pre-rollback"]
    assert job["spec"]["backoffLimit"] == 0
    pod = job["spec"]["template"]["spec"]
    # In order: wait for Postgres, migrate, sync plugins, then grant the app's role its
    # privileges.
    wait, migrate, sync = pod["initContainers"]
    assert wait["args"] == ["wait_for_database", "--timeout", "300"]
    assert migrate["args"] == ["migrate_deployment"]
    assert sync["args"] == ["sync_plugins"]
    assert "IKI_DB_ANALYST_ROLE" not in {e["name"] for e in sync["env"]}
    (grant,) = pod["containers"]
    assert grant["args"] == ["grant_app_privileges"]


def test_local_plugins_are_refused_unless_allowed() -> None:
    for _, spec in pod_specs(manifests()):
        for container in [*spec.get("initContainers", []), *spec["containers"]]:
            env = {e["name"]: e.get("value") for e in container.get("env", [])}
            if "IKI_ENVIRONMENT" in env:
                assert env["IKI_ALLOW_LOCAL_PLUGINS"] == "false"
    (job,) = by_kind(manifests({"plugins": {"allowLocal": True}}), "Job")
    env = {
        e["name"]: e.get("value") for e in job["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["IKI_ALLOW_LOCAL_PLUGINS"] == "true"
    assert "allowLocal" in str(render({"plugins": {"allowLocal": "yes"}}, ok=False))


@pytest.mark.parametrize("deployment", ["staging", "production"])
def test_the_config_templates_values_render(deployment: str) -> None:
    directory = CHART.parents[1] / "config-template" / "deployments" / deployment
    values: dict[str, Any] = {}
    for name in ("values.yaml", "image.yaml"):
        values.update(yaml.safe_load((directory / name).read_text()))
    helm = shutil.which("helm")
    assert helm
    result = subprocess.run(  # noqa: S603 - fixed argv
        [helm, "template", "iki", str(CHART), "--namespace", "iki", "-f", "-"],
        input=yaml.safe_dump(values),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert f"ghcr.io/acme/iki-{deployment}" in result.stdout


def test_an_analyst_role_reads_the_views() -> None:
    database = {**REQUIRED["database"], "analystRole": "bi_reader"}
    (job,) = by_kind(manifests({"database": database}), "Job")
    sync = job["spec"]["template"]["spec"]["initContainers"][2]
    assert {"name": "IKI_DB_ANALYST_ROLE", "value": "bi_reader"} in sync["env"]
    assert "/database/analystRole" in str(
        render({"database": {**database, "analystRole": "x; DROP"}}, ok=False)
    )


def database_secret(container: Manifest, var: str = "IKI_DATABASE_URL") -> str:
    env = {e["name"]: e for e in container["env"]}
    return str(env[var]["valueFrom"]["secretKeyRef"]["name"])


def test_only_the_migrate_job_connects_as_the_schema_owner() -> None:
    for name, spec in pod_specs(manifests()):
        for container in [*spec.get("initContainers", []), *spec["containers"]]:
            where = f"{name}:{container['name']}"
            if name == "Job/iki-migrate":
                assert database_secret(container) == "iki-db-owner", where
                assert database_secret(container, "IKI_APP_DATABASE_URL") == "iki-db-app", where
            else:
                assert database_secret(container) == "iki-db-app", where
                names = {e["name"] for e in container["env"]}
                assert "IKI_APP_DATABASE_URL" not in names, where


def test_no_container_sets_a_variable_twice() -> None:
    for name, spec in pod_specs(manifests()):
        for container in [*spec.get("initContainers", []), *spec["containers"]]:
            names = [e["name"] for e in container["env"]]
            assert len(names) == len(set(names)), f"{name}:{container['name']}"


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
    deploy = named(docs, "Deployment", "iki-web")
    assert "replicas" not in deploy["spec"]
    assert len(by_kind(docs, "HorizontalPodAutoscaler")) == 1


def test_rollouts_never_drop_below_capacity() -> None:
    deploy = named(manifests(), "Deployment", "iki-web")
    assert deploy["spec"]["strategy"]["rollingUpdate"]["maxUnavailable"] == 0
    container = deploy["spec"]["template"]["spec"]["containers"][0]
    assert container["readinessProbe"]["httpGet"]["path"] == "/healthz/ready"
    assert container["livenessProbe"]["httpGet"]["path"] == "/healthz/live"
    assert container["lifecycle"]["preStop"] == {"sleep": {"seconds": 5}}


def test_database_ca_is_mounted_when_configured() -> None:
    docs = manifests(
        {"database": {**REQUIRED["database"], "caSecret": {"name": "db-ca", "key": "ca.crt"}}}
    )
    for name, spec in pod_specs(docs):
        env = {e["name"]: e.get("value") for e in spec["containers"][0]["env"]}
        assert env["IKI_DB_SSLROOTCERT"] == "/etc/iki/db-ca/ca.crt", name
        mounts = {m["mountPath"] for m in spec["containers"][0]["volumeMounts"]}
        assert "/etc/iki/db-ca" in mounts, name


def test_maintenance_cronjob_runs_hourly() -> None:
    (cron,) = by_kind(manifests(), "CronJob")
    assert cron["spec"]["schedule"] == "17 * * * *"
    assert cron["spec"]["concurrencyPolicy"] == "Forbid"
    (container,) = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"]
    assert container["args"] == ["maintain"]
    assert not by_kind(manifests({"maintenance": {"enabled": False}}), "CronJob")


def test_api_rate_limit_is_configurable() -> None:
    for name, spec in pod_specs(manifests({"api": {"rateLimitPerMinute": 1200}})):
        env = {e["name"]: e.get("value") for e in spec["containers"][0]["env"]}
        assert env["IKI_API_RATE_LIMIT_PER_MINUTE"] == "1200", name


def test_web_keeps_idle_connections_longer_than_load_balancers_do() -> None:
    (web,) = [d for d in by_kind(manifests(), "Deployment") if d["metadata"]["name"] == "iki-web"]
    env = {
        e["name"]: e.get("value") for e in web["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert int(env["IKI_KEEP_ALIVE_SECONDS"]) > 60


def web_env(values: dict[str, Any] | None = None) -> dict[str, Any]:
    (web,) = [
        d for d in by_kind(manifests(values), "Deployment") if d["metadata"]["name"] == "iki-web"
    ]
    containers = web["spec"]["template"]["spec"]["containers"]
    return {e["name"]: e.get("value") for e in containers[0]["env"]}


def test_the_web_app_dev_server_is_off_and_only_for_plain_http() -> None:
    assert "IKI_WEB_DEV_SERVER" not in web_env()
    local = {"https": False, "web": {"devServer": True}}
    assert web_env(local)["IKI_WEB_DEV_SERVER"] == "true"
    refused = render({"web": {"devServer": True}}, ok=False)
    assert "web.devServer is for local development" in str(refused)


def test_oauth_settings_reach_every_container() -> None:
    values = {"oauth": {"accessTokenMinutes": 15, "refreshTokenDays": 7}}
    for name, spec in pod_specs(manifests(values)):
        env = {e["name"]: e.get("value") for e in spec["containers"][0]["env"]}
        assert env["IKI_OAUTH_ACCESS_TOKEN_MINUTES"] == "15", name
        assert env["IKI_OAUTH_REFRESH_TOKEN_DAYS"] == "7", name
        assert env["IKI_OAUTH_CLIENT_METADATA_DOCUMENTS"] == "false", name


def test_oauth_token_lifetimes_are_bounded() -> None:
    error = render({"oauth": {"accessTokenMinutes": 100000}}, ok=False)
    assert "accessTokenMinutes" in str(error)


def test_client_metadata_documents_open_public_https_for_web_pods_only_when_on() -> None:
    def policy(docs: list[Manifest]) -> list[Manifest]:
        return [
            p
            for p in by_kind(docs, "NetworkPolicy")
            if p["metadata"]["name"] == "iki-web-client-metadata"
        ]

    assert not policy(manifests())
    (on,) = policy(manifests({"oauth": {"clientMetadataDocuments": True}}))
    assert on["spec"]["podSelector"]["matchLabels"]["app.kubernetes.io/component"] == "web"
    (rule,) = on["spec"]["egress"]
    assert rule["ports"] == [{"protocol": "TCP", "port": 443}]
    v4, v6 = (peer["ipBlock"] for peer in rule["to"])
    assert "169.254.0.0/16" in v4["except"] and "10.0.0.0/8" in v4["except"]
    assert "fc00::/7" in v6["except"]


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


def test_task_runners_have_probes_a_grace_period_and_enough_connections() -> None:
    docs = manifests({"tasks": {"replicas": 2, "concurrency": 6, "graceSeconds": 40}})
    tasks = named(docs, "Deployment", "iki-tasks")
    assert tasks["spec"]["replicas"] == 2
    assert tasks["spec"]["strategy"]["rollingUpdate"]["maxUnavailable"] == 0
    spec = tasks["spec"]["template"]["spec"]
    assert spec["serviceAccountName"] == "iki-tasks"
    # Longer than the runner's own grace, so it can log what it abandoned.
    assert spec["terminationGracePeriodSeconds"] == 55
    (container,) = spec["containers"]
    assert container["args"] == ["tasks"]
    env = {e["name"]: e.get("value") for e in container["env"]}
    assert env["IKI_TASKS_CONCURRENCY"] == "6"
    assert env["IKI_TASKS_GRACE_SECONDS"] == "40"
    assert env["IKI_DB_POOL_MAX_SIZE"] == "8"
    assert container["ports"] == [{"name": "health", "containerPort": 8001, "protocol": "TCP"}]
    assert container["livenessProbe"]["httpGet"] == {"path": "/healthz/live", "port": "health"}
    assert container["readinessProbe"]["httpGet"] == {"path": "/healthz/ready", "port": "health"}
    # Nothing in the cluster calls the runners, so no Service, and default-deny ingress applies.
    assert "iki-tasks" not in {s["metadata"]["name"] for s in by_kind(docs, "Service")}


def test_task_runners_can_be_turned_off() -> None:
    docs = manifests({"tasks": {"enabled": False}})
    assert {d["metadata"]["name"] for d in by_kind(docs, "Deployment")} == {"iki-web"}


EMAIL = {"email": {"existingSecret": "iki-email", "from": "Iki <iki@example.com>"}}


def test_email_is_off_unless_configured_and_then_reaches_every_container() -> None:
    for name, spec in pod_specs(manifests()):
        env = {e["name"] for e in spec["containers"][0]["env"]}
        assert not env & {"IKI_EMAIL_URL", "IKI_EMAIL_FROM"}, name
    for name, spec in pod_specs(manifests(EMAIL)):
        variables = {e["name"]: e for e in spec["containers"][0]["env"]}
        # The URL carries the mail server's password: from the Secret only.
        assert variables["IKI_EMAIL_URL"]["valueFrom"]["secretKeyRef"] == {
            "name": "iki-email",
            "key": "url",
        }, name
        assert variables["IKI_EMAIL_FROM"]["value"] == "Iki <iki@example.com>", name
        assert variables["IKI_EMAIL_ALLOW_PLAINTEXT"]["value"] == "false", name


def test_email_needs_a_from_address() -> None:
    error = render({"email": {"existingSecret": "iki-email"}}, ok=False)
    assert "from" in str(error)


def test_only_web_pods_and_task_runners_reach_the_mail_server() -> None:
    def policy(docs: list[Manifest]) -> list[Manifest]:
        return [p for p in by_kind(docs, "NetworkPolicy") if p["metadata"]["name"] == "iki-email"]

    assert not policy(manifests())
    (on,) = policy(manifests(EMAIL))
    selector = {e["key"]: e["values"] for e in on["spec"]["podSelector"]["matchExpressions"]}
    assert selector["app.kubernetes.io/component"] == ["web", "tasks"]
    assert selector["app.kubernetes.io/instance"] == ["iki"]
    (rule,) = on["spec"]["egress"]
    assert rule["ports"] == [{"protocol": "TCP", "port": 587}, {"protocol": "TCP", "port": 465}]
    v4, v6 = (peer["ipBlock"] for peer in rule["to"])
    assert "169.254.0.0/16" in v4["except"] and "10.0.0.0/8" in v4["except"]
    assert "fc00::/7" in v6["except"]


def test_webhooks_reach_only_public_addresses_from_task_runners_by_default() -> None:
    policies = {p["metadata"]["name"]: p for p in by_kind(manifests(), "NetworkPolicy")}
    on = policies["iki-tasks-webhooks"]
    assert on["spec"]["podSelector"]["matchLabels"]["app.kubernetes.io/component"] == "tasks"
    (rule,) = on["spec"]["egress"]
    assert rule["ports"] == [{"protocol": "TCP", "port": 443}]
    v4, v6 = (peer["ipBlock"] for peer in rule["to"])
    assert "169.254.0.0/16" in v4["except"] and "10.0.0.0/8" in v4["except"]
    assert "fc00::/7" in v6["except"]
    names = {
        p["metadata"]["name"]
        for p in by_kind(manifests({"tasks": {"enabled": False}}), "NetworkPolicy")
    }
    assert "iki-tasks-webhooks" not in names


def test_webhook_networks_and_internal_ca_reach_every_container() -> None:
    docs = manifests(
        {
            "webhooks": {
                "allowedNetworks": ["10.20.0.0/16", "fd00:1::/64"],
                "caSecret": {"name": "lims-ca", "key": "ca.pem"},
            }
        }
    )
    for name, spec in pod_specs(docs):
        env = {e["name"]: e.get("value") for e in spec["containers"][0]["env"]}
        assert env["IKI_WEBHOOK_ALLOWED_NETWORKS"] == "10.20.0.0/16,fd00:1::/64", name
        assert env["IKI_WEBHOOK_CA_FILE"] == "/etc/iki/webhook-ca/ca.pem", name
        assert env["IKI_WEBHOOK_ALLOW_HTTP"] == "false", name
        mounts = {m["mountPath"] for m in spec["containers"][0]["volumeMounts"]}
        assert "/etc/iki/webhook-ca" in mounts, name
        (volume,) = [v for v in spec["volumes"] if v["name"] == "webhook-ca"]
        assert volume["secret"]["secretName"] == "lims-ca", name
    for name, spec in pod_specs(manifests()):
        names = {e["name"] for e in spec["containers"][0]["env"]}
        assert "IKI_WEBHOOK_CA_FILE" not in names, name


def test_plain_http_webhooks_need_https_off() -> None:
    error = render({"webhooks": {"allowHttp": True}}, ok=False)
    assert "allowHttp" in str(error)
    assert manifests({"https": False, "webhooks": {"allowHttp": True}})


def test_webhook_networks_must_be_cidrs() -> None:
    error = render({"webhooks": {"allowedNetworks": ["lims.internal"]}}, ok=False)
    assert "allowedNetworks" in str(error)


FILES: dict[str, Any] = {"files": {"bucket": "iki-files", "prefix": "prod"}}


def test_files_are_off_unless_a_bucket_is_set_and_then_reach_every_container() -> None:
    for name, spec in pod_specs(manifests()):
        env = {e["name"] for e in spec["containers"][0]["env"]}
        assert not {v for v in env if v.startswith(("IKI_FILES_", "AWS_"))}, name
    for name, spec in pod_specs(manifests(FILES)):
        variables = {e["name"]: e.get("value") for e in spec["containers"][0]["env"]}
        assert variables["IKI_FILES_BUCKET"] == "iki-files", name
        assert variables["IKI_FILES_PREFIX"] == "prod", name
        assert variables["IKI_FILES_ENDPOINT_URL"] == "", name
        assert variables["IKI_FILES_MAX_UPLOAD_SIZE"] == "100GiB", name
        # No keys: on AWS, the service account's IAM role gives access.
        assert "AWS_ACCESS_KEY_ID" not in variables, name


def test_storage_keys_come_only_from_a_secret() -> None:
    values = {
        "files": {
            "bucket": "iki",
            "endpoint": "https://s3.internal.example.com",
            "credentials": {"existingSecret": "iki-s3"},
        }
    }
    for name, spec in pod_specs(manifests(values)):
        variables = {e["name"]: e for e in spec["containers"][0]["env"]}
        assert variables["AWS_ACCESS_KEY_ID"]["valueFrom"]["secretKeyRef"] == {
            "name": "iki-s3",
            "key": "access-key-id",
        }, name
        assert variables["AWS_SECRET_ACCESS_KEY"]["valueFrom"]["secretKeyRef"]["key"] == (
            "secret-access-key"
        ), name
        # Signed URLs name the endpoint unless a public one is given.
        assert variables["IKI_FILES_PUBLIC_ENDPOINT_URL"]["value"] == (
            "https://s3.internal.example.com"
        ), name


def test_only_web_task_and_maintenance_pods_reach_object_storage() -> None:
    def policy(docs: list[Manifest]) -> list[Manifest]:
        return [
            p
            for p in by_kind(docs, "NetworkPolicy")
            if p["metadata"]["name"] == "iki-object-storage"
        ]

    assert not policy(manifests())
    (on,) = policy(manifests(FILES))
    selector = {e["key"]: e["values"] for e in on["spec"]["podSelector"]["matchExpressions"]}
    assert selector["app.kubernetes.io/component"] == ["web", "tasks", "maintenance"]
    (rule,) = on["spec"]["egress"]
    assert rule["ports"] == [{"protocol": "TCP", "port": 443}]
    v4, _ = (peer["ipBlock"] for peer in rule["to"])
    assert "169.254.0.0/16" in v4["except"]


def test_the_upload_size_is_checked_by_the_schema() -> None:
    error = render({"files": {"bucket": "iki", "maxUploadSize": "lots"}}, ok=False)
    assert "maxUploadSize" in str(error)


def test_register_locations_reach_every_container_and_are_checked() -> None:
    values = {"files": {"bucket": "iki", "registerLocations": ["s3://lab-data/runs/", "s3://cro"]}}
    for name, spec in pod_specs(manifests(values)):
        variables = {e["name"]: e.get("value") for e in spec["containers"][0]["env"]}
        assert variables["IKI_FILES_REGISTER_LOCATIONS"] == "s3://lab-data/runs/,s3://cro", name
    for name, spec in pod_specs(manifests(FILES)):
        variables = {e["name"]: e.get("value") for e in spec["containers"][0]["env"]}
        assert variables["IKI_FILES_REGISTER_LOCATIONS"] == "", name
    error = render({"files": {"bucket": "iki", "registerLocations": ["lab-data/runs"]}}, ok=False)
    assert "registerLocations" in str(error)
