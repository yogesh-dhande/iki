"""A fake Iki for the SDK's tests: answers by method and path, and records requests."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2
import pytest

from iki import auth
from iki.client import Client

URL = "https://iki.example.com"


class MemoryKeychain:
    """A system keychain, in memory."""

    priority = 1

    def __init__(self) -> None:
        self.saved: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, user: str) -> str | None:
        return self.saved.get((service, user))

    def set_password(self, service: str, user: str, password: str) -> None:
        self.saved[(service, user)] = password

    def delete_password(self, service: str, user: str) -> None:
        self.saved.pop((service, user), None)


Answer = httpx2.Response | Callable[[httpx2.Request], httpx2.Response]


def problem(status: int, detail: str, code: str = "", **extra: Any) -> httpx2.Response:
    body = {"status": status, "detail": detail, "code": code, **extra}
    return httpx2.Response(status, json=body, headers={"Content-Type": "application/problem+json"})


@dataclass
class FakeIki:
    answers: dict[tuple[str, str], list[Answer]] = field(default_factory=dict)
    requests: list[httpx2.Request] = field(default_factory=list)
    slept: list[float] = field(default_factory=list)

    def on(self, method: str, path: str, *answers: Answer | Any) -> None:
        """Answers `method path` with each of `answers` in turn, the last for ever after.
        Anything that isn't a response or a function is sent as JSON."""
        self.answers[(method, path)] = [
            a if isinstance(a, httpx2.Response) or callable(a) else httpx2.Response(200, json=a)
            for a in answers
        ]

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        key = (request.method, request.url.path)
        queue = self.answers.get(key)
        if not queue:
            return problem(404, f"Nothing at {key}", "not_found")
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        return answer(request) if callable(answer) else answer

    def sent(self, method: str, path: str) -> list[httpx2.Request]:
        return [r for r in self.requests if (r.method, r.url.path) == (method, path)]

    def body(self, method: str, path: str) -> Any:
        return json.loads(self.sent(method, path)[-1].content)

    def client(self, store: auth.Store, **kw: Any) -> Client:
        return Client(
            URL,
            transport=httpx2.MockTransport(self.handle),
            store=store,
            sleep=self.slept.append,
            **kw,
        )


def store_with(directory: Path, **credentials: Any) -> auth.Store:
    """A store holding a sign-in to the fake Iki."""
    store = auth.Store(directory)
    store.save(
        auth.Credentials(
            url=URL,
            access_token=credentials.get("access_token", "access-1"),
            refresh_token=credentials.get("refresh_token", "refresh-1"),
            expires_at=credentials.get("expires_at", 4102444800.0),  # 2100
            scope="read write run",
        )
    )
    return store


def entity(hrid: str = "PRT-000001", **values: Any) -> dict[str, Any]:
    return {
        "id": f"00000000-0000-7000-8000-{int(hrid[-6:]):012d}",
        "hrid": hrid,
        "type": "protein.Protein",
        "revision": 1,
        "archived": False,
        "archived_at": None,
        "created_at": "2026-11-01T09:00:00Z",
        "updated_at": "2026-11-02T10:30:00Z",
        "owner_project": {"id": "p1", "slug": "kinase-atlas", "name": "Kinase Atlas"},
        "values": values,
        "refs": {},
        "files": {},
    }


# --- Fixtures, which the test modules import ----------------------------------------------


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Tests never see the machine's settings, keychain or environment."""
    monkeypatch.setenv("IKI_CONFIG_DIR", str(tmp_path / "config"))
    for name in ("IKI_URL", "IKI_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(auth, "_keyring", lambda: None)
    yield


@pytest.fixture
def keychain(monkeypatch: pytest.MonkeyPatch) -> MemoryKeychain:
    found = MemoryKeychain()
    monkeypatch.setattr(auth, "_keyring", lambda: found)
    return found


@pytest.fixture
def config(tmp_path: Path) -> Path:
    return tmp_path / "config"
