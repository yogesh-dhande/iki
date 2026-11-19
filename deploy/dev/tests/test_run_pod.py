"""The dev-cluster check must see a short run's labels even after its pod has gone."""

import json
from urllib.parse import parse_qs, urlsplit

import pytest
from deploy.dev import sso


@pytest.mark.parametrize("labelled", [True, False])
def test_run_pod_events_survive_cleanup(monkeypatch: pytest.MonkeyPatch, labelled: bool) -> None:
    worker = "iki-run-short"
    versions: list[str] = []

    def event(kind: str, version: str, labels: dict[str, str]) -> str:
        return json.dumps(
            {
                "type": kind,
                "object": {
                    "metadata": {"name": worker, "resourceVersion": version, "labels": labels},
                    "spec": {"containers": [{"image": "example"}]},
                },
            }
        )

    def kubectl(*args: str) -> str:
        url = urlsplit(args[2])
        assert url.path == "/api/v1/namespaces/iki-runs/pods"
        query = parse_qs(url.query)
        assert query["watch"] == ["true"]
        assert query["fieldSelector"] == [f"metadata.name={worker}"]
        versions.append(query["resourceVersion"][0])
        labels = {"iki.ikisci.org/run": "r", "iki.ikisci.org/project": "other"}
        if len(versions) == 1:
            return event("ADDED", "105", labels)
        assert len(versions) == 2
        if labelled:
            labels["iki.ikisci.org/project"] = "p"
        return "\n".join([event("MODIFIED", "109", labels), event("DELETED", "111", labels)])

    monkeypatch.setattr(sso, "kubectl", kubectl)
    if labelled:
        pod = sso._labelled_run_pod("100", worker, "r", "p")
        assert pod["metadata"]["resourceVersion"] == "109"
        assert pod["spec"]["containers"][0]["image"] == "example"
    else:
        with pytest.raises(SystemExit):
            sso._labelled_run_pod("100", worker, "r", "p")
    assert versions == ["100", "105"]
