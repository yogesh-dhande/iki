"""What the provisioner asks Kubernetes for (ADR 0061), and how.

:class:`Cluster` is the little of the Kubernetes API the provisioner uses. :class:`InCluster`
talks to the API server the way any pod does, with its service account's token (which the
chart mounts into the provisioner's pod alone) and the cluster's CA.

:func:`job_manifest` is a run pod's Job. The pod runs the environment's image, pinned by
its digest, with ``iki-worker`` as its command: an init container from Iki's own signed
worker image copies the binary into a volume the image's container mounts read-only. The
worker registers with the launch's token, from a Secret the Job owns, runs one job and
exits, so the Job completes and Kubernetes cleans it up. On a persistent machine it runs
one job after another, cleaning up between them, until the provisioner stops it (ADR
0062). The pod meets PodSecurity ``restricted``: a non-root user, no privilege escalation
or capabilities, a read-only root filesystem, the runtime's default seccomp profile, and no
Kubernetes API token.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import requests

from iki_server.runs.machines import BIN, RUN_LABELS, SCRATCH, UID, worker_settings
from iki_spec.compute import Template

__all__ = [
    "LABEL",
    "RUN_LABELS",
    "Cluster",
    "InCluster",
    "KubernetesError",
    "RunPods",
    "job_manifest",
    "quantity",
]

#: Every object the provisioner makes carries this label, with its launch's ID.
LABEL = "iki.ikisci.org/launch"
MANAGED = {"app.kubernetes.io/managed-by": "iki-provisioner", "app.kubernetes.io/component": "run"}
#: Seconds a finished Job is kept, for whoever is looking into it, before Kubernetes
#: deletes it with its pod and Secret.
KEEP_FINISHED = 600


class KubernetesError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Kubernetes said {status}: {message}")
        self.status = status
        self.message = message


class Cluster(Protocol):
    def create_job(self, namespace: str, job: dict[str, Any], secret: dict[str, Any]) -> None:
        """Creates the Job and its Secret, owned by the Job so they go together."""

    def jobs(self, namespace: str) -> list[dict[str, Any]]:
        """The provisioner's Jobs in `namespace`."""

    def pods(self, namespace: str) -> list[dict[str, Any]]:
        """The provisioner's pods in `namespace`."""

    def delete_job(self, namespace: str, name: str) -> None:
        """Deletes a Job, with its pods and Secret; nothing if it's gone already."""

    def label_pod(self, namespace: str, name: str, labels: dict[str, str | None]) -> None:
        """Sets the pod's labels, removing those given as None."""


@dataclass(frozen=True)
class RunPods:
    """What every run pod gets, from the deployment's settings."""

    server_url: str
    worker_image: str
    service_account: str
    allow_http: bool = False
    image_pull_secrets: tuple[str, ...] = ()
    labels: dict[str, str] = field(default_factory=dict)


def quantity(value: float | None, unit: str = "") -> str | None:
    if value is None:
        return None
    if unit == "cpu":
        return f"{round(value * 1000)}m"
    return str(int(value))


def _resources(template: Template) -> dict[str, Any]:
    have = template.capacity
    amounts = {"cpu": quantity(have.cpu, "cpu"), "memory": quantity(have.memory)}
    if have.scratch_disk:
        amounts["ephemeral-storage"] = quantity(have.scratch_disk)
    if have.gpu_count:
        amounts["nvidia.com/gpu"] = str(have.gpu_count)
    # Requests equal to limits: the pod gets exactly what the template promises, and
    # Kubernetes gives it Guaranteed quality of service.
    kept = {k: v for k, v in amounts.items() if v is not None}
    return {"requests": kept, "limits": kept}


def _worker_env(
    template: Template, environment: str, image: str, pods: RunPods, *, once: bool
) -> list[dict[str, Any]]:
    values = worker_settings(
        template,
        environment,
        image,
        server_url=pods.server_url,
        allow_http=pods.allow_http,
        once=once,
    )
    env: list[dict[str, Any]] = [{"name": name, "value": value} for name, value in values.items()]
    env.append(
        {"name": "IKI_WORKER_NAME", "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}}
    )
    return env


def _pod_security() -> dict[str, Any]:
    return {
        "runAsNonRoot": True,
        "runAsUser": UID,
        "runAsGroup": UID,
        "fsGroup": UID,
        "seccompProfile": {"type": "RuntimeDefault"},
    }


def _container_security() -> dict[str, Any]:
    return {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }


def job_manifest(
    *,
    name: str,
    launch_id: str,
    template: Template,
    environment: str,
    image: str,
    pods: RunPods,
    once: bool = True,
) -> tuple[dict[str, Any], str]:
    """The Job for a launch, and the name of the Secret holding its token. Its worker runs
    one job, or with `once` false, one after another (ADR 0062)."""
    labels = {**pods.labels, **MANAGED, LABEL: launch_id}
    secret = name
    kube = template.kubernetes
    have = template.capacity
    scratch: dict[str, Any] = {}
    if have.scratch_disk:
        scratch["sizeLimit"] = quantity(have.scratch_disk)
    pod: dict[str, Any] = {
        "restartPolicy": "Never",
        "serviceAccountName": pods.service_account,
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        "securityContext": _pod_security(),
        # Its worker stops the command, saves a checkpointable run's progress (ADR 0069),
        # and reports it interrupted, so it's retried and resumes.
        "terminationGracePeriodSeconds": 300,
        "initContainers": [
            {
                "name": "iki-worker",
                "image": pods.worker_image,
                "imagePullPolicy": "IfNotPresent",
                "command": ["iki-worker", "install", BIN],
                "securityContext": _container_security(),
                "resources": {
                    "requests": {"cpu": "50m", "memory": "32Mi"},
                    "limits": {"cpu": "200m", "memory": "64Mi"},
                },
                "volumeMounts": [{"name": "iki-bin", "mountPath": BIN}],
            }
        ],
        "containers": [
            {
                "name": "run",
                "image": image,
                "imagePullPolicy": "IfNotPresent",
                "command": [f"{BIN}/iki-worker", "start", *(["--once"] if once else [])],
                "env": [
                    *_worker_env(template, environment, image, pods, once=once),
                    {
                        "name": "IKI_REGISTRATION_TOKEN",
                        "valueFrom": {"secretKeyRef": {"name": secret, "key": "token"}},
                    },
                ],
                "resources": _resources(template),
                "securityContext": _container_security(),
                "volumeMounts": [
                    {"name": "iki-bin", "mountPath": BIN, "readOnly": True},
                    {"name": "scratch", "mountPath": SCRATCH},
                ],
            }
        ],
        "volumes": [
            {"name": "iki-bin", "emptyDir": {"sizeLimit": "200Mi"}},
            {"name": "scratch", "emptyDir": scratch},
        ],
    }
    if kube.get("node_selector"):
        pod["nodeSelector"] = dict(kube["node_selector"])
    if kube.get("tolerations"):
        pod["tolerations"] = [dict(t) for t in kube["tolerations"]]
    if kube.get("runtime_class"):
        pod["runtimeClassName"] = kube["runtime_class"]
    if pods.image_pull_secrets:
        pod["imagePullSecrets"] = [{"name": s} for s in pods.image_pull_secrets]
    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "labels": labels,
            "annotations": {"iki.ikisci.org/template": template.name},
        },
        "spec": {
            # One pod, never retried by Kubernetes: Iki retries the run, under its policy.
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": KEEP_FINISHED,
            "template": {"metadata": {"labels": labels}, "spec": pod},
        },
    }
    return job, secret


def secret_manifest(
    name: str, launch_id: str, token: str, labels: dict[str, str]
) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "Opaque",
        "metadata": {"name": name, "labels": {**labels, **MANAGED, LABEL: launch_id}},
        "immutable": True,
        "data": {"token": base64.b64encode(token.encode()).decode()},
    }


# --- Talking to the API server ------------------------------------------------------------

ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")


class InCluster:
    """The API server, as a pod sees it: its address from the environment Kubernetes
    sets, with the pod's service account's token and the cluster's CA. The token is read
    for each request, since Kubernetes rotates it."""

    def __init__(self, host: str, port: str, account: Path = ACCOUNT) -> None:
        self.base = f"https://{host}:{port}"
        self.account = account
        self.session = requests.Session()
        self.session.verify = str(account / "ca.crt")

    def _call(self, method: str, path: str, body: Any = None, **params: str) -> Any:  # noqa: ANN401 - JSON
        token = (self.account / "token").read_text().strip()
        response = self.session.request(
            method,
            self.base + path,
            json=body,
            params=params or None,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=30,
        )
        if response.status_code >= 400:
            try:
                message = response.json().get("message", response.text)
            except ValueError:
                message = response.text
            raise KubernetesError(response.status_code, str(message)[:500])
        return response.json() if response.content else None

    def create_job(self, namespace: str, job: dict[str, Any], secret: dict[str, Any]) -> None:
        # The Job first, suspended, so its Secret can be made with the Job as its owner:
        # the provisioner may only create Secrets, never read, change or delete one, and
        # Kubernetes deletes this one with its Job.
        suspended = {**job, "spec": {**job["spec"], "suspend": True}}
        jobs = f"/apis/batch/v1/namespaces/{namespace}/jobs"
        made = self._call("POST", jobs, suspended)
        owner = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "name": made["metadata"]["name"],
            "uid": made["metadata"]["uid"],
            "blockOwnerDeletion": False,
        }
        owned = {**secret, "metadata": {**secret["metadata"], "ownerReferences": [owner]}}
        try:
            self._call("POST", f"/api/v1/namespaces/{namespace}/secrets", owned)
        except KubernetesError:
            self.delete_job(namespace, made["metadata"]["name"])
            raise
        self._patch(f"{jobs}/{made['metadata']['name']}", {"spec": {"suspend": False}})

    def _patch(self, path: str, body: dict[str, Any]) -> None:
        token = (self.account / "token").read_text().strip()
        response = self.session.patch(
            self.base + path,
            json=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/merge-patch+json",
            },
            timeout=30,
        )
        if response.status_code >= 400:
            raise KubernetesError(response.status_code, response.text[:500])

    def _delete(self, path: str) -> None:
        try:
            self._call("DELETE", path, {"propagationPolicy": "Background"})
        except KubernetesError as error:
            if error.status != 404:
                raise

    def _list(self, path: str) -> list[dict[str, Any]]:
        found = self._call("GET", path, labelSelector=LABEL)
        return list(found.get("items") or [])

    def jobs(self, namespace: str) -> list[dict[str, Any]]:
        return self._list(f"/apis/batch/v1/namespaces/{namespace}/jobs")

    def pods(self, namespace: str) -> list[dict[str, Any]]:
        return self._list(f"/api/v1/namespaces/{namespace}/pods")

    def delete_job(self, namespace: str, name: str) -> None:
        self._delete(f"/apis/batch/v1/namespaces/{namespace}/jobs/{name}")

    def label_pod(self, namespace: str, name: str, labels: dict[str, str | None]) -> None:
        self._patch(f"/api/v1/namespaces/{namespace}/pods/{name}", {"metadata": {"labels": labels}})
