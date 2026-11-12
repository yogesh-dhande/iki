"""The client: one deployment, as one person, through the REST API (``/api/v1``)."""

from __future__ import annotations

import builtins
import os
import platform
import re
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from importlib import metadata
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote

import httpx2

from iki import auth
from iki.errors import IkiError, NotSignedIn, RunFailed, TimedOut, from_response

if TYPE_CHECKING:
    import pandas as pd

    from iki.schema import (
        ActionOut,
        AttemptLogOut,
        CreatedOut,
        EntityOut,
        EntityTypeOut,
        LineageGraphOut,
        MeOut,
        OriginOut,
        ProjectOut,
        RevisionOut,
        RunAttemptOut,
        RunOut,
    )

__all__ = ["Client", "Results"]

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
#: Runs in these states have ended.
FINISHED = frozenset({"succeeded", "failed", "cancelled", "timed_out", "rejected", "declined"})
#: Retries of a request the API asked to wait for (429, or 503 while it restarts).
_RETRIES = 4
_LONGEST_WAIT = 60.0


def _version() -> str:
    try:
        return metadata.version("ikisci")
    except metadata.PackageNotFoundError:
        return "0+unknown"


def _path(template: str, **parts: str | int) -> str:
    """An API path, each part escaped, so a value can't reach another endpoint."""
    return template.format(**{k: quote(str(v), safe="") for k, v in parts.items()})


class Results[T]:
    """The items a list or search finds, fetched a page at a time as you go through
    them. Iterate over it, or call ``list()`` on it; ``to_pandas()`` makes a DataFrame.
    """

    def __init__(
        self,
        fetch: Callable[[str | None], Mapping[str, Any]],
        frame: Callable[[list[T]], pd.DataFrame] | None = None,
    ) -> None:
        self._fetch = fetch
        self._frame = frame

    def __iter__(self) -> Iterator[T]:
        cursor: str | None = None
        while True:
            page = self._fetch(cursor)
            yield from cast("list[T]", page["items"])
            cursor = page.get("next_cursor")
            if not cursor:
                return

    def first(self) -> T | None:
        """The first item, or None: fetches one page at most."""
        return next(iter(self), None)

    def to_pandas(self, limit: int | None = None) -> pd.DataFrame:
        """Everything it finds (or the first `limit`), as a DataFrame."""
        items: list[T] = []
        for item in self:
            if limit is not None and len(items) >= limit:
                break
            items.append(item)
        from iki import frames

        return self._frame(items) if self._frame else frames.records(items)


class Client:
    """Iki, as you: ``iki.Client()`` uses your sign-in (see ``iki.login``), or
    ``IKI_URL`` and ``IKI_TOKEN``.

    Its parts follow the API: ``entities``, ``types``, ``projects``, ``actions``,
    ``runs`` and ``files``. For anything else, ``request`` calls the API directly.
    """

    def __init__(
        self,
        url: str | None = None,
        *,
        token: str | None = None,
        timeout: float = 30.0,
        store: auth.Store | None = None,
        transport: httpx2.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._store = store or auth.Store()
        chosen = url or os.environ.get("IKI_URL") or auth.remembered_url(self._store.directory)
        if not chosen:
            raise NotSignedIn(
                "Which Iki? Sign in with iki.login('https://iki.example.com'), or set IKI_URL."
            )
        self.url = auth.normalize_url(chosen)
        self._token = token or os.environ.get("IKI_TOKEN") or None
        self._credentials: auth.Credentials | None = None
        if self._token is None:
            self._credentials = self._store.load(self.url)
            if self._credentials is None:
                raise NotSignedIn(f"You're not signed in to {self.url}: run iki.login().")
        self._sleep = sleep
        self._http = httpx2.Client(
            base_url=self.url,
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
            headers={
                "User-Agent": f"ikisci/{_version()} python/{platform.python_version()}",
                "Accept": "application/json",
            },
        )
        self.entities = Entities(self)
        self.types = Types(self)
        self.projects = Projects(self)
        self.actions = Actions(self)
        self.runs = Runs(self)
        self.files = Files(self)

    # --- The connection -------------------------------------------------------------

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"<iki.Client {self.url}>"

    def _access_token(self, *, renew: bool = False) -> str:
        if self._token is not None:
            return self._token
        if self._credentials is None:  # pragma: no cover - made with one or the other
            raise NotSignedIn(f"You're not signed in to {self.url}: run iki.login().")
        if renew or self._credentials.expiring():
            self._credentials = auth.renew(self._http, self._credentials, self._store)
        return self._credentials.access_token

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,  # noqa: ANN401 - JSON
        headers: Mapping[str, str] | None = None,
    ) -> Any:  # noqa: ANN401 - JSON
        """Calls ``/api/v1`` + `path` and returns its JSON, or raises an ``ApiError``.

        Expired access tokens are renewed, and requests the API asks to wait for (429)
        are sent again after waiting as long as it says.
        """
        response = self._send(method, path, params=params, json=json, headers=headers)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def _send(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,  # noqa: ANN401
        headers: Mapping[str, str] | None = None,
        stream: bool = False,
    ) -> httpx2.Response:
        query = {k: _param(v) for k, v in (params or {}).items() if v is not None}
        renewed = False
        for attempt in range(_RETRIES + 1):
            sent = {**(headers or {}), "Authorization": f"Bearer {self._access_token()}"}
            request = self._http.build_request(
                method, "/api/v1" + path, params=query, json=json, headers=sent
            )
            response = self._http.send(request, stream=stream)
            if response.status_code == 401 and not renewed and self._credentials is not None:
                response.close()
                self._access_token(renew=True)
                renewed = True
                continue
            if response.status_code == 401:
                response.read()
                raise NotSignedIn(
                    f"{self.url} refused the sign-in: "
                    + ("check IKI_TOKEN." if self._token is not None else "run iki.login() again.")
                )
            waits = response.status_code == 429 or (
                response.status_code == 503 and method in ("GET", "HEAD")
            )
            if waits and attempt < _RETRIES:
                response.close()
                self._sleep(_retry_after(response, attempt))
                continue
            if response.status_code >= 400:
                response.read()
                try:
                    body = response.json()
                except ValueError:
                    body = None
                raise from_response(response.status_code, body)
            return response
        raise AssertionError("unreachable")  # pragma: no cover

    def me(self) -> MeOut:
        """Who you are, with your organization roles."""
        return cast("MeOut", self.request("GET", "/me"))

    # --- Refs ------------------------------------------------------------------------

    def _project_id(self, project: str | Mapping[str, Any]) -> str:
        if isinstance(project, Mapping):
            return str(project["id"])
        if _UUID.match(project):
            return project
        return str(self.projects.get(project)["id"])

    def _entity_id(self, entity: str | Mapping[str, Any]) -> str:
        if isinstance(entity, Mapping):
            return str(entity["id"])
        if _UUID.match(entity):
            return entity
        return str(self.entities.get(entity)["id"])

    def _inputs(self, inputs: Mapping[str, Any] | None) -> dict[str, str | list[str]]:
        result: dict[str, str | list[str]] = {}
        for name, value in (inputs or {}).items():
            if isinstance(value, str | Mapping):
                result[name] = self._entity_id(value)
            else:
                result[name] = [self._entity_id(v) for v in value]
        return result


def _param(value: Any) -> Any:  # noqa: ANN401
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list | tuple):
        return [_param(v) for v in value]
    return value


def _retry_after(response: httpx2.Response, attempt: int) -> float:
    text = response.headers.get("Retry-After", "")
    try:
        wait = float(text)
    except ValueError:
        wait = 2.0**attempt
    return min(max(wait, 0.5), _LONGEST_WAIT)


def _sort(sort: str | None) -> dict[str, Any] | None:
    """``"-created_at"`` sorts newest first; ``"name"`` by name."""
    if not sort:
        return None
    if sort.startswith("-"):
        return {"field": sort[1:], "descending": True}
    return {"field": sort, "descending": False}


class _Part:
    def __init__(self, client: Client) -> None:
        self._client = client


class Entities(_Part):
    """Entities: search, read, create, change, archive."""

    def search(
        self,
        type: str | None = None,
        *,
        project: str | Mapping[str, Any] | None = None,
        q: str | None = None,
        where: Mapping[str, Any] | None = None,
        sort: str | None = None,
        collection: str | None = None,
        archived: bool | None = False,
        exact_type: bool = False,
        page_size: int = 100,
    ) -> Results[EntityOut]:
        """Entities you can read, a page at a time.

        `type` includes its subtypes unless `exact_type`. `where` is a condition, as
        the API takes it::

            iki.entities.search(
                "protein.Protein",
                project="kinase-atlas",
                where={"field": "length", "op": "gte", "value": 300},
                sort="-created_at",
            ).to_pandas()

        Combine conditions with ``{"all": [...]}``, ``{"any": [...]}`` or
        ``{"not": ...}``. `archived` is False by default; None finds both.
        """
        client = self._client
        body: dict[str, Any] = {
            "type": type,
            "project": client._project_id(project) if project is not None else None,
            "q": q,
            "where": dict(where) if where is not None else None,
            "sort": _sort(sort),
            "collection": collection,
            "archived": archived,
            "exact_type": exact_type,
            "limit": page_size,
        }
        body = {k: v for k, v in body.items() if v is not None}

        def fetch(cursor: str | None) -> Mapping[str, Any]:
            return cast(
                "Mapping[str, Any]",
                client.request("POST", "/entities/search", json={**body, "cursor": cursor}),
            )

        from iki import frames

        return Results(fetch, frames.entities)

    def get(self, ref: str) -> EntityOut:
        """An entity, by its ID or HRID (``PRT-000123``)."""
        return cast("EntityOut", self._client.request("GET", _path("/entities/{ref}", ref=ref)))

    def create(
        self,
        type: str,
        values: Mapping[str, Any],
        *,
        project: str | Mapping[str, Any],
        reason: str = "",
        allow_duplicate: bool = False,
    ) -> CreatedOut:
        """A new entity in `project` (its ID or slug). If its type reuses duplicates
        and one with the same identity exists, you get that one back, with
        ``reused`` true, and it's added to the project."""
        body = {
            "type": type,
            "project": self._client._project_id(project),
            "values": dict(values),
            "reason": reason,
            "allow_duplicate": allow_duplicate,
        }
        return cast("CreatedOut", self._client.request("POST", "/entities", json=body))

    def update(
        self, ref: str, values: Mapping[str, Any], *, revision: int, reason: str
    ) -> EntityOut:
        """Changes some of an entity's values as one new revision. `revision` is the
        one you read: if it has changed since, nothing changes and ``iki.Conflict`` is
        raised. A value of None unsets that field."""
        return self._patch(ref, {"values": dict(values), "reason": reason}, revision)

    def archive(self, ref: str, *, revision: int, reason: str) -> EntityOut:
        """Archives an entity: it's kept, with its history, and can't change."""
        return self._patch(ref, {"archived": True, "reason": reason}, revision)

    def restore(self, ref: str, *, revision: int, reason: str) -> EntityOut:
        """Restores an archived entity."""
        return self._patch(ref, {"archived": False, "reason": reason}, revision)

    def _patch(self, ref: str, body: dict[str, Any], revision: int) -> EntityOut:
        return cast(
            "EntityOut",
            self._client.request(
                "PATCH",
                _path("/entities/{ref}", ref=ref),
                json=body,
                headers={"If-Match": f'"{revision}"'},
            ),
        )

    def history(self, ref: str) -> Results[RevisionOut]:
        """Its revisions, newest first: who changed what, when, how and why."""
        client = self._client
        path = _path("/entities/{ref}/revisions", ref=ref)
        return Results(lambda c: client.request("GET", path, params={"cursor": c}))

    def provenance(self, ref: str) -> dict[str, Any]:
        """Where each of its values came from: the person, run or import that set it."""
        return cast(
            "dict[str, Any]",
            self._client.request("GET", _path("/entities/{ref}/provenance", ref=ref)),
        )

    def origins(self, ref: str) -> builtins.list[OriginOut]:
        """Where runs imported it from, newest first: a database's record (its version,
        link and when it was fetched) or a record in a file."""
        return cast(
            "builtins.list[OriginOut]",
            self._client.request("GET", _path("/entities/{ref}/origins", ref=ref)),
        )

    def lineage(self, ref: str, *, up: int = 1, down: int = 1) -> LineageGraphOut:
        """What it was made from (`up` generations) and what was made from it (`down`)."""
        return cast(
            "LineageGraphOut",
            self._client.request(
                "GET",
                _path("/entities/{ref}/lineage/graph", ref=ref),
                params={"up": up, "down": down},
            ),
        )


class Types(_Part):
    """The installed entity types."""

    def list(self) -> builtins.list[EntityTypeOut]:
        return cast("builtins.list[EntityTypeOut]", self._client.request("GET", "/entity-types"))

    def get(self, name: str) -> EntityTypeOut:
        """A type, such as ``protein.Protein``: its fields, units and rules."""
        return cast(
            "EntityTypeOut", self._client.request("GET", _path("/entity-types/{n}", n=name))
        )


class Projects(_Part):
    """Projects you can see."""

    def list(
        self, *, member: bool | None = None, archived: bool | None = False
    ) -> Results[ProjectOut]:
        client = self._client
        params = {"member": member, "archived": archived}
        return Results(lambda c: client.request("GET", "/projects", params={**params, "cursor": c}))

    def get(self, project: str) -> ProjectOut:
        """A project, by its ID or its slug (``kinase-atlas``)."""
        if _UUID.match(project):
            return cast(
                "ProjectOut",
                self._client.request("GET", _path("/projects/{id}", id=project)),
            )
        for found in self.list(archived=None):
            if found["slug"] == project:
                return found
        raise IkiError(f"No project {project!r} that you can see.")


class Actions(_Part):
    """The installed actions."""

    def list(self, *, takes: Sequence[str] = ()) -> builtins.list[ActionOut]:
        """Every action, or those that take entities of each of the types in `takes`."""
        return cast(
            "builtins.list[ActionOut]",
            self._client.request("GET", "/actions", params={"takes": list(takes)}),
        )

    def get(self, name: str) -> ActionOut:
        """An action, such as ``protein.compute_properties``: its inputs, params,
        effects and where it runs."""
        return cast("ActionOut", self._client.request("GET", _path("/actions/{n}", n=name)))


class Runs(_Part):
    """Runs of actions: start them, follow them, wait for them."""

    def start(
        self,
        action: str,
        *,
        project: str | Mapping[str, Any],
        inputs: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        reason: str = "",
        reuse: bool = True,
        add_to_project: bool = False,
    ) -> RunOut:
        """Queues a run. `inputs` name entities by ID, HRID or the entity itself:
        ``{"protein": "PRT-000123"}``, or a list for inputs that take many.

        For a deterministic action, an identical run's results are reused when you can
        read one: the run ends at once, with ``reused_from``. ``reuse=False`` runs it.

        A run reads only what's in its project. Inputs from other projects raise an
        :class:`ApiError` with code ``inputs_outside_project`` (its ``problem`` lists
        them in ``entities``), unless ``add_to_project=True`` adds them to it first: its
        members can then read them."""
        client = self._client
        body = {
            "project": client._project_id(project),
            "inputs": client._inputs(inputs),
            "params": dict(params or {}),
            "reason": reason,
            "reuse": reuse,
            "add_to_project": add_to_project,
        }
        return cast(
            "RunOut",
            client.request("POST", _path("/actions/{n}/runs", n=action), json=body),
        )

    def start_each(
        self,
        action: str,
        entities: Iterable[str | Mapping[str, Any]],
        *,
        each: str,
        project: str | Mapping[str, Any],
        inputs: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        reason: str = "",
        reuse: bool = True,
        add_to_project: bool = False,
    ) -> builtins.list[RunOut]:
        """Queues one run for each of `entities` as its `each` input, all or none:
        if any can't run, none is queued, and the error says which. Runs identical to
        earlier ones reuse their results, and inputs from other projects need
        `add_to_project`, as for :meth:`start`."""
        client = self._client
        body = {
            "project": client._project_id(project),
            "each": each,
            "entities": [client._entity_id(e) for e in entities],
            "inputs": client._inputs(inputs),
            "params": dict(params or {}),
            "reason": reason,
            "reuse": reuse,
            "add_to_project": add_to_project,
        }
        batch = client.request("POST", _path("/actions/{n}/runs/batch", n=action), json=body)
        return cast("builtins.list[RunOut]", batch["runs"])

    def get(self, ref: str | Mapping[str, Any]) -> RunOut:
        """A run, by its ID or HRID (``RUN-000045``)."""
        return cast("RunOut", self._client.request("GET", _path("/runs/{r}", r=_ref(ref))))

    def list(
        self,
        *,
        project: str | Mapping[str, Any] | None = None,
        action: str | None = None,
        status: str | None = None,
    ) -> Results[RunOut]:
        """Runs you can read, newest first."""
        client = self._client
        params = {
            "project": client._project_id(project) if project is not None else None,
            "action": action,
            "status": status,
        }
        from iki import frames

        return Results(
            lambda c: client.request("GET", "/runs", params={**params, "cursor": c}),
            frames.runs,
        )

    def reproduce(self, ref: str | Mapping[str, Any], *, reason: str = "") -> RunOut:
        """Queues a run that does this one again exactly: the same plugin commit, params,
        settings, seed, and inputs at the revisions it read. It writes nothing; once it
        ends, its ``reproduction`` says whether its results match."""
        return cast(
            "RunOut",
            self._client.request(
                "POST", _path("/runs/{r}/reproduce", r=_ref(ref)), json={"reason": reason}
            ),
        )

    def cancel(self, ref: str | Mapping[str, Any], *, reason: str = "") -> RunOut:
        """Cancels a run: at once if it hasn't started, else its worker stops it."""
        return cast(
            "RunOut",
            self._client.request(
                "POST", _path("/runs/{r}/cancel", r=_ref(ref)), json={"reason": reason}
            ),
        )

    def attempts(self, ref: str | Mapping[str, Any]) -> builtins.list[RunAttemptOut]:
        """Its attempts, oldest first: where each ran and how it ended."""
        return cast(
            "builtins.list[RunAttemptOut]",
            self._client.request("GET", _path("/runs/{r}/attempts", r=_ref(ref))),
        )

    def log(self, ref: str | Mapping[str, Any], *, attempt: int | None = None) -> str:
        """What an attempt printed (the latest, unless you name one)."""
        run = _ref(ref)
        number = attempt or max((a["attempt"] for a in self.attempts(run)), default=1)
        path = _path("/runs/{r}/attempts/{n}/log", r=run, n=number)
        text, offset = [], 0
        while True:
            page = cast(
                "AttemptLogOut", self._client.request("GET", path, params={"offset": offset})
            )
            text.append(page["text"])
            if not page["more"]:
                return "".join(text)
            offset = page["next_offset"]

    def wait(
        self,
        runs: str | Mapping[str, Any] | Sequence[str | Mapping[str, Any]],
        *,
        timeout: float | None = None,
        check: bool = True,
        poll: float = 2.0,
    ) -> Any:  # noqa: ANN401 - a run, or a list of them, as given
        """Waits until each run has ended, and returns it (or them, in order).

        With `check`, a run that didn't succeed raises ``iki.RunFailed``. With a
        `timeout` (in seconds), waiting longer raises ``iki.TimedOut``; the runs go on.
        """
        single = isinstance(runs, str | Mapping)
        refs = [_ref(runs)] if isinstance(runs, str | Mapping) else [_ref(r) for r in runs]
        deadline = None if timeout is None else time.monotonic() + timeout
        done: dict[str, RunOut] = {}
        delay = min(poll, 1.0)
        while True:
            for ref in refs:
                if ref not in done:
                    run = self.get(ref)
                    if run["status"] in FINISHED:
                        done[ref] = run
            if len(done) == len(refs):
                break
            if deadline is not None and time.monotonic() + delay > deadline:
                waiting = [r for r in refs if r not in done]
                raise TimedOut(f"Still going after {timeout} s: {', '.join(waiting)}.")
            self._client._sleep(delay)
            delay = min(delay * 1.5, poll)
        ended = [done[r] for r in refs]
        if check:
            for run in ended:
                if run["status"] != "succeeded":
                    raise RunFailed(cast("dict[str, Any]", run))
        return ended[0] if single else ended


def _ref(ref: str | Mapping[str, Any]) -> str:
    """A run's HRID (as people read it in messages), or the ID or HRID given."""
    if isinstance(ref, Mapping):
        return str(ref.get("hrid") or ref["id"])
    return ref


class Files(_Part):
    """Files that entities' values refer to."""

    def download(self, file_id: str, to: str | os.PathLike[str]) -> str:
        """Saves a file's content at `to` and returns where. Its checksum is checked
        against the one Iki recorded, so a damaged download is never kept."""
        import hashlib
        from pathlib import Path, PurePosixPath

        info = self._client.request("GET", _path("/files/{f}", f=file_id))
        target = Path(to)
        if target.is_dir():
            # Only the name's last part, so a name can't put the file somewhere else.
            name = PurePosixPath(str(info.get("name") or "").replace("\\", "/")).name
            target = target / (name if name not in ("", ".", "..") else file_id)
        partial = target.with_name(target.name + ".part")
        digest = hashlib.sha256()
        # It answers with a short-lived signed URL. That's fetched without Iki's token,
        # which is for Iki alone.
        signed = self._client._send("GET", _path("/files/{f}/content", f=file_id))
        location = signed.headers.get("Location")
        if signed.status_code not in (301, 302, 303, 307) or not location:
            raise IkiError(f"{self._client.url} didn't say where to download {file_id} from.")
        response = self._client._http.send(
            self._client._http.build_request("GET", location), stream=True
        )
        try:
            if response.status_code != 200:
                raise IkiError(
                    f"The file store refused the download of {file_id} "
                    f"({response.status_code}): try again."
                )
            with partial.open("wb") as out:
                for chunk in response.iter_bytes():
                    digest.update(chunk)
                    out.write(chunk)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        finally:
            response.close()
        expected = str(info.get("sha256") or "")
        if expected and digest.hexdigest() != expected:
            partial.unlink(missing_ok=True)
            raise IkiError(f"The download of {file_id} was damaged: try again.")
        partial.replace(target)
        return str(target)
