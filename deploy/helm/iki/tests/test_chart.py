"""Policy tests for the Helm chart: render it with `helm template` and check
the security properties every Iki workload must have.
"""

import ipaddress
import json
import re
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


def render(
    values: dict[str, Any] | None = None, *, ok: bool = True, api_versions: tuple[str, ...] = ()
) -> list[Manifest] | str:
    helm = shutil.which("helm")
    assert helm, "helm is required for chart tests (brew install helm)"
    values_yaml = yaml.safe_dump({**REQUIRED, **(values or {})})
    apis = [arg for api in api_versions for arg in ("--api-versions", api)]
    result = subprocess.run(  # noqa: S603 - fixed argv
        [helm, "template", "iki", str(CHART), "--namespace", "iki", "-f", "-", *apis],
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


def merged(*values: dict[str, Any]) -> dict[str, Any]:
    """Values merged as Helm merges them: maps key by key, the later winning."""

    def merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
        out = dict(base)
        for key, value in over.items():
            both = isinstance(value, dict) and isinstance(out.get(key), dict)
            out[key] = merge(out[key], value) if both else value
        return out

    out: dict[str, Any] = {}
    for each in values:
        out = merge(out, each)
    return out


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
        "ServiceAccount/iki-maintenance",
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
    image = {"repository": "ghcr.io/yogesh-dhande/iki-server", "tag": "1.0.0", "digest": digest}
    docs = manifests({"image": image})
    for name, spec in pod_specs(docs):
        assert spec["containers"][0]["image"] == f"ghcr.io/yogesh-dhande/iki-server@{digest}", name


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
    # Wait for Postgres, then one transaction: the plugins' migrations, their guards and
    # views, and the app role's grants (ADR 0134).
    (wait,) = pod["initContainers"]
    assert wait["args"] == ["wait_for_database", "--timeout", "300"]
    (migrate,) = pod["containers"]
    assert migrate["args"] == ["migrate_deployment"]
    env = {e["name"] for e in migrate["env"]}
    assert {"IKI_DATABASE_URL", "IKI_APP_DATABASE_URL"} <= env
    assert "IKI_DB_ANALYST_ROLE" not in env


def test_migrator_receives_only_the_reviewed_preflight_revisions() -> None:
    revisions = ["a" * 64, "b" * 64]
    (job,) = by_kind(manifests({"migrate": {"acknowledgePreflight": revisions}}), "Job")
    (migrate,) = job["spec"]["template"]["spec"]["containers"]
    assert migrate["args"] == [
        "migrate_deployment",
        "--acknowledge-preflight",
        revisions[0],
        "--acknowledge-preflight",
        revisions[1],
    ]


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


def test_mirrors_of_ikis_repository_reach_every_server_container() -> None:
    mirrors = ["https://git.acme.example/mirrors/iki.git", "git@git.acme.example:mirrors/iki"]
    for values, expected in (({}, None), ({"plugins": {"mirrors": mirrors}}, ",".join(mirrors))):
        for _, spec in pod_specs(manifests(values)):
            for container in [*spec.get("initContainers", []), *spec["containers"]]:
                env = {e["name"]: e.get("value") for e in container.get("env", [])}
                if "IKI_ENVIRONMENT" in env:
                    assert env.get("IKI_PLUGIN_MIRRORS") == expected
    # Credentials go to git's helper, never into values; one at a time; https or ssh only.
    for bad in (
        "https://user:token@git.acme.example/mirrors/iki.git",
        "http://git.acme.example/mirrors/iki.git",
        "file:///srv/iki.git",
        "https://git.acme.example/a.git,https://git.acme.example/b.git",
    ):
        assert "/plugins/mirrors/0" in str(render({"plugins": {"mirrors": [bad]}}, ok=False)), bad


@pytest.mark.parametrize("deployment", ["staging", "production"])
@pytest.mark.parametrize("workers", [False, True])
def test_the_config_templates_values_render(deployment: str, workers: bool) -> None:
    directory = CHART.parents[1] / "config-template" / "deployments" / deployment
    values = (directory / "values.yaml").read_text()
    if workers:
        # The block its values.yaml says to uncomment once there's a registration token.
        block = re.search(r"(?m)^# backendWorkers:\n(?:# (?:networkPolicy:|  ).*\n)+", values)
        assert block
        values = values.replace(block[0], re.sub(r"(?m)^# ", "", block[0]))
    helm = shutil.which("helm")
    assert helm
    image = str(directory / "image.yaml")
    result = subprocess.run(  # noqa: S603 - fixed argv
        [helm, "template", "iki", str(CHART), "--namespace", "iki", "-f", "-", "-f", image],
        input=values,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert f"image: ghcr.io/acme/iki-{deployment}:" in result.stdout
    assert (f"image: ghcr.io/acme/iki-{deployment}-worker:" in result.stdout) is workers


def test_an_analyst_role_reads_the_views() -> None:
    database = {**REQUIRED["database"], "analystRole": "bi_reader"}
    (job,) = by_kind(manifests({"database": database}), "Job")
    (migrate,) = job["spec"]["template"]["spec"]["containers"]
    assert {"name": "IKI_DB_ANALYST_ROLE", "value": "bi_reader"} in migrate["env"]
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
    # Its own service account, so a cloud role for expiring files is its alone.
    assert cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]["serviceAccountName"] == (
        "iki-maintenance"
    )
    off = manifests({"maintenance": {"enabled": False}})
    assert not by_kind(off, "CronJob")
    assert "iki-maintenance" not in [d["metadata"]["name"] for d in by_kind(off, "ServiceAccount")]


def test_api_rate_limit_is_configurable() -> None:
    for name, spec in pod_specs(manifests({"api": {"rateLimitPerMinute": 1200}})):
        env = {e["name"]: e.get("value") for e in spec["containers"][0]["env"]}
        assert env["IKI_API_RATE_LIMIT_PER_MINUTE"] == "1200", name


def test_client_worker_updates_are_configurable() -> None:
    def env_of(values: dict[str, Any] | None = None) -> dict[str, Any]:
        _, spec = next(pod_specs(manifests(values)))
        return {e["name"]: e.get("value") for e in spec["containers"][0]["env"]}

    default = env_of()
    assert "IKI_WORKER_VERSION" not in default  # the server's own
    assert default["IKI_WORKER_UPDATES"] == "true"
    assert (
        default["IKI_WORKER_DOWNLOADS"] == "https://github.com/yogesh-dhande/iki/releases/download"
    )
    pinned = env_of(
        {
            "selfHostedWorkers": {
                "version": "0.4.1",
                "autoUpdate": False,
                "downloadsUrl": "https://artifacts.example.com/iki",
            }
        }
    )
    assert pinned["IKI_WORKER_VERSION"] == "0.4.1"
    assert pinned["IKI_WORKER_UPDATES"] == "false"
    assert pinned["IKI_WORKER_DOWNLOADS"] == "https://artifacts.example.com/iki"
    refused = render({"selfHostedWorkers": {"downloadsUrl": "http://mirror"}}, ok=False)
    assert "downloadsUrl" in str(refused)


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


def test_bundle_keys_reach_the_server_and_not_workers() -> None:
    staging = "ed25519:riAtz4Nv0SFcXARze-_ieUqB027N1ie892Fk7U0Ao-4"
    qa = "ed25519:" + "A" * 43
    docs = manifests(
        {
            "bundles": {
                "signingKey": {"existingSecret": "iki-bundle-key"},
                "trusted": {"staging": [staging], "qa": [qa, staging]},
            }
        }
    )
    for name, pod in pod_specs(docs):
        for container in pod.get("initContainers", []) + pod["containers"]:
            env = {e["name"]: e for e in container["env"]}
            assert env["IKI_BUNDLE_SIGNING_KEY"]["valueFrom"]["secretKeyRef"] == {
                "name": "iki-bundle-key",
                "key": "key",
            }, name
            assert env["IKI_BUNDLE_TRUSTED_KEYS"]["value"] == (
                f"qa={qa},qa={staging},staging={staging}"
            ), name
    names = {
        e["name"] for _, pod in pod_specs(manifests()) for c in pod["containers"] for e in c["env"]
    }
    assert not {"IKI_BUNDLE_SIGNING_KEY", "IKI_BUNDLE_TRUSTED_KEYS"} & names  # neither by default
    assert "not match" in render({"bundles": {"trusted": {"staging": ["rsa:x"]}}}, ok=False)


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
        assert variables["IKI_FILES_BACKEND"] == "s3", name
        assert variables["IKI_FILES_MAX_UPLOAD_SIZE"] == "100GiB", name
        assert variables["IKI_FILES_ARTIFACT_RETENTION"] == "90d", name
        assert variables["IKI_FILES_ARTIFACT_MIN_RETENTION"] == "", name
        # No keys: on AWS, the service account's IAM role gives access.
        assert "AWS_ACCESS_KEY_ID" not in variables, name


def test_cloud_storage_takes_no_key_and_gs_locations() -> None:
    values = {"files": {"bucket": "iki_files", "backend": "gcs"}}
    for name, spec in pod_specs(manifests(values)):
        variables = {e["name"]: e.get("value") for e in spec["containers"][0]["env"]}
        assert variables["IKI_FILES_BACKEND"] == "gcs", name
        assert variables["IKI_FILES_BUCKET"] == "iki_files", name
        assert "AWS_ACCESS_KEY_ID" not in variables, name
    error = render({"files": {**values["files"], "credentials": {"existingSecret": "k"}}}, ok=False)
    assert "Google service account" in str(error)
    located = {"files": {**values["files"], "registerLocations": ["gs://lab-data/runs/"]}}
    for name, spec in pod_specs(manifests(located)):
        variables = {e["name"]: e.get("value") for e in spec["containers"][0]["env"]}
        assert variables["IKI_FILES_REGISTER_LOCATIONS"] == "gs://lab-data/runs/", name
    error = render({"files": {**values["files"], "registerLocations": ["s3://lab"]}}, ok=False)
    assert "s3://lab must start gs://" in str(error)
    error = render({"files": {"bucket": "iki", "registerLocations": ["gs://lab"]}}, ok=False)
    assert "gs://lab must start s3://" in str(error)
    assert "backend" in str(render({"files": {"bucket": "iki", "backend": "azure"}}, ok=False))


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
        # Workers' name the public one too, unless they have their own.
        assert variables["IKI_FILES_WORKER_ENDPOINT_URL"]["value"] == "", name
    values["files"]["workerEndpoint"] = "https://s3.cluster.example.com"
    for name, spec in pod_specs(manifests(values)):
        variables = {e["name"]: e.get("value") for e in spec["containers"][0]["env"]}
        assert variables["IKI_FILES_WORKER_ENDPOINT_URL"] == "https://s3.cluster.example.com", name
    assert "workerEndpoint" in str(
        render({"files": {"bucket": "iki", "workerEndpoint": "s3.cluster"}}, ok=False)
    )


def test_only_web_task_maintenance_and_worker_pods_reach_object_storage() -> None:
    def policy(docs: list[Manifest]) -> list[Manifest]:
        return [
            p
            for p in by_kind(docs, "NetworkPolicy")
            if p["metadata"]["name"] == "iki-object-storage"
        ]

    assert not policy(manifests())
    (on,) = policy(manifests(FILES))
    selector = {e["key"]: e["values"] for e in on["spec"]["podSelector"]["matchExpressions"]}
    assert selector["app.kubernetes.io/component"] == ["web", "tasks", "maintenance", "worker"]
    (rule,) = on["spec"]["egress"]
    assert rule["ports"] == [{"protocol": "TCP", "port": 443}]
    v4, _ = (peer["ipBlock"] for peer in rule["to"])
    assert "169.254.0.0/16" in v4["except"]


def test_the_upload_size_is_checked_by_the_schema() -> None:
    error = render({"files": {"bucket": "iki", "maxUploadSize": "lots"}}, ok=False)
    assert "maxUploadSize" in str(error)


def test_artifact_retention_is_checked_by_the_schema() -> None:
    error = render({"files": {"bucket": "iki", "artifactRetention": "a year"}}, ok=False)
    assert "artifactRetention" in str(error)
    error = render({"files": {"bucket": "iki", "artifactMinRetention": "0d"}}, ok=False)
    assert "artifactMinRetention" in str(error)


def test_checkpoints_are_saved_as_often_as_set_and_no_more_than_once_a_minute() -> None:
    servers = 0
    for name, spec in pod_specs(manifests()):
        variables = {e["name"]: e.get("value") for e in spec["containers"][0].get("env", [])}
        if "IKI_SECRET_KEY" in variables:
            assert variables["IKI_RUNS_CHECKPOINT_INTERVAL"] == "10m", name
            servers += 1
    assert servers >= 3
    assert "interval" in str(render({"checkpoints": {"interval": "30s"}}, ok=False))
    render({"checkpoints": {"interval": "90s"}})


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


WORKER_IMAGE = "ghcr.io/acme/iki-prod-worker"
#: Where workers and run pods reach the server: the load balancer's own address.
SERVER_PEERS = [{"ipBlock": {"cidr": "203.0.113.10/32"}}]
TO_SERVER: dict[str, Any] = {"networkPolicy": {"egress": {"server": {"to": SERVER_PEERS}}}}
#: And object storage: the store's own addresses.
STORAGE_PEERS = [{"ipBlock": {"cidr": "198.51.100.0/24"}}]
TO_STORAGE: dict[str, Any] = {"networkPolicy": {"egress": {"objectStorage": {"to": STORAGE_PEERS}}}}
WORKERS: dict[str, Any] = {
    "backendWorkers": {
        "enabled": True,
        "image": {"repository": WORKER_IMAGE, "digest": "sha256:" + "b" * 64},
        "registrationToken": {"existingSecret": "iki-worker-registration"},
    },
    **TO_SERVER,
}


def with_workers(**overrides: Any) -> dict[str, Any]:
    return {**WORKERS, "backendWorkers": {**WORKERS["backendWorkers"], **overrides}}


def worker_pod(docs: list[Manifest]) -> Manifest:
    spec: Manifest = named(docs, "Deployment", "iki-worker")["spec"]["template"]["spec"]
    return spec


def worker_env(docs: list[Manifest]) -> dict[str, Manifest]:
    return {e["name"]: e for e in worker_pod(docs)["containers"][0]["env"]}


def test_backend_workers_are_off_unless_turned_on() -> None:
    names = {f"{d['kind']}/{d['metadata']['name']}" for d in manifests(WORKERS)}
    assert names - {f"{d['kind']}/{d['metadata']['name']}" for d in manifests()} == {
        "Deployment/iki-worker",
        "ServiceAccount/iki-worker",
        "NetworkPolicy/iki-worker-egress",
    }


def test_workers_hold_only_their_registration_token() -> None:
    docs = manifests(merged(WORKERS, FILES, TO_STORAGE, EMAIL))
    pod = worker_pod(docs)
    assert pod["serviceAccountName"] == "iki-worker"
    assert pod["automountServiceAccountToken"] is False
    assert named(docs, "ServiceAccount", "iki-worker")["automountServiceAccountToken"] is False
    (container,) = pod["containers"]
    secrets = [
        e["valueFrom"]["secretKeyRef"]["name"]
        for e in container["env"]
        if "secretKeyRef" in e.get("valueFrom", {})
    ]
    assert secrets == ["iki-worker-registration"]
    assert "envFrom" not in container
    env = worker_env(docs)
    assert not {"IKI_DATABASE_URL", "IKI_SECRET_KEY", "IKI_ENCRYPTION_KEYS"} & env.keys()
    assert not any(name.startswith("AWS_") for name in env)


def test_workers_meet_pod_security_restricted_and_write_only_scratch() -> None:
    docs = manifests(WORKERS)
    pod = worker_pod(docs)
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["runAsUser"] == 10001
    assert pod["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    assert pod["enableServiceLinks"] is False
    (container,) = pod["containers"]
    csc = container["securityContext"]
    assert csc["readOnlyRootFilesystem"] is True
    assert csc["allowPrivilegeEscalation"] is False
    assert csc["capabilities"]["drop"] == ["ALL"]
    assert container["resources"]["limits"]["memory"]
    assert container["volumeMounts"] == [{"name": "scratch", "mountPath": "/tmp"}]  # noqa: S108 - an emptyDir
    (scratch,) = pod["volumes"]
    assert scratch["emptyDir"]["sizeLimit"] == "20Gi"
    # Time to stop the command, save its run's checkpoint and report it interrupted.
    assert pod["terminationGracePeriodSeconds"] >= 300


def test_workers_report_what_one_job_may_use() -> None:
    resources = {"requests": {"cpu": "1500m", "memory": "4Gi"}, "limits": {"memory": "4Gi"}}
    env = worker_env(manifests(with_workers(resources=resources, scratch={"sizeLimit": "50Gi"})))
    assert env["IKI_WORKER_CPU"]["value"] == "1.5"
    assert env["IKI_WORKER_MEMORY"]["valueFrom"]["resourceFieldRef"]["resource"] == "limits.memory"
    assert env["IKI_WORKER_SCRATCH_DISK"]["value"] == "50Gi"
    assert env["IKI_WORKER_CLEAN_DIR"]["value"] == "/tmp"  # noqa: S108 - the pod's scratch volume
    assert env["IKI_WORKER_NAME"]["valueFrom"]["fieldRef"]["fieldPath"] == "metadata.name"
    assert "IKI_WORKER_STATE" not in env
    resources["limits"]["cpu"] = "2"
    env = worker_env(manifests(with_workers(resources=resources)))
    assert env["IKI_WORKER_CPU"]["value"] == "2"


def test_workers_run_the_deployments_image_by_digest() -> None:
    docs = manifests(WORKERS)
    image = worker_pod(docs)["containers"][0]["image"]
    assert image == f"{WORKER_IMAGE}@sha256:{'b' * 64}"
    error = render(with_workers(image={"repository": ""}), ok=False)
    assert "backendWorkers.image.repository" in str(error)
    error = render(with_workers(registrationToken={"existingSecret": ""}), ok=False)
    assert "backendWorkers.registrationToken.existingSecret" in str(error)


def test_workers_reach_the_server_only_over_https() -> None:
    assert worker_env(manifests(WORKERS))["IKI_URL"]["value"] == "https://iki.example.com"
    env = worker_env(manifests(with_workers(serverUrl="https://iki.internal.example.com")))
    assert env["IKI_URL"]["value"] == "https://iki.internal.example.com"
    assert "serverUrl" in str(render(with_workers(serverUrl="http://iki-web:8000"), ok=False))
    assert "https://" in str(render({**WORKERS, "publicUrl": "http://iki.example.com"}, ok=False))
    assert "IKI_ALLOW_HTTP" not in worker_env(manifests(WORKERS))


def test_plain_http_to_the_server_only_for_development() -> None:
    dev = with_workers(serverUrl="http://iki-web:8000", allowHttp=True)
    assert "allowHttp" in str(render(dev, ok=False))
    env = worker_env(manifests({**dev, "https": False}))
    assert env["IKI_URL"]["value"] == "http://iki-web:8000"
    assert env["IKI_ALLOW_HTTP"]["value"] == "1"
    env = worker_env(manifests({**with_workers(), "https": False}))
    assert "IKI_ALLOW_HTTP" not in env


def test_an_internal_ca_for_the_server_is_mounted_for_workers() -> None:
    docs = manifests(with_workers(caSecret={"name": "corp-ca", "key": "bundle.pem"}))
    env = worker_env(docs)
    assert env["SSL_CERT_FILE"]["value"] == "/etc/iki/server-ca/bundle.pem"
    pod = worker_pod(docs)
    assert {"name": "server-ca", "mountPath": "/etc/iki/server-ca", "readOnly": True} in pod[
        "containers"
    ][0]["volumeMounts"]
    assert "SSL_CERT_FILE" not in worker_env(manifests(WORKERS))


def test_workers_never_reach_the_database_and_reach_only_dns_and_the_server() -> None:
    ingress = [
        {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "nginx"}}},
        # A load balancer's front ends reach web pods from here; workers don't go there.
        {"ipBlock": {"cidr": "10.0.192.0/22"}},
    ]
    docs = manifests(
        merged(
            WORKERS,
            {
                "networkPolicy": {
                    "ingressFrom": ingress,
                    "egress": {"database": {"to": [{"ipBlock": {"cidr": "10.0.0.0/24"}}]}},
                }
            },
        )
    )
    policies = {p["metadata"]["name"]: p["spec"] for p in by_kind(docs, "NetworkPolicy")}
    selector = {
        e["key"]: (e["operator"], e["values"])
        for e in policies["iki-egress"]["podSelector"]["matchExpressions"]
    }
    # Nor does the export sandbox, which reaches nothing (ADR 0230).
    assert selector["app.kubernetes.io/component"] == ("NotIn", ["worker", "sandbox"])
    worker = policies["iki-worker-egress"]
    assert worker["podSelector"]["matchLabels"]["app.kubernetes.io/component"] == "worker"
    assert worker["policyTypes"] == ["Egress"]
    dns, server = worker["egress"]
    assert [p["port"] for p in dns["ports"]] == [53, 53]
    assert server["ports"] == [{"protocol": "TCP", "port": 443}]
    assert server["to"] == [ingress[0], *SERVER_PEERS]


def test_workers_get_extra_egress_only_when_listed() -> None:
    rule = {"to": [{"ipBlock": {"cidr": "10.20.0.5/32"}}], "ports": [{"port": 27000}]}
    docs = manifests(merged(WORKERS, {"networkPolicy": {"egress": {"workers": [rule]}}}))
    (policy,) = [
        p for p in by_kind(docs, "NetworkPolicy") if p["metadata"]["name"] == "iki-worker-egress"
    ]
    assert policy["spec"]["egress"][-1] == rule
    (policy,) = [
        p
        for p in by_kind(manifests(WORKERS), "NetworkPolicy")
        if p["metadata"]["name"] == "iki-worker-egress"
    ]
    assert len(policy["spec"]["egress"]) == 2


# --- The egress proxy (ADR 0053) ----------------------------------------------------------------

EGRESS: dict[str, Any] = {**WORKERS, "egressProxy": {"enabled": True}}


def test_the_egress_proxy_is_off_unless_turned_on() -> None:
    names = {f"{d['kind']}/{d['metadata']['name']}" for d in manifests(EGRESS)}
    assert names - {f"{d['kind']}/{d['metadata']['name']}" for d in manifests(WORKERS)} == {
        "Deployment/iki-egress-proxy",
        "Service/iki-egress-proxy",
        "PodDisruptionBudget/iki-egress-proxy",
        "ServiceAccount/iki-egress-proxy",
        "NetworkPolicy/iki-egress-proxy",
    }
    for name, spec in pod_specs(manifests(WORKERS)):
        env = {e["name"] for e in spec["containers"][0]["env"]}
        assert not {v for v in env if v.startswith("IKI_EGRESS_")}, name


def test_the_egress_proxy_meets_pod_security_restricted_and_reaches_every_container() -> None:
    docs = manifests(EGRESS)
    pod = named(docs, "Deployment", "iki-egress-proxy")["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True
    (container,) = pod["containers"]
    assert container["args"] == ["egress_proxy"]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert container["readinessProbe"]["tcpSocket"]["port"] == "proxy"
    # Web pods (which lease jobs and say which actions can run) and task runners know it.
    for name, spec in pod_specs(docs):
        if name in ("Deployment/iki-worker",):
            continue
        env = {e["name"]: e.get("value") for e in spec["containers"][0]["env"]}
        assert env["IKI_EGRESS_PROXY_URL"] == "http://iki-egress-proxy:3128", name
    # Workers themselves get it per job, from the server, with the job's credentials.
    assert not {v for v in worker_env(docs) if v.startswith("IKI_EGRESS_")}


def test_an_upstream_proxys_credentials_come_only_from_a_secret() -> None:
    upstream = {"existingSecret": "corporate-proxy", "key": "url"}
    docs = manifests({**EGRESS, "egressProxy": {"enabled": True, "upstreamProxy": upstream}})
    pod = named(docs, "Deployment", "iki-egress-proxy")["spec"]["template"]["spec"]
    env = {e["name"]: e for e in pod["containers"][0]["env"]}
    assert env["IKI_EGRESS_UPSTREAM_PROXY"]["valueFrom"]["secretKeyRef"] == {
        "name": "corporate-proxy",
        "key": "url",
    }
    plain = {"enabled": True, "upstreamProxy": {"url": "http://proxy.corp.example:3128"}}
    docs = manifests({**EGRESS, "egressProxy": plain})
    pod = named(docs, "Deployment", "iki-egress-proxy")["spec"]["template"]["spec"]
    env = {e["name"]: e for e in pod["containers"][0]["env"]}
    assert env["IKI_EGRESS_UPSTREAM_PROXY"]["value"] == "http://proxy.corp.example:3128"
    secret = {"enabled": True, "upstreamProxy": {"url": "http://u:p@proxy.corp.example:3128"}}
    assert "upstreamProxy" in str(render({**EGRESS, "egressProxy": secret}, ok=False))


def test_only_workers_reach_the_egress_proxy_and_only_it_reaches_the_internet() -> None:
    policies = {
        p["metadata"]["name"]: p["spec"] for p in by_kind(manifests(EGRESS), "NetworkPolicy")
    }
    proxy = policies["iki-egress-proxy"]
    assert proxy["podSelector"]["matchLabels"]["app.kubernetes.io/component"] == "egress-proxy"
    (ingress,) = proxy["ingress"]
    (source,) = ingress["from"]
    assert source["podSelector"]["matchLabels"]["app.kubernetes.io/component"] == "worker"
    assert ingress["ports"] == [{"protocol": "TCP", "port": 3128}]
    (out,) = proxy["egress"]
    assert out["ports"] == [{"protocol": "TCP", "port": 443}]
    v4 = out["to"][0]["ipBlock"]
    assert v4["cidr"] == "0.0.0.0/0"
    assert {"10.0.0.0/8", "169.254.0.0/16"} <= set(v4["except"])
    # Workers reach the proxy, and still nothing on the internet themselves.
    worker = policies["iki-worker-egress"]["egress"]
    to_proxy = worker[-1]
    assert to_proxy["to"][0]["podSelector"]["matchLabels"]["app.kubernetes.io/component"] == (
        "egress-proxy"
    )
    assert to_proxy["ports"] == [{"protocol": "TCP", "port": 3128}]
    # The proxy reaches the database (to check credentials and keep what runs reached).
    shared = policies["iki-egress"]["podSelector"]["matchExpressions"]
    assert {
        "key": "app.kubernetes.io/component",
        "operator": "NotIn",
        "values": ["worker", "sandbox"],
    } in shared


def test_the_egress_proxy_calls_outside_apis_with_its_gateway_key() -> None:
    pod = named(manifests(EGRESS), "Deployment", "iki-egress-proxy")["spec"]["template"]["spec"]
    env = {e["name"] for e in pod["containers"][0]["env"]}
    assert not {"IKI_EGRESS_GATEWAY_KEY_FILE", "SSL_CERT_FILE"} & env
    proxy = {
        "enabled": True,
        "gatewayKey": {"existingSecret": "iki-egress-gateway", "key": "key.pem"},
        "caSecret": {"name": "internal-ca", "key": "bundle.pem"},
    }
    docs = manifests({**EGRESS, "egressProxy": proxy})
    pod = named(docs, "Deployment", "iki-egress-proxy")["spec"]["template"]["spec"]
    (container,) = pod["containers"]
    values = {e["name"]: e.get("value") for e in container["env"]}
    assert values["IKI_EGRESS_GATEWAY_KEY_FILE"] == "/etc/iki/egress-gateway/key.pem"
    assert values["SSL_CERT_FILE"] == "/etc/iki/egress-ca/bundle.pem"
    mounts = {m["name"]: m for m in container["volumeMounts"]}
    assert mounts["egress-gateway"]["readOnly"] is True
    volumes = {v["name"]: v for v in pod["volumes"]}
    assert volumes["egress-gateway"]["secret"] == {
        "secretName": "iki-egress-gateway",
        "defaultMode": 0o440,
        "items": [{"key": "key.pem", "path": "key.pem"}],
    }
    assert volumes["egress-ca"]["secret"]["secretName"] == "internal-ca"
    # Nothing else gets the key.
    for name, spec in pod_specs(docs):
        if name != "Deployment/iki-egress-proxy":
            assert all(v["name"] != "egress-gateway" for v in spec.get("volumes", [])), name


# --- Run secrets (ADR 0054) ---------------------------------------------------------------------


def test_only_web_pods_and_the_egress_proxy_hold_run_secrets_mounted_read_only() -> None:
    for name, spec in pod_specs(manifests(EGRESS)):
        names = {e["name"] for e in spec["containers"][0]["env"]}
        assert "IKI_RUN_SECRETS_DIR" not in names, name
    docs = manifests({**EGRESS, "runSecrets": {"existingSecret": "iki-run-secrets"}})
    for name, spec in pod_specs(docs):
        (container,) = spec["containers"]
        env = {e["name"]: e.get("value") for e in container["env"]}
        mounts = {m["name"]: m for m in container.get("volumeMounts", [])}
        # The proxy adds outside APIs' keys to their requests (ADR 0130).
        if name in ("Deployment/iki-web", "Deployment/iki-egress-proxy"):
            assert env["IKI_RUN_SECRETS_DIR"] == "/etc/iki/run-secrets"
            assert mounts["run-secrets"] == {
                "name": "run-secrets",
                "mountPath": "/etc/iki/run-secrets",
                "readOnly": True,
            }
            (volume,) = [v for v in spec["volumes"] if v["name"] == "run-secrets"]
            assert volume["secret"] == {
                "secretName": "iki-run-secrets",
                "defaultMode": 0o440,
                "optional": True,
            }
        else:
            assert "IKI_RUN_SECRETS_DIR" not in env, name
            assert "run-secrets" not in mounts, name
            assert all(v["name"] != "run-secrets" for v in spec.get("volumes", [])), name


# --- The container tier (ADR 0061) ---------------------------------------------------------

CONTAINERS: dict[str, Any] = {
    "containerTier": {"enabled": True, "namespace": "iki-runs"},
    **TO_SERVER,
}


def with_containers(**overrides: Any) -> dict[str, Any]:
    return {**CONTAINERS, "containerTier": {**CONTAINERS["containerTier"], **overrides}}


def where(doc: Manifest) -> str:
    return f"{doc['metadata'].get('namespace', 'iki')}/{doc['kind']}/{doc['metadata']['name']}"


def provisioner_env(docs: list[Manifest]) -> dict[str, Any]:
    pod = named(docs, "Deployment", "iki-provisioner")["spec"]["template"]["spec"]
    return {e["name"]: e.get("value", e.get("valueFrom")) for e in pod["containers"][0]["env"]}


def test_the_container_tier_is_off_unless_turned_on() -> None:
    names = {where(d) for d in manifests(CONTAINERS)} - {where(d) for d in manifests()}
    assert names == {
        "iki/Deployment/iki-provisioner",
        "iki/ServiceAccount/iki-provisioner",
        "iki/NetworkPolicy/iki-provisioner-kubernetes-api",
        "iki-runs/Role/iki-provisioner",
        "iki-runs/RoleBinding/iki-provisioner",
        "iki-runs/ServiceAccount/iki-run",
        "iki-runs/NetworkPolicy/iki-runs",
    }


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"containerTier": {"enabled": True}}, "containerTier.namespace is required"),
        (
            {"containerTier": {"enabled": True, "namespace": "iki"}},
            "must not be this release's namespace",
        ),
        (
            {"containerTier": {"enabled": True, "namespace": "runs", "extraNamespaces": ["iki"]}},
            "extraNamespaces must not include this release's namespace",
        ),
        (
            {
                "containerTier": {
                    "enabled": True,
                    "namespace": "runs",
                    "serverUrl": "http://iki-web.iki.svc:8000",
                }
            },
            "must be an https:// address",
        ),
    ],
)
def test_run_pods_go_in_a_namespace_of_their_own_and_reach_the_server_over_https(
    values: dict[str, Any], message: str
) -> None:
    error = render(values, ok=False)
    assert isinstance(error, str)
    assert message in error


def test_the_provisioner_manages_only_jobs_where_runs_go() -> None:
    docs = manifests(with_containers(extraNamespaces=["gpu"]))
    assert not by_kind(docs, "ClusterRole")
    assert not by_kind(docs, "ClusterRoleBinding")
    roles = by_kind(docs, "Role")
    assert sorted(r["metadata"]["namespace"] for r in roles) == ["gpu", "iki-runs"]
    for role in roles:
        assert role["rules"] == [
            {
                "apiGroups": ["batch"],
                "resources": ["jobs"],
                "verbs": ["create", "get", "list", "watch", "patch", "delete"],
            },
            {
                "apiGroups": [""],
                "resources": ["pods"],
                "verbs": ["get", "list", "watch", "patch"],
            },
            # Never read, change or delete a Secret: only make each Job's own.
            {"apiGroups": [""], "resources": ["secrets"], "verbs": ["create"]},
            # Setup builds' disks, and their snapshots (ADR 0075).
            {
                "apiGroups": [""],
                "resources": ["persistentvolumeclaims"],
                "verbs": ["create", "get", "list", "delete"],
            },
            {
                "apiGroups": ["snapshot.storage.k8s.io"],
                "resources": ["volumesnapshots"],
                "verbs": ["create", "get", "list", "delete"],
            },
        ]
    for binding in by_kind(docs, "RoleBinding"):
        assert binding["subjects"] == [
            {"kind": "ServiceAccount", "name": "iki-provisioner", "namespace": "iki"}
        ]
    pod = named(docs, "Deployment", "iki-provisioner")["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "iki-provisioner"
    assert pod["automountServiceAccountToken"] is True
    assert pod["containers"][0]["args"] == ["provision"]
    # Run pods never get a Kubernetes token.
    for account in [
        d for d in by_kind(docs, "ServiceAccount") if d["metadata"]["name"] == "iki-run"
    ]:
        assert account["automountServiceAccountToken"] is False
    env = provisioner_env(docs)
    assert env["IKI_RUNS_NAMESPACE"] == "iki-runs"
    assert env["IKI_RUNS_NAMESPACES"] == "gpu"
    # The API and tasks know where runs go too: which version of an environment's
    # volumes machines there get (ADR 0075).
    for name in ("iki-web", "iki-tasks"):
        pod = named(docs, "Deployment", name)["spec"]["template"]["spec"]
        theirs = {e["name"]: e.get("value") for e in pod["containers"][0]["env"]}
        assert (theirs["IKI_RUNS_NAMESPACE"], theirs["IKI_RUNS_NAMESPACES"]) == ("iki-runs", "gpu")
    assert env["IKI_RUNS_SERVER_URL"] == "https://iki.example.com"
    assert env["IKI_RUNS_SERVICE_ACCOUNT"] == "iki-run"
    assert "IKI_RUNS_ALLOW_HTTP" not in env


def test_runs_copy_iki_worker_from_iki_s_own_image() -> None:
    chart = yaml.safe_load((CHART / "Chart.yaml").read_text())
    env = provisioner_env(manifests(CONTAINERS))
    assert env["IKI_RUNS_WORKER_IMAGE"] == (
        f"ghcr.io/yogesh-dhande/iki-worker:{chart['appVersion']}"
    )
    digest = "sha256:" + "c" * 64
    pinned = with_containers(workerImage={"digest": digest})
    assert provisioner_env(manifests(pinned))["IKI_RUNS_WORKER_IMAGE"] == (
        f"ghcr.io/yogesh-dhande/iki-worker@{digest}"
    )


def test_runs_reach_only_the_server_storage_and_the_egress_proxy() -> None:
    values = merged(
        CONTAINERS,
        FILES,
        {
            "egressProxy": {"enabled": True},
            "networkPolicy": {
                "enabled": True,
                "egress": {
                    "objectStorage": {
                        "ports": [8333],
                        "to": [{"podSelector": {"matchLabels": {"app": "seaweedfs"}}}],
                    }
                },
            },
        },
    )
    docs = manifests(values)
    runs = next(
        d for d in by_kind(docs, "NetworkPolicy") if where(d) == "iki-runs/NetworkPolicy/iki-runs"
    )
    assert runs["spec"]["podSelector"] == {}
    assert runs["spec"]["policyTypes"] == ["Ingress", "Egress"]
    assert "ingress" not in runs["spec"]
    peers = [rule["to"] for rule in runs["spec"]["egress"]]
    # Pods named without a namespace are in Iki's own.
    assert [
        {
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "iki"}},
            "podSelector": {"matchLabels": {"app": "seaweedfs"}},
        }
    ] in peers
    proxy = named(docs, "NetworkPolicy", "iki-egress-proxy-from-runs")
    assert proxy["spec"]["ingress"][0]["from"] == [
        {
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "iki-runs"}},
            "podSelector": {"matchLabels": {"app.kubernetes.io/component": "run"}},
        }
    ]
    # Straight to the web pods, only when asked.
    assert not [
        d for d in by_kind(docs, "NetworkPolicy") if d["metadata"]["name"] == "iki-web-from-runs"
    ]
    # With no server peers: they reach the web pods instead.
    direct = {"containerTier": {**CONTAINERS["containerTier"], "serverInCluster": True}}
    docs = manifests(direct)
    assert named(docs, "NetworkPolicy", "iki-web-from-runs")["spec"]["ingress"][0]["ports"] == [
        {"protocol": "TCP", "port": 8000}
    ]
    kube = named(docs, "NetworkPolicy", "iki-provisioner-kubernetes-api")
    assert (
        kube["spec"]["podSelector"]["matchLabels"]["app.kubernetes.io/component"] == "provisioner"
    )


# --- What workers and run pods reach (ADR 0155) -------------------------------------------------

#: The labels the provisioner gives run pods (iki_server.runs.kubernetes.MANAGED).
RUN_LABELS = {
    "app.kubernetes.io/managed-by": "iki-provisioner",
    "app.kubernetes.io/component": "run",
}
Reach = set[tuple[str, str, int | str]]


def selects(selector: Manifest, labels: dict[str, str]) -> bool:
    """Whether a label selector matches these labels, as Kubernetes matches them."""
    if any(labels.get(k) != v for k, v in selector.get("matchLabels", {}).items()):
        return False
    for e in selector.get("matchExpressions", []):
        key, values = e["key"], e.get("values", [])
        matched = {
            "In": key in labels and labels[key] in values,
            "NotIn": key not in labels or labels[key] not in values,
            "Exists": key in labels,
            "DoesNotExist": key not in labels,
        }[e["operator"]]
        if not matched:
            return False
    return True


def egress_of(docs: list[Manifest], namespace: str, labels: dict[str, str]) -> Reach:
    """Every (peer, protocol, port) a pod may reach: the union over every NetworkPolicy in
    its namespace that selects it and limits its egress. A rule without peers or ports
    reaches "*" there."""
    reach: Reach = set()
    limited = False
    for policy in by_kind(docs, "NetworkPolicy"):
        spec = policy["spec"]
        types = spec.get("policyTypes") or ["Ingress", *(["Egress"] if "egress" in spec else [])]
        if policy["metadata"].get("namespace", "iki") != namespace or "Egress" not in types:
            continue
        if not selects(spec["podSelector"], labels):
            continue
        limited = True
        for rule in spec.get("egress", []):
            peers = [json.dumps(p, sort_keys=True) for p in rule.get("to", [])] or ["*"]
            ports = [(p.get("protocol", "TCP"), p.get("port", "*")) for p in rule.get("ports", [])]
            reach |= {(peer, *port) for peer in peers for port in ports or [("*", "*")]}
    assert limited, f"no policy limits the egress of {labels} in {namespace}"
    return reach


def reach(peers: list[Manifest], *ports: tuple[str, int]) -> Reach:
    return {(json.dumps(p, sort_keys=True), *port) for p in peers for port in ports}


def worker_labels(docs: list[Manifest]) -> dict[str, str]:
    labels: dict[str, str] = named(docs, "Deployment", "iki-worker")["spec"]["template"][
        "metadata"
    ]["labels"]
    return labels


def test_workers_and_runs_reach_only_the_server_storage_and_the_proxy() -> None:
    nginx = {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "ingress-nginx"}}}
    values = merged(
        WORKERS,
        CONTAINERS,
        FILES,
        TO_STORAGE,
        EMAIL,
        {
            "egressProxy": {"enabled": True},
            "otel": {"endpoint": "http://collector:4318"},
            "oauth": {"clientMetadataDocuments": True},
            "networkPolicy": {
                # An ingress controller in the cluster, and a load balancer's front ends.
                "ingressFrom": [nginx, {"ipBlock": {"cidr": "10.0.192.0/22"}}],
                "egress": {
                    "database": {"to": [{"ipBlock": {"cidr": "10.0.208.0/22"}}]},
                    "otel": {"to": [{"podSelector": {"matchLabels": {"app": "collector"}}}]},
                    "podIdentity": {"to": [{"ipBlock": {"cidr": "169.254.170.23/32"}}]},
                },
            },
        },
    )
    docs = manifests(values)
    dns = [
        {
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
            "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
        }
    ]
    proxy = {
        "podSelector": {
            "matchLabels": named(docs, "Deployment", "iki-egress-proxy")["spec"]["selector"][
                "matchLabels"
            ]
        }
    }
    https = ("TCP", 443)
    common = (
        reach(dns, ("UDP", 53), ("TCP", 53))
        | reach([nginx, *SERVER_PEERS], https)
        | reach(STORAGE_PEERS, https)
    )

    assert egress_of(docs, "iki", worker_labels(docs)) == common | reach([proxy], ("TCP", 3128))
    in_iki = {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "iki"}}}
    assert egress_of(docs, "iki-runs", RUN_LABELS) == common | reach(
        [{**in_iki, **proxy}], ("TCP", 3128)
    )


BROAD_V4 = {
    "cidr": "0.0.0.0/0",
    "except": [
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",
        "169.254.0.0/16",
        "127.0.0.0/8",
    ],
}
BROAD_V6 = {"cidr": "::/0", "except": ["fc00::/7", "fe80::/10", "::1/128"]}


def egress(**values: Any) -> dict[str, Any]:
    return {"networkPolicy": {"egress": values}}


@pytest.mark.parametrize(
    ("values", "message"),
    [
        # Public addresses: everywhere but private ranges.
        (
            egress(server={"to": [{"ipBlock": BROAD_V4}, {"ipBlock": BROAD_V6}]}),
            "networkPolicy.egress.server.to: 0.0.0.0/0 is wider",
        ),
        (
            egress(server={"to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": ["10.0.0.0/8"]}}]}),
            "networkPolicy.egress.server.to: 0.0.0.0/0 is wider",
        ),
        (
            egress(server={"to": [*SERVER_PEERS, {"ipBlock": {"cidr": "::/0"}}]}),
            "networkPolicy.egress.server.to: ::/0 is wider",
        ),
        (
            egress(server={"to": [{"ipBlock": {"cidr": "2001:db8::/31"}}]}),
            "networkPolicy.egress.server.to: 2001:db8::/31 is wider",
        ),
        (
            egress(server={"to": [{"ipBlock": {"cidr": "10.0.0.0/7"}}]}),
            "networkPolicy.egress.server.to: 10.0.0.0/7 is wider",
        ),
        # Every IPv4 address, written as IPv6.
        (
            egress(server={"to": [{"ipBlock": {"cidr": "::ffff:0:0/96"}}]}),
            "networkPolicy.egress.server.to: ::ffff:0:0/96 is in ::/16",
        ),
        # The chart's own default for object storage, which web pods may keep.
        (FILES, "networkPolicy.egress.objectStorage.to: 0.0.0.0/0 is wider"),
        (
            egress(
                workers=[{"to": [{"ipBlock": {"cidr": "0.0.0.0/1"}}], "ports": [{"port": 443}]}]
            ),
            "networkPolicy.egress.workers[0].to: 0.0.0.0/1 is wider",
        ),
        (
            egress(workers=[{"to": SERVER_PEERS}, {"ports": [{"port": 27000}]}]),
            "networkPolicy.egress.workers[1].to: list where workers and run pods may go",
        ),
        (egress(server={"to": []}), "networkPolicy.egress.server.to: workers and run pods reach"),
    ],
)
@pytest.mark.parametrize("tier", ["workers", "containers"])
def test_workers_and_runs_reach_nothing_wider_than_the_bound(
    tier: str, values: dict[str, Any], message: str
) -> None:
    error = render(merged(WORKERS if tier == "workers" else CONTAINERS, values), ok=False)
    assert isinstance(error, str)
    assert message in error
    assert "ADR 0155" in error


def test_run_pods_reach_the_web_pods_directly_with_no_server_peers() -> None:
    docs = manifests(merged(with_containers(serverInCluster=True), egress(server={"to": []})))
    in_iki = {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "iki"}}}
    web = {
        **in_iki,
        "podSelector": {
            "matchLabels": {
                "app.kubernetes.io/name": "iki",
                "app.kubernetes.io/instance": "iki",
                "app.kubernetes.io/component": "web",
            }
        },
    }
    assert reach([web], ("TCP", 8000)) <= egress_of(docs, "iki-runs", RUN_LABELS)
    # Backend workers still need them.
    error = render(
        merged(WORKERS, with_containers(serverInCluster=True), egress(server={"to": []})), ok=False
    )
    assert "networkPolicy.egress.server.to: workers and run pods reach" in str(error)


def test_blocks_as_wide_as_the_bound_are_allowed() -> None:
    edge = [{"ipBlock": {"cidr": "10.0.0.0/8"}}, {"ipBlock": {"cidr": "2001:db8::/32"}}]
    docs = manifests(merged(WORKERS, egress(server={"to": edge})))
    assert reach(edge, ("TCP", 443)) <= egress_of(docs, "iki", worker_labels(docs))


POLICY_API = "policy.sigstore.dev/v1beta1/ClusterImagePolicy"
APPROVED = {
    "repository": "ghcr.io/acme/iki-production-approvals",
    "subject": "https://github.com/acme/iki-config/.github/workflows/build.yml@refs/heads/main",
}


def with_policy(**policy: Any) -> dict[str, Any]:
    return with_containers(imagePolicy={"enabled": True, "approvals": APPROVED, **policy})


def test_run_pods_admit_only_images_iki_signed_or_the_build_approved() -> None:
    docs = render(with_policy(), api_versions=(POLICY_API,))
    assert isinstance(docs, list)
    (policy,) = by_kind(docs, "ClusterImagePolicy")
    assert policy["apiVersion"] == "policy.sigstore.dev/v1beta1"
    spec = policy["spec"]
    assert spec["images"] == [{"glob": "**"}]
    # It checks Iki's run pods, and Jobs that make them, wherever they are.
    managed = {"matchLabels": {"app.kubernetes.io/managed-by": "iki-provisioner"}}
    assert spec["match"] == [
        {"group": "batch", "version": "v1", "resource": "jobs", "selector": managed},
        {"version": "v1", "resource": "pods", "selector": managed},
    ]
    iki, approved = spec["authorities"]
    assert iki["keyless"]["identities"] == [
        {
            "issuer": "https://token.actions.githubusercontent.com",
            "subjectRegExp": (
                r"^https://github\.com/yogesh-dhande/iki/\.github/workflows/publish\.yml@refs/tags/v"
            ),
        }
    ]
    assert "source" not in iki
    assert approved["keyless"]["identities"] == [
        {"issuer": "https://token.actions.githubusercontent.com", "subject": APPROVED["subject"]}
    ]
    assert approved["source"] == [{"oci": APPROVED["repository"]}]
    for authority in (iki, approved):
        assert authority["keyless"]["url"] == "https://fulcio.sigstore.dev"
        assert authority["ctlog"] == {"url": "https://rekor.sigstore.dev"}
    # The provisioner's Jobs carry the label it matches.
    from iki_server.runs.kubernetes import MANAGED

    assert MANAGED["app.kubernetes.io/managed-by"] == "iki-provisioner"


def test_the_image_policy_is_off_unless_turned_on() -> None:
    assert not by_kind(manifests(CONTAINERS), "ClusterImagePolicy")


@pytest.mark.parametrize(
    ("values", "api_versions", "message"),
    [
        (with_policy(), (), "needs Sigstore's policy-controller"),
        (
            with_policy(approvals={"repository": ""}),
            (POLICY_API,),
            "approvals.repository is required",
        ),
        (
            with_policy(approvals={"repository": APPROVED["repository"]}),
            (POLICY_API,),
            "needs subject (or subjectRegExp)",
        ),
        (
            with_policy(approvals={**APPROVED, "repository": "not a repository"}),
            (POLICY_API,),
            "repository",
        ),
    ],
)
def test_the_image_policy_needs_its_controller_and_the_builds_identity(
    values: dict[str, Any], api_versions: tuple[str, ...], message: str
) -> None:
    error = render(values, ok=False, api_versions=api_versions)
    assert isinstance(error, str)
    assert message in error


@pytest.mark.parametrize("authority", ["iki", "approvals"])
def test_a_signers_subject_meets_every_identity_constraint(authority: str) -> None:
    both = {"subject": "workflow-main", "subjectRegExp": "^workflow-"}
    if authority == "approvals":
        both["repository"] = APPROVED["repository"]
    docs = render(with_policy(**{authority: both}), api_versions=(POLICY_API,))
    assert isinstance(docs, list)
    [policy] = by_kind(docs, "ClusterImagePolicy")
    actual = policy["spec"]["authorities"][0 if authority == "iki" else 1]
    assert actual["keyless"]["identities"] == [
        {"issuer": "https://token.actions.githubusercontent.com", "subject": "workflow-main"}
    ]
    for exact, pattern in (("workflow-main", "^other$"), (None, None)):
        invalid = {**both, "subject": exact, "subjectRegExp": pattern}
        error = render(with_policy(**{authority: invalid}), api_versions=(POLICY_API,), ok=False)
        assert isinstance(error, str) and "subject" in error


def test_slurm_clusters_workers_learn_who_signs_images_without_the_controller() -> None:
    # A Slurm cluster's worker checks images itself (ADR 0133): the server tells it whom
    # to trust once the deployment names its build's signer, controller or not.
    docs = manifests({"containerTier": {"imagePolicy": {"approvals": APPROVED}}})
    assert not by_kind(docs, "ClusterImagePolicy")
    web = named(docs, "Deployment", "iki-web")["spec"]["template"]["spec"]
    env = {e["name"]: e.get("value") for e in web["containers"][0]["env"]}
    assert json.loads(env["IKI_IMAGE_POLICY"]) == {
        "iki": {
            "issuer": "https://token.actions.githubusercontent.com",
            "subject_regexp": (
                r"^https://github\.com/yogesh-dhande/iki/\.github/workflows/publish\.yml"
                r"@refs/tags/v"
            ),
        },
        "approvals": {
            "issuer": "https://token.actions.githubusercontent.com",
            **APPROVED,
        },
    }
    for _, spec in pod_specs(manifests()):
        assert "IKI_IMAGE_POLICY" not in {e["name"] for e in spec["containers"][0]["env"]}


# --- The VM tier (ADR 0074) ----------------------------------------------------------------

ROLE = "arn:aws:iam::123456789012:role/iki-provisioner"
VMS: dict[str, Any] = {
    "vmTier": {
        "enabled": True,
        "owner": "iki-production",
        "serverUrl": "https://iki.internal.example.com",
        "egressProxyUrl": "http://iki-egress.internal.example.com:3128",
        "region": "us-east-1",
        "serviceAccountAnnotations": {"eks.amazonaws.com/role-arn": ROLE},
    }
}


def test_the_vm_tier_runs_the_provisioner_with_no_kubernetes_access() -> None:
    docs = manifests({**VMS, "networkPolicy": {"enabled": True}})
    names = {where(d) for d in docs} - {
        where(d) for d in manifests({"networkPolicy": {"enabled": True}})
    }
    assert names == {
        "iki/Deployment/iki-provisioner",
        "iki/ServiceAccount/iki-provisioner",
        "iki/NetworkPolicy/iki-provisioner-cloud-api",
    }
    assert not by_kind(docs, "Role")
    account = named(docs, "ServiceAccount", "iki-provisioner")
    assert account["metadata"]["annotations"] == {"eks.amazonaws.com/role-arn": ROLE}
    env = provisioner_env(docs)
    assert env["IKI_RUNS_VM_OWNER"] == "iki-production"
    assert env["IKI_RUNS_VM_SERVER_URL"] == "https://iki.internal.example.com"
    assert env["IKI_RUNS_VM_EGRESS_PROXY_URL"] == "http://iki-egress.internal.example.com:3128"
    assert env["AWS_REGION"] == "us-east-1"
    assert env["IKI_RUNS_WORKER_IMAGE"].startswith("ghcr.io/yogesh-dhande/iki-worker:")
    assert "IKI_RUNS_NAMESPACE" not in env
    # It reaches AWS's APIs over HTTPS, but not the node's own metadata.
    cloud = named(docs, "NetworkPolicy", "iki-provisioner-cloud-api")["spec"]
    assert cloud["egress"][0]["ports"] == [{"protocol": "TCP", "port": 443}]
    assert "169.254.0.0/16" in cloud["egress"][0]["to"][0]["ipBlock"]["except"]


GCE: dict[str, Any] = {
    "vmTier": {
        "enabled": True,
        "cloud": "gcp",
        "owner": "iki-production",
        "gcp": {
            "project": "iki-prod",
            "instanceTemplate": "https://www.googleapis.com/compute/v1/projects/iki-prod/regions/europe-west1/instanceTemplates/iki-production-vm-1",
            "kmsKey": "projects/iki-prod/locations/europe-west1/keyRings/iki/cryptoKeys/vm-disks",
        },
        "serviceAccountAnnotations": {
            "iam.gke.io/gcp-service-account": (
                "iki-production-provisioner@iki-prod.iam.gserviceaccount.com"
            )
        },
    }
}


def test_the_vm_tier_on_compute_engine() -> None:
    docs = manifests({**GCE, "networkPolicy": {"enabled": True}})
    env = provisioner_env(docs)
    assert env["IKI_RUNS_VM_CLOUD"] == "gcp"
    assert env["IKI_RUNS_VM_GCP_PROJECT"] == "iki-prod"
    assert env["IKI_RUNS_VM_GCP_INSTANCE_TEMPLATE"].endswith(
        "/instanceTemplates/iki-production-vm-1"
    )
    assert env["IKI_RUNS_VM_GCP_KMS_KEY"].endswith("/cryptoKeys/vm-disks")
    assert "AWS_REGION" not in env
    account = named(docs, "ServiceAccount", "iki-provisioner")
    assert account["metadata"]["annotations"] == GCE["vmTier"]["serviceAccountAnnotations"]
    # On EC2 nothing changes.
    assert "IKI_RUNS_VM_CLOUD" not in provisioner_env(manifests(VMS))


def test_every_server_pod_knows_whose_machines_the_vm_tier_starts_and_where() -> None:
    """The API and tasks say where an environment's setup builds go, and which version of
    its volumes machines there get, by the VM tier's owner and cloud: without them, every
    VM-tier setup build was refused as having nowhere to go. How machines start (their
    template, key, server address and CA) stays the provisioner's alone."""
    docs = manifests(GCE)
    for name in ("iki-web", "iki-tasks", "iki-provisioner"):
        pod = named(docs, "Deployment", name)["spec"]["template"]["spec"]
        env = {e["name"]: e.get("value") for e in pod["containers"][0]["env"]}
        assert (
            env["IKI_RUNS_VM_OWNER"],
            env["IKI_RUNS_VM_CLOUD"],
            env["IKI_RUNS_VM_GCP_PROJECT"],
        ) == ("iki-production", "gcp", "iki-prod"), name
        names = [e["name"] for e in pod["containers"][0]["env"]]
        assert len(names) == len(set(names)), name
        if name != "iki-provisioner":
            assert not {
                "IKI_RUNS_VM_GCP_INSTANCE_TEMPLATE",
                "IKI_RUNS_VM_GCP_KMS_KEY",
                "IKI_RUNS_VM_SERVER_URL",
            } & set(env), name
    # On EC2 the cloud is the default; without the VM tier, none of it.
    assert web_env(VMS)["IKI_RUNS_VM_OWNER"] == "iki-production"
    assert "IKI_RUNS_VM_CLOUD" not in web_env(VMS)
    assert "IKI_RUNS_VM_OWNER" not in web_env()


def test_machines_trust_the_ca_the_vm_tier_names() -> None:
    """vmTier.caSecret: mounted read-only in the provisioner alone, which hands its
    certificates to each machine it starts."""
    docs = manifests(
        {"vmTier": {**GCE["vmTier"], "caSecret": {"name": "corp-ca", "key": "ca.pem"}}}
    )
    assert provisioner_env(docs)["IKI_RUNS_VM_CA_FILE"] == "/etc/iki/vm-ca/ca.pem"
    pod = named(docs, "Deployment", "iki-provisioner")["spec"]["template"]["spec"]
    (mount,) = [m for m in pod["containers"][0]["volumeMounts"] if m["name"] == "vm-ca"]
    assert (mount["mountPath"], mount["readOnly"]) == ("/etc/iki/vm-ca", True)
    (volume,) = [v for v in pod["volumes"] if v["name"] == "vm-ca"]
    assert volume["secret"] == {
        "secretName": "corp-ca",
        "items": [{"key": "ca.pem", "path": "ca.pem"}],
    }
    web = named(docs, "Deployment", "iki-web")["spec"]["template"]["spec"]
    assert not [v for v in web["volumes"] if v["name"] == "vm-ca"]
    # Without one, machines trust their own roots alone.
    plain = manifests(GCE)
    assert "IKI_RUNS_VM_CA_FILE" not in provisioner_env(plain)
    pod = named(plain, "Deployment", "iki-provisioner")["spec"]["template"]["spec"]
    assert not [v for v in pod["volumes"] if v["name"] == "vm-ca"]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"owner": "Iki-Production"}, "3 to 20 lowercase letters"),
        ({"owner": "iki-production-in-europe-west"}, "3 to 20 lowercase letters"),
        (
            {"gcp": {"instanceTemplate": GCE["vmTier"]["gcp"]["instanceTemplate"]}},
            "vmTier.gcp.project is required",
        ),
        ({"gcp": {"project": "iki-prod"}}, "vmTier.gcp.instanceTemplate is required"),
        ({"cloud": "azure"}, "/vmTier/cloud"),
        (
            {"gcp": {"project": "iki-prod", "instanceTemplate": "iki-vm"}},
            "/vmTier/gcp/instanceTemplate",
        ),
    ],
)
def test_compute_engine_machines_need_a_project_a_template_and_a_lowercase_owner(
    change: dict[str, Any], message: str
) -> None:
    error = render({"vmTier": {**GCE["vmTier"], **change}}, ok=False)
    assert isinstance(error, str)
    assert message in error


def test_both_tiers_share_one_provisioner() -> None:
    docs = manifests({**CONTAINERS, **VMS})
    assert (
        len([d for d in by_kind(docs, "Deployment") if "provisioner" in d["metadata"]["name"]]) == 1
    )
    env = provisioner_env(docs)
    assert env["IKI_RUNS_NAMESPACE"] == "iki-runs"
    assert env["IKI_RUNS_VM_OWNER"] == "iki-production"


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"vmTier": {"enabled": True}}, "vmTier.owner is required"),
        (
            {"vmTier": {"enabled": True, "owner": "iki", "serverUrl": "http://iki.internal"}},
            "/vmTier/serverUrl",
        ),
        (
            {"publicUrl": "http://iki.example.com", "vmTier": {"enabled": True, "owner": "iki"}},
            "must be an https:// address",
        ),
    ],
)
def test_machines_reach_the_server_over_https_and_are_tagged_as_this_deployments(
    values: dict[str, Any], message: str
) -> None:
    error = render(values, ok=False)
    assert isinstance(error, str)
    assert message in error


AWS = CHART.parents[1] / "aws" / "production"


def _merged(*files: Path) -> dict[str, Any]:
    """Values files merged as Helm merges them."""
    return merged(*(yaml.safe_load(file.read_text()) for file in files))


def _secrets_read(node: Any) -> Iterator[str]:
    """Every Secret a manifest names: in env (secretKeyRef, secretRef) and volumes."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("secretKeyRef", "secretRef") and isinstance(value, dict):
                yield value["name"]
            elif key == "secret" and isinstance(value, dict) and "secretName" in value:
                yield value["secretName"]
            else:
                yield from _secrets_read(value)
    elif isinstance(node, list):
        for item in node:
            yield from _secrets_read(item)


def test_the_aws_production_values_render_a_locked_down_deployment() -> None:
    # The values deploy/aws/production writes for its test inputs (its own test checks
    # they're what it writes), with values-production.yaml, as the runbook installs them.
    values = _merged(AWS / "values-production.yaml", AWS / "tests" / "helm-values.yaml")
    docs = manifests(values)

    ingress = named(docs, "Ingress", "iki-web")
    assert ingress["spec"]["ingressClassName"] == "alb"
    (rule,) = ingress["spec"]["rules"]
    assert rule["host"] == "iki.example.com"
    assert rule["http"]["paths"][0]["backend"]["service"] == {
        "name": "iki-web",
        "port": {"name": "http"},
    }
    notes = ingress["metadata"]["annotations"]
    assert notes["alb.ingress.kubernetes.io/listen-ports"] == '[{"HTTPS":443}]'
    assert notes["alb.ingress.kubernetes.io/target-type"] == "ip"

    # The ALB reaches web pods from its subnets, and nothing else does.
    web_in = named(docs, "NetworkPolicy", "iki-web-ingress")["spec"]["ingress"]
    peers = [p["ipBlock"]["cidr"] for rule in web_in for p in rule.get("from", [])]
    assert peers == ["10.40.192.0/22", "10.40.196.0/22", "10.40.200.0/22"]

    # Pods with an AWS role reach the node's Pod Identity agent, and only they do.
    identity = named(docs, "NetworkPolicy", "iki-pod-identity")["spec"]
    (components,) = [
        e["values"]
        for e in identity["podSelector"]["matchExpressions"]
        if e["key"] == "app.kubernetes.io/component"
    ]
    assert components == ["web", "tasks", "maintenance"]
    assert [p["ipBlock"]["cidr"] for p in identity["egress"][0]["to"]] == [
        "169.254.170.23/32",
        "fd00:ec2::23/128",
    ]
    assert identity["egress"][0]["ports"] == [{"protocol": "TCP", "port": 80}]

    # Every Secret a pod reads is one External Secrets makes, or the database's CA.
    made = {
        values["secretKey"]["existingSecret"],
        values["encryptionKeys"]["existingSecret"],
        values["bundles"]["signingKey"]["existingSecret"],
        values["runSecrets"]["existingSecret"],
        values["database"]["existingSecret"],
        values["database"]["migrator"]["existingSecret"],
        "iki-database-ca",
    }
    names = set(_secrets_read(docs))
    assert {"iki-secret-key", "iki-database", "iki-database-migrator"} <= names
    assert names <= made, names - made

    # The autoscaler owns web's replicas: at least 3, one per zone.
    assert "replicas" not in named(docs, "Deployment", "iki-web")["spec"]
    assert named(docs, "HorizontalPodAutoscaler", "iki-web")["spec"]["minReplicas"] == 3
    assert named(docs, "PodDisruptionBudget", "iki-web")["spec"]["minAvailable"] == 2
    assert named(docs, "Deployment", "iki-egress-proxy")["spec"]["replicas"] == 2


GCP = CHART.parents[1] / "gcp" / "production"


def test_the_gcp_production_values_render_a_locked_down_deployment() -> None:
    # The values deploy/gcp/production writes for its test inputs (its own test checks
    # they're what it writes), with values-production.yaml, as the runbook installs them.
    values = _merged(GCP / "values-production.yaml", GCP / "tests" / "helm-values.yaml")
    docs = manifests(values)

    # Served through the Gateway deploy/gcp/production's gateway output holds.
    assert not by_kind(docs, "Ingress")
    service = named(docs, "Service", "iki-web")
    assert service["spec"]["ports"][0]["port"] == 8000  # the HTTPRoute's backendRef

    # Google's front ends and health checkers reach web pods, and nothing else does. Web
    # pods keep idle connections longer than Google's load balancers (600 seconds).
    web_in = named(docs, "NetworkPolicy", "iki-web-ingress")["spec"]["ingress"]
    peers = [p["ipBlock"]["cidr"] for rule in web_in for p in rule.get("from", [])]
    assert peers == ["35.191.0.0/16", "130.211.0.0/22"]
    (web,) = named(docs, "Deployment", "iki-web")["spec"]["template"]["spec"]["containers"]
    (keep_alive,) = [e["value"] for e in web["env"] if e["name"] == "IKI_KEEP_ALIVE_SECONDS"]
    assert int(keep_alive) > 600

    # Pods with a Google identity reach GKE's metadata server, and only they do.
    identity = named(docs, "NetworkPolicy", "iki-pod-identity")["spec"]
    (components,) = [
        e["values"]
        for e in identity["podSelector"]["matchExpressions"]
        if e["key"] == "app.kubernetes.io/component"
    ]
    assert components == ["web", "tasks", "maintenance"]
    assert [p["ipBlock"]["cidr"] for p in identity["egress"][0]["to"]] == [
        "169.254.169.254/32",
        "169.254.169.252/32",
    ]
    assert identity["egress"][0]["ports"] == [
        {"protocol": "TCP", "port": 80},
        {"protocol": "TCP", "port": 988},
    ]
    for name in ("iki-web", "iki-tasks", "iki-maintenance"):
        notes = named(docs, "ServiceAccount", name)["metadata"]["annotations"]
        assert notes == values["serviceAccount"]["annotations"]

    # Cloud Storage only through Google's private range, and the database only at its own.
    storage = named(docs, "NetworkPolicy", "iki-object-storage")["spec"]["egress"]
    assert [p["ipBlock"]["cidr"] for p in storage[0]["to"]] == ["199.36.153.8/30"]

    # Every Secret a pod reads is one External Secrets makes, or the database's CA.
    made = {
        values["secretKey"]["existingSecret"],
        values["encryptionKeys"]["existingSecret"],
        values["bundles"]["signingKey"]["existingSecret"],
        values["runSecrets"]["existingSecret"],
        values["database"]["existingSecret"],
        values["database"]["migrator"]["existingSecret"],
        "iki-database-ca",
    }
    names = set(_secrets_read(docs))
    assert {"iki-secret-key", "iki-database"} <= names
    assert names <= made, names - made

    # Files through Cloud Storage's own APIs as the pods' Google account: no key (ADR 0109).
    assert "credentials" not in values["files"]
    for name, spec in pod_specs(docs):
        variables = {e["name"]: e.get("value") for e in spec["containers"][0].get("env", [])}
        if "IKI_FILES_BUCKET" in variables:
            assert variables["IKI_FILES_BACKEND"] == "gcs", name
            assert variables["IKI_FILES_ENDPOINT_URL"] == "", name
            assert "AWS_ACCESS_KEY_ID" not in variables, name

    assert named(docs, "HorizontalPodAutoscaler", "iki-web")["spec"]["minReplicas"] == 3
    assert named(docs, "PodDisruptionBudget", "iki-web")["spec"]["minAvailable"] == 2


def _narrow(docs: list[Manifest], namespace: str, labels: dict[str, str]) -> None:
    for peer, _, _ in egress_of(docs, namespace, labels):
        assert peer != "*", (namespace, labels)
        if block := json.loads(peer).get("ipBlock"):
            network = ipaddress.ip_network(block["cidr"])
            assert network.prefixlen >= (8 if network.version == 4 else 32), (labels, block)


DEV = CHART.parents[1] / "dev" / "values-dev.yaml"
#: An operator's own: the internet-facing ALB's addresses aren't known to OpenTofu.
AWS_SERVER = egress(server={"to": [{"ipBlock": {"cidr": "198.51.100.20/32"}}]})


@pytest.mark.parametrize("fixtures", [False, True], ids=["product", "fixtures"])
def test_dev_fixture_services_and_secrets_are_opt_in(fixtures: bool) -> None:
    values = _merged(DEV, DEV.parent / "fixtures" / "values.yaml") if fixtures else _merged(DEV)
    docs = manifests(values)
    secrets = set(_secrets_read(docs))
    assert ("iki-dev-ca" in secrets) == fixtures
    assert ("iki-run-secrets" in secrets) == fixtures

    proxy = named(docs, "NetworkPolicy", "iki-egress-proxy")["spec"]["egress"]
    peers = [peer for rule in proxy for peer in rule["to"]]
    for service in ("fake-lims", "fake-foldcloud"):
        peer = {"podSelector": {"matchLabels": {"app.kubernetes.io/name": service}}}
        assert (peer in peers) == fixtures

    run_namespaces = {role["metadata"]["namespace"] for role in by_kind(docs, "Role")}
    assert "iki-runs" in run_namespaces
    for namespace in ("iki-runs-shared", "iki-runs-bucket"):
        assert (namespace in run_namespaces) == fixtures


@pytest.mark.parametrize(
    "values",
    [
        pytest.param(lambda: _merged(DEV), id="dev"),
        pytest.param(
            lambda: _merged(GCP / "values-production.yaml", GCP / "tests" / "helm-values.yaml"),
            id="gcp",
        ),
        pytest.param(
            lambda: merged(
                _merged(AWS / "values-production.yaml", AWS / "tests" / "helm-values.yaml"),
                AWS_SERVER,
            ),
            id="aws",
        ),
    ],
)
def test_the_reference_values_keep_workers_and_runs_to_narrow_blocks(
    values: Any,
) -> None:
    on = {
        "backendWorkers": {
            "enabled": True,
            "image": {"repository": WORKER_IMAGE},
            "registrationToken": {"existingSecret": "iki-worker-registration"},
        },
        "containerTier": {"enabled": True, "namespace": "iki-runs"},
    }
    docs = manifests(merged(values(), on))
    _narrow(docs, "iki", worker_labels(docs))
    _narrow(docs, "iki-runs", RUN_LABELS)


def test_workers_on_a_public_alb_need_the_operator_to_say_where_it_is() -> None:
    values = _merged(AWS / "values-production.yaml", AWS / "tests" / "helm-values.yaml")
    error = render(merged(values, {"backendWorkers": WORKERS["backendWorkers"]}), ok=False)
    assert "networkPolicy.egress.server.to: workers and run pods reach" in str(error)


def _images(node: Any) -> Iterator[str]:
    """Every container image a manifest names."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "image" and isinstance(value, str):
                yield value
            else:
                yield from _images(value)
    elif isinstance(node, list):
        for item in node:
            yield from _images(item)


def _binary_authorization_allows(image: str, pattern: str) -> bool:
    """As Binary Authorization matches an allowlist pattern: ``**`` at the end matches
    anything below, ``*`` anything within the last path segment, else exactly."""
    if pattern.endswith("**"):
        return image.startswith(pattern[:-2])
    if pattern.endswith("*"):
        prefix = pattern[:-1]
        return image.startswith(prefix) and "/" not in image[len(prefix) :]
    return image == pattern


def test_binary_authorization_allows_every_image_the_gcp_deployment_runs() -> None:
    # The patterns deploy/gcp/production's policy allows for its test inputs (its own test
    # checks they're what it makes); anything else GKE refuses to start.
    patterns = json.loads((GCP / "tests" / "allowed-images.json").read_text())
    values = _merged(GCP / "values-production.yaml", GCP / "tests" / "helm-values.yaml")
    chart = yaml.safe_load((CHART / "values.yaml").read_text())
    tag = ":1.2.3@sha256:" + "0" * 64
    images = {
        *_images(manifests(values)),
        # Iki's own images as the chart's defaults name them, wherever they're used.
        chart["image"]["repository"] + tag,
        chart["containerTier"]["workerImage"]["repository"] + tag,
        # And one built with a deployment's plugins, in its own repository.
        "europe-west1-docker.pkg.dev/iki-test-project/iki-test-server/iki-server" + tag,
    }
    refused = {i for i in images if not any(_binary_authorization_allows(i, p) for p in patterns)}
    assert not refused, refused
    # The patterns aren't so broad they'd allow anything: not another registry's image, not
    # Iki's server's name under another account, not a path below Iki's own.
    owner = chart["image"]["repository"].split("/")[1]
    for stranger in (
        "docker.io/library/nginx:1",
        chart["image"]["repository"].replace(f"/{owner}/", "/someone-else/") + ":1",
        f"ghcr.io/{owner}/x/y:1",
    ):
        assert not any(_binary_authorization_allows(stranger, p) for p in patterns), stranger


def test_an_ingress_routes_public_urls_host_when_asked() -> None:
    assert not by_kind(manifests(), "Ingress")
    docs = manifests({"ingress": {"enabled": True, "className": "nginx", "tlsSecret": "iki-tls"}})
    spec = named(docs, "Ingress", "iki-web")["spec"]
    assert spec["ingressClassName"] == "nginx"
    assert spec["rules"][0]["host"] == "iki.example.com"
    assert spec["tls"] == [{"hosts": ["iki.example.com"], "secretName": "iki-tls"}]


def test_service_accounts_that_reach_object_storage_take_annotations() -> None:
    role = {"eks.amazonaws.com/role-arn": "arn:aws:iam::123456789012:role/iki-server"}
    docs = manifests({"serviceAccount": {"annotations": role}})
    for name in ("iki-web", "iki-tasks", "iki-maintenance"):
        assert named(docs, "ServiceAccount", name)["metadata"]["annotations"] == role
    # No AWS role means no route to the Pod Identity agent.
    assert "iki-pod-identity" not in [p["metadata"]["name"] for p in by_kind(docs, "NetworkPolicy")]


# --- Dashboards and alerts (ADR 0098) -------------------------------------------------------

CHART_FILES = CHART / "files"
SERVER = CHART.parents[2] / "server" / "src" / "iki_server"


def test_dashboards_and_alerts_are_off_unless_asked_for() -> None:
    docs = manifests()
    assert not by_kind(docs, "PrometheusRule")
    assert not [d for d in by_kind(docs, "ConfigMap") if d["metadata"]["name"] == "iki-dashboards"]


def test_dashboards_and_alerts_render_the_charts_files_as_they_are() -> None:
    docs = manifests(
        {
            "monitoring": {
                "dashboards": {
                    "enabled": True,
                    "annotations": {"grafana_folder": "Iki"},
                    "namespace": "monitoring",
                },
                "alerts": {"enabled": True, "labels": {"release": "kube-prometheus-stack"}},
            }
        }
    )
    dashboards = named(docs, "ConfigMap", "iki-dashboards")
    assert dashboards["metadata"]["namespace"] == "monitoring"
    assert dashboards["metadata"]["labels"]["grafana_dashboard"] == "1"
    assert dashboards["metadata"]["annotations"] == {"grafana_folder": "Iki"}
    assert dashboards["data"] == {
        "iki.json": (CHART_FILES / "dashboards" / "iki.json").read_text().rstrip("\n")
    }
    alerts = named(docs, "PrometheusRule", "iki-alerts")
    assert alerts["metadata"]["labels"]["release"] == "kube-prometheus-stack"
    # Its {{ $labels... }} are the alert's, left for Prometheus, not Helm's.
    assert alerts["spec"] == yaml.safe_load((CHART_FILES / "alerts.yaml").read_text())


def _queries() -> list[str]:
    dashboard = json.loads((CHART_FILES / "dashboards" / "iki.json").read_text())
    exprs = [t["expr"] for p in dashboard["panels"] for t in p.get("targets", [])]
    rules = yaml.safe_load((CHART_FILES / "alerts.yaml").read_text())
    return exprs + [r["expr"] for g in rules["groups"] for r in g["rules"]]


def _metrics_only(query: str) -> str:
    """The query without its label matchers, groupings and strings: what's left names metrics."""
    query = re.sub(r'"[^"]*"', "", query)
    query = re.sub(r"\{[^}]*\}", "", query)
    return re.sub(r"\b(by|without|on|ignoring)\s*\([^)]*\)", "", query)


def test_dashboards_and_alerts_read_only_metrics_iki_exports() -> None:
    """A misspelt metric draws an empty panel and an alert that never fires."""
    # Prometheus' name for each of Iki's metrics, as its OTLP translation gives it.
    suffixes = ("_seconds_bucket", "_seconds_count", "_seconds_total", "_seconds", "_total")
    used = {m for q in map(_metrics_only, _queries()) for m in re.findall(r"\biki_[a-z_]+", q)}
    assert used
    source = "\n".join(p.read_text() for p in SERVER.rglob("*.py"))
    for metric in sorted(used):
        base = next((metric.removesuffix(s) for s in suffixes if metric.endswith(s)), metric)
        dotted = base.replace("iki_runs_", "iki.runs.").replace("iki_compute_", "iki.compute.")
        dotted = dotted.replace("iki_tasks_", "iki.tasks.").replace("iki_", "iki.", 1)
        assert f'"{dotted}"' in source, f"{metric}: no metric named {dotted} in the server"


def test_bucket_aliases_reach_the_pods_as_old_equals_new() -> None:
    docs = manifests(
        {
            "files": {
                "bucket": "iki-files-usw2",
                "bucketAliases": {"iki-files-use1": "iki-files-usw2", "lab-data": "lab-data-dr"},
            }
        }
    )
    env = {
        e["name"]: e.get("value")
        for e in named(docs, "Deployment", "iki-web")["spec"]["template"]["spec"]["containers"][0][
            "env"
        ]
    }
    assert env["IKI_FILES_BUCKET_ALIASES"] == "iki-files-use1=iki-files-usw2,lab-data=lab-data-dr"
    message = render(
        {"files": {"bucket": "iki-files", "bucketAliases": {"S3://old": "new-files"}}}, ok=False
    )
    assert "invalid propertyName 'S3://old'" in str(message)


# --- The export sandbox (ADR 0230) ---------------------------------------------------------

SANDBOX: dict[str, Any] = {"sandbox": {"enabled": True}}
SANDBOX_URL = "tcp://iki-sandbox:8090"
#: A cloud identity on every annotated service account: IRSA's and Workload Identity's.
CLOUD_IDENTITY = {
    "eks.amazonaws.com/role-arn": "arn:aws:iam::123456789012:role/iki-server",
    "iam.gke.io/gcp-service-account": "iki-server@acme.iam.gserviceaccount.com",
}


def pod_labels(docs: list[Manifest], deployment: str) -> dict[str, str]:
    labels: dict[str, str] = named(docs, "Deployment", deployment)["spec"]["template"]["metadata"][
        "labels"
    ]
    return labels


def sandbox_spec(docs: list[Manifest]) -> Manifest:
    spec: Manifest = named(docs, "Deployment", "iki-sandbox")["spec"]["template"]["spec"]
    return spec


def env_names(docs: list[Manifest], kind: str, name: str) -> dict[str, Any]:
    (spec,) = [spec for where, spec in pod_specs(docs) if where == f"{kind}/{name}"]
    return {e["name"]: e.get("value") for c in spec["containers"] for e in c.get("env", [])}


def ingress_of(docs: list[Manifest], namespace: str, labels: dict[str, str]) -> Reach:
    """Every (peer, protocol, port) a pod accepts from, as egress_of reads egress."""
    reach: Reach = set()
    limited = False
    for policy in by_kind(docs, "NetworkPolicy"):
        spec = policy["spec"]
        types = spec.get("policyTypes") or ["Ingress"]
        if policy["metadata"].get("namespace", "iki") != namespace or "Ingress" not in types:
            continue
        if not selects(spec["podSelector"], labels):
            continue
        limited = True
        for rule in spec.get("ingress", []):
            peers = [json.dumps(p, sort_keys=True) for p in rule.get("from", [])] or ["*"]
            ports = [(p.get("protocol", "TCP"), p.get("port", "*")) for p in rule.get("ports", [])]
            reach |= {(peer, *port) for peer in peers for port in ports or [("*", "*")]}
    assert limited, f"no policy limits the ingress of {labels} in {namespace}"
    return reach


def test_the_sandbox_is_off_unless_turned_on() -> None:
    docs = manifests()
    assert [d["metadata"]["name"] for d in docs if "sandbox" in d["metadata"]["name"]] == []
    for name in ("iki-web", "iki-tasks"):
        assert "IKI_EXPORT_SANDBOX" not in env_names(docs, "Deployment", name)


def test_the_sandbox_renders_its_deployment_service_and_account() -> None:
    docs = manifests(SANDBOX)
    found = sorted(
        f"{d['kind']}/{d['metadata']['name']}" for d in docs if "sandbox" in d["metadata"]["name"]
    )
    assert found == [
        "Deployment/iki-sandbox",
        "NetworkPolicy/iki-sandbox",
        "NetworkPolicy/iki-sandbox-clients",
        "PodDisruptionBudget/iki-sandbox",
        "Service/iki-sandbox",
        "ServiceAccount/iki-sandbox",
    ]
    deployment = named(docs, "Deployment", "iki-sandbox")
    assert deployment["spec"]["replicas"] == 2
    assert deployment["spec"]["strategy"]["rollingUpdate"]["maxUnavailable"] == 0
    assert pod_labels(docs, "iki-sandbox")["app.kubernetes.io/component"] == "sandbox"
    service = named(docs, "Service", "iki-sandbox")["spec"]
    assert service["type"] == "ClusterIP"
    assert service["selector"]["app.kubernetes.io/component"] == "sandbox"
    assert service["ports"] == [
        {"name": "export", "port": 8090, "targetPort": "export", "protocol": "TCP"}
    ]
    assert named(docs, "PodDisruptionBudget", "iki-sandbox")["spec"]["minAvailable"] == 1
    off = manifests(merged(SANDBOX, {"sandbox": {"podDisruptionBudget": {"enabled": False}}}))
    assert not [
        d for d in by_kind(off, "PodDisruptionBudget") if d["metadata"]["name"] == "iki-sandbox"
    ]


def test_the_sandbox_runs_the_deployments_image_as_the_writer_launcher() -> None:
    docs = manifests(merged(SANDBOX, {"image": {"digest": "sha256:" + "a" * 64}}))
    (web,) = named(docs, "Deployment", "iki-web")["spec"]["template"]["spec"]["containers"]
    (sandbox,) = sandbox_spec(docs)["containers"]
    assert sandbox["image"] == web["image"]
    assert sandbox["image"].endswith("@sha256:" + "a" * 64)
    assert sandbox["command"] == [
        "/opt/iki/venv/bin/python",
        "-I",
        "-m",
        "iki_spec.writer_sandbox",
    ]
    assert sandbox["args"] == ["--port", "8090", "--health-port", "8091", "--concurrency", "4"]
    assert {p["name"]: p["containerPort"] for p in sandbox["ports"]} == {
        "export": 8090,
        "health": 8091,
    }
    for probe, path in (
        ("startupProbe", "/healthz/live"),
        ("livenessProbe", "/healthz/live"),
        ("readinessProbe", "/healthz/ready"),
    ):
        assert sandbox[probe]["httpGet"] == {"path": path, "port": "health"}, probe
    assert sandbox["resources"]["requests"] == {"cpu": "100m", "memory": "256Mi"}
    assert sandbox["resources"]["limits"] == {"memory": "768Mi"}
    eight = manifests(merged(SANDBOX, {"sandbox": {"concurrency": 8}}))
    assert sandbox_spec(eight)["containers"][0]["args"][-1] == "8"


@pytest.mark.parametrize("concurrency", [0, 33, "4"])
def test_the_sandboxs_concurrency_is_checked_by_the_schema(concurrency: Any) -> None:
    assert "/sandbox/concurrency" in str(
        render(merged(SANDBOX, {"sandbox": {"concurrency": concurrency}}), ok=False)
    )


def test_the_sandbox_holds_no_secret_token_environment_or_cloud_identity() -> None:
    # Everything that puts a Secret, a token or an identity into the other pods, on.
    values = merged(
        SANDBOX,
        WORKERS,
        TO_STORAGE,
        {
            "serviceAccount": {"annotations": CLOUD_IDENTITY},
            "database": {
                **REQUIRED["database"],
                "caSecret": {"name": "iki-db-ca", "key": "ca.crt"},
            },
            "webhooks": {"caSecret": {"name": "iki-webhook-ca", "key": "ca.crt"}},
            "runSecrets": {"existingSecret": "iki-run-secrets"},
            "email": {"existingSecret": "iki-email", "from": "Iki <iki@example.com>"},
            "files": {"bucket": "iki-files", "credentials": {"existingSecret": "iki-s3"}},
            "bundles": {"signingKey": {"existingSecret": "iki-bundle-key"}},
        },
    )
    docs = manifests(values)
    spec = sandbox_spec(docs)
    assert spec["serviceAccountName"] == "iki-sandbox"
    assert spec["automountServiceAccountToken"] is False
    assert spec["enableServiceLinks"] is False
    assert "volumes" not in spec
    assert "initContainers" not in spec
    (container,) = spec["containers"]
    for key in ("env", "envFrom", "volumeMounts"):
        assert key not in container, key
    # Nothing in the pod names a Secret, in any form.
    assert list(_secrets_read(spec)) == []
    assert "secret" not in json.dumps(spec).lower()
    # Its account has no token and no identity, while the server's accounts carry one.
    account = named(docs, "ServiceAccount", "iki-sandbox")
    assert account["automountServiceAccountToken"] is False
    assert "annotations" not in account["metadata"]
    assert named(docs, "ServiceAccount", "iki-web")["metadata"]["annotations"] == CLOUD_IDENTITY
    # Nor any Kubernetes permissions.
    for binding in [*by_kind(docs, "RoleBinding"), *by_kind(docs, "ClusterRoleBinding")]:
        names = {s.get("name") for s in binding.get("subjects", [])}
        assert "iki-sandbox" not in names, binding["metadata"]["name"]


def test_the_sandbox_meets_pod_security_restricted() -> None:
    docs = manifests(SANDBOX)
    spec = sandbox_spec(docs)
    psc = spec["securityContext"]
    assert psc["runAsNonRoot"] is True
    assert psc["runAsUser"] >= 10000
    assert psc["seccompProfile"] == {"type": "RuntimeDefault"}
    (container,) = spec["containers"]
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "privileged": False,
        "capabilities": {"drop": ["ALL"]},
    }


def test_web_and_task_runners_and_only_they_know_where_the_sandbox_is() -> None:
    docs = manifests(merged(SANDBOX, WORKERS))
    for name in ("iki-web", "iki-tasks"):
        assert env_names(docs, "Deployment", name)["IKI_EXPORT_SANDBOX"] == SANDBOX_URL, name
    for where, spec in pod_specs(docs):
        if where not in ("Deployment/iki-web", "Deployment/iki-tasks"):
            names = {e["name"] for c in spec["containers"] for e in c.get("env", [])}
            assert "IKI_EXPORT_SANDBOX" not in names, where


def test_the_sandbox_reaches_nothing_and_only_web_and_tasks_reach_it() -> None:
    values = merged(
        SANDBOX,
        WORKERS,
        CONTAINERS,
        {
            "otel": {"endpoint": "http://collector:4318"},
            "email": {"existingSecret": "iki-email", "from": "Iki <iki@example.com>"},
            "files": {"bucket": "iki-files"},
            "egressProxy": {"enabled": True},
            "networkPolicy": {
                "ingressFrom": [{"namespaceSelector": {"matchLabels": {"x": "y"}}}],
                "egress": {
                    "database": {"to": [{"ipBlock": {"cidr": "10.0.0.0/24"}}]},
                    "otel": {"to": [{"podSelector": {"matchLabels": {"app": "collector"}}}]},
                    "objectStorage": {"to": STORAGE_PEERS},
                    "podIdentity": {"to": [{"ipBlock": {"cidr": "169.254.170.23/32"}}]},
                },
            },
        },
    )
    docs = manifests(values)
    sandbox = pod_labels(docs, "iki-sandbox")
    # No egress at all: not DNS, the database, object storage or a cloud's identity agent.
    assert egress_of(docs, "iki", sandbox) == set()
    for policy in by_kind(docs, "NetworkPolicy"):
        spec = policy["spec"]
        here = policy["metadata"].get("namespace", "iki") == "iki"
        if here and selects(spec["podSelector"], sandbox) and spec.get("egress"):
            raise AssertionError(f"{policy['metadata']['name']} gives the sandbox egress")
    # Exports only from web pods and task runners, and only to the export port.
    web, tasks = pod_labels(docs, "iki-web"), pod_labels(docs, "iki-tasks")
    selector = {
        "app.kubernetes.io/name": "iki",
        "app.kubernetes.io/instance": "iki",
    }
    clients = [
        {"podSelector": {"matchLabels": {**selector, "app.kubernetes.io/component": c}}}
        for c in ("web", "tasks")
    ]
    assert ingress_of(docs, "iki", sandbox) == reach(clients, ("TCP", 8090))
    for other in ("iki-worker", "iki-egress-proxy", "iki-provisioner"):
        labels = pod_labels(docs, other)
        for peer in clients:
            assert not selects(peer["podSelector"], labels), other
    # Web pods and task runners reach it; nothing else in Iki does.
    to_sandbox = reach(
        [{"podSelector": {"matchLabels": {**selector, "app.kubernetes.io/component": "sandbox"}}}],
        ("TCP", 8090),
    )
    for labels in (web, tasks):
        assert to_sandbox <= egress_of(docs, "iki", labels)
    maintenance = named(docs, "CronJob", "iki-maintenance")["spec"]["jobTemplate"]["spec"][
        "template"
    ]["metadata"]["labels"]
    for labels in (
        pod_labels(docs, "iki-worker"),
        pod_labels(docs, "iki-egress-proxy"),
        pod_labels(docs, "iki-provisioner"),
        maintenance,
    ):
        assert not to_sandbox & egress_of(docs, "iki", labels), labels
    assert not to_sandbox & egress_of(docs, "iki-runs", RUN_LABELS)


@pytest.mark.parametrize(
    "values",
    [
        pytest.param(lambda: _merged(DEV, DEV.parent / "fixtures" / "values.yaml"), id="dev"),
        pytest.param(
            lambda: _merged(AWS / "values-production.yaml", AWS / "tests" / "helm-values.yaml"),
            id="aws",
        ),
        pytest.param(
            lambda: _merged(GCP / "values-production.yaml", GCP / "tests" / "helm-values.yaml"),
            id="gcp",
        ),
    ],
)
def test_the_reference_values_leave_the_sandbox_with_no_way_out(values: Any) -> None:
    docs = manifests(merged(values(), SANDBOX))
    sandbox = pod_labels(docs, "iki-sandbox")
    assert egress_of(docs, "iki", sandbox) == set()
    assert "annotations" not in named(docs, "ServiceAccount", "iki-sandbox")["metadata"]
    assert list(_secrets_read(sandbox_spec(docs))) == []
    if identity := [
        p for p in by_kind(docs, "NetworkPolicy") if p["metadata"]["name"] == "iki-pod-identity"
    ]:
        assert not selects(identity[0]["spec"]["podSelector"], sandbox)


def test_the_dev_fixtures_run_the_sandbox_and_product_dev_doesnt() -> None:
    assert not [d for d in manifests(_merged(DEV)) if d["metadata"]["name"] == "iki-sandbox"]
    docs = manifests(_merged(DEV, DEV.parent / "fixtures" / "values.yaml"))
    named(docs, "Deployment", "iki-sandbox")
    assert env_names(docs, "Deployment", "iki-web")["IKI_EXPORT_SANDBOX"] == SANDBOX_URL
