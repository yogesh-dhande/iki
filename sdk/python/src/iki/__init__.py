"""Iki's Python SDK.

Sign in once, then work with Iki from notebooks and scripts::

    import iki

    iki.login("https://iki.example.com")  # once: you enter a code in your browser

    proteins = iki.entities.search("protein.Protein", project="kinase-atlas")
    table = proteins.to_pandas()

    run = iki.run(
        "protein.compute_properties",
        project="kinase-atlas",
        inputs={"protein": "PRT-000123"},
        reason="Weights for the summary table",
    )
    run = iki.wait(run)

``iki.entities``, ``iki.types``, ``iki.projects``, ``iki.actions``, ``iki.runs`` and
``iki.files`` use your sign-in, or ``IKI_URL`` and ``IKI_TOKEN`` if they're set. For
more than one deployment, make an ``iki.Client`` for each.

Everything you do is recorded in Iki as done by you, through the API, with this SDK.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

import httpx2

from iki import auth
from iki.client import Client, Results
from iki.errors import (
    ApiError,
    Conflict,
    FieldError,
    Forbidden,
    IkiError,
    Invalid,
    NotFound,
    NotSignedIn,
    RateLimited,
    RunFailed,
    SignInFailed,
    TimedOut,
)

if TYPE_CHECKING:
    from iki.client import Actions, Entities, Files, Projects, Runs, Types
    from iki.schema import MeOut, RunOut

    entities: Entities
    types: Types
    projects: Projects
    actions: Actions
    runs: Runs
    files: Files

__all__ = [
    "ApiError",
    "Client",
    "Conflict",
    "FieldError",
    "Forbidden",
    "IkiError",
    "Invalid",
    "NotFound",
    "NotSignedIn",
    "RateLimited",
    "Results",
    "RunFailed",
    "SignInFailed",
    "TimedOut",
    "actions",
    "client",
    "connect",
    "entities",
    "files",
    "login",
    "logout",
    "me",
    "projects",
    "run",
    "run_each",
    "runs",
    "types",
    "wait",
]

_PARTS = frozenset({"entities", "types", "projects", "actions", "runs", "files"})
_default: Client | None = None
_lock = threading.Lock()


def _say(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def login(
    url: str | None = None,
    *,
    scopes: Sequence[str] = auth.DEFAULT_SCOPES,
    open_browser: bool = True,
) -> Client:
    """Signs in to an Iki deployment, as you, and keeps the sign-in for next time.

    It shows a code to enter in Iki in your browser (which it opens, if it can),
    where you choose what to allow: reading (``read``), changing data (``write``) and
    running actions (``run``), in every project or the ones you pick.
    """
    store = auth.Store()
    chosen = url or auth.remembered_url(store.directory)
    if not chosen:
        raise IkiError("Which Iki? iki.login('https://iki.example.com')")
    target = auth.normalize_url(chosen)
    with httpx2.Client(timeout=30.0) as http:
        credentials = auth.device_login(
            http, target, scopes=scopes, open_browser=open_browser, say=_say
        )
    where = store.save(credentials)
    auth._remember_url(target, store.directory)
    signed_in = connect(target)
    person = signed_in.me()
    allowed = credentials.scope or "nothing"
    _say(
        f"Signed in to {target} as {person.get('name') or person.get('email')}, "
        f"allowed: {allowed}. Kept in {where}."
    )
    return signed_in


def logout(url: str | None = None) -> None:
    """Ends your sign-in: Iki stops accepting its tokens, and they're forgotten here."""
    global _default
    store = auth.Store()
    chosen = url or auth.remembered_url(store.directory)
    if not chosen:
        return
    target = auth.normalize_url(chosen)
    credentials = store.load(target)
    if credentials is not None:
        with httpx2.Client(timeout=30.0) as http:
            auth.revoke(http, credentials)
        store.delete(target)
    with _lock:
        if _default is not None and _default.url == target:
            _default.close()
            _default = None
    _say(f"Signed out of {target}.")


def connect(url: str | None = None, *, token: str | None = None) -> Client:
    """Makes the client that ``iki.entities`` and the rest use, and returns it."""
    global _default
    made = Client(url, token=token)
    with _lock:
        if _default is not None:
            _default.close()
        _default = made
    return made


def client() -> Client:
    """The client that ``iki.entities`` and the rest use, made when first needed."""
    global _default
    with _lock:
        if _default is None:
            _default = Client()
        return _default


def me() -> MeOut:
    """Who you are."""
    return client().me()


def run(
    action: str,
    *,
    project: str | Mapping[str, Any],
    inputs: Mapping[str, Any] | None = None,
    params: Mapping[str, Any] | None = None,
    reason: str = "",
) -> RunOut:
    """Queues a run of `action`: see ``iki.runs.start``."""
    return client().runs.start(action, project=project, inputs=inputs, params=params, reason=reason)


def run_each(
    action: str,
    entities: Sequence[str | Mapping[str, Any]],
    *,
    each: str,
    project: str | Mapping[str, Any],
    inputs: Mapping[str, Any] | None = None,
    params: Mapping[str, Any] | None = None,
    reason: str = "",
) -> list[RunOut]:
    """Queues one run for each entity, all or none: see ``iki.runs.start_each``."""
    return client().runs.start_each(
        action,
        entities,
        each=each,
        project=project,
        inputs=inputs,
        params=params,
        reason=reason,
    )


def wait(
    runs: str | Mapping[str, Any] | Sequence[str | Mapping[str, Any]],
    *,
    timeout: float | None = None,
    check: bool = True,
    poll: float = 2.0,
) -> Any:  # noqa: ANN401 - a run, or a list of them, as given
    """Waits for runs to end: see ``iki.runs.wait``."""
    return client().runs.wait(runs, timeout=timeout, check=check, poll=poll)


def __getattr__(name: str) -> Any:  # noqa: ANN401 - a part of the default client
    if name in _PARTS:
        return getattr(client(), name)
    raise AttributeError(f"module 'iki' has no attribute {name!r}")
