"""The Google REST and credential transports, with in-memory test credentials."""

from typing import cast

import google.auth
import pytest
import requests
from google.auth import credentials
from google.auth.transport.requests import Request

from iki_server.files import gcs
from iki_server.runs.gce import GoogleGce

GOOGLE_SESSIONS = (
    "storage JSON",
    "storage XML and IAM",
    "storage token refresh",
    "signing token refresh",
    "storage credential discovery",
    "compute",
    "compute token refresh",
    "compute credential discovery",
    "zone operations",
    "global operations",
)


def google_session(kind: str, monkeypatch: pytest.MonkeyPatch) -> requests.Session:
    discovered: list[requests.Session] = []
    refreshed: list[requests.Session] = []

    class Workload(credentials.Credentials):
        def refresh(self, request: Request) -> None:
            refreshed.append(request.session)
            self.token = "fixture"
            self.service_account_email = "fixture@example.org"

    def default(*, request: Request, **kwargs: object) -> tuple[Workload, None]:
        discovered.append(request.session)
        found = Workload()  # type: ignore[no-untyped-call]
        if kind != "signing token refresh":
            found.token = "fixture"
        return found, None

    monkeypatch.setattr(google.auth, "default", default)
    if kind.startswith(("storage", "signing")):
        gcs.clients.cache_clear()
        try:
            clients = gcs.clients()
            if kind == "storage JSON":
                return cast(requests.Session, clients.client._http)
            if kind == "storage XML and IAM":
                return clients.session
            if kind == "signing token refresh":
                clients.signer()
                return refreshed[0]
            if kind == "storage credential discovery":
                return discovered[0]
            return cast(requests.Session, clients.session._auth_request.session)
        finally:
            gcs.clients.cache_clear()
    client = GoogleGce()._client(
        "ImagesClient" if kind == "global operations" else "InstancesClient"
    )
    if kind == "compute credential discovery":
        return discovered[0]
    transport = client.transport
    if kind == "zone operations":
        transport = transport._zone_operations_client.transport
    elif kind == "global operations":
        transport = transport._global_operations_client.transport
    if kind == "compute token refresh":
        return cast(requests.Session, transport._session._auth_request.session)
    return cast(requests.Session, transport._session)
