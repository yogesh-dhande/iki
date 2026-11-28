"""Plugins' writers of file formats (ADR 0230), driven as Iki's own are.

A plugin declares a writer, a WebAssembly module built from its Rust crate, and a type maps
the writer's roles to its fields (``exports: [{export: <writer>, fields: {...}}]``). The
server gathers each record's values at the revision asked for, as it does for its own
formats, and hands them to the writer in the export sandbox (``IKI_EXPORT_SANDBOX``): a
separate Deployment with no Secrets, no token, no network and no cloud identity, where a
fresh process serves one export and is never used again. :class:`PluginWriter` is the
:class:`~iki_server.entities.exports.Writer` that talks to it, so one record's download and
many records' file (ADR 0229) are written, pinned, hashed and audited as Iki's own are.

What it can't do, it says: a writer's refusal of a record is the record's (422,
``writer_refused``); a writer that traps, runs out of fuel or memory, or passes its deadline
fails the export (422, ``writer_failed``); a sandbox that's down or full is 503
(``sandbox_unavailable``), to try again. A failed export is logged and counted, not
audited as a download, since no file left Iki.
"""

from __future__ import annotations

import contextlib
import json
import socket
import subprocess
import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from importlib.util import find_spec
from pathlib import Path
from typing import IO, Any

import structlog
from django.conf import settings
from opentelemetry import metrics

from iki_server.entities import registry
from iki_server.entities.sequence_files import Written
from iki_spec import units
from iki_spec.model import Writer
from iki_spec.writers import BUILD, DIRECTORY, IMAGE, MANIFEST
from iki_spec.writers import abi as writer_abi
from iki_spec.writers.limits import Limits
from iki_spec.writers.manifest import ACTIVE_MEDIA_TYPES
from iki_spec.writers.protocol import Session, SessionFailed, Unavailable, canonical

logger = structlog.get_logger("iki.plugin_writers")

failures = metrics.get_meter("iki.exports").create_counter(
    "iki.exports.writer_failures",
    description="Exports a plugin's writer refused or failed, or the sandbox couldn't take.",
)


def inline_deadline() -> float:
    """The longest a plugin's writer may take over a file written while its request
    waits: the deployment's (``IKI_EXPORT_INLINE_DEADLINE``), 120 seconds by default."""
    return float(settings.IKI_EXPORT_INLINE_DEADLINE)


def background_deadline() -> float:
    """And over one written by a background task: 30 minutes by default."""
    return float(settings.IKI_EXPORT_BACKGROUND_DEADLINE)


#: How long connecting to the sandbox may take.
CONNECT_TIMEOUT = 5
#: What a 503 asks a client to wait before trying again.
RETRY_AFTER = 5


class WriterRefused(Exception):  # noqa: N818 - said as "the writer refused"
    """The writer refused a record, saying why: the record's, not the writer's, fault."""


class WriterBroke(Exception):  # noqa: N818 - said as "the writer broke"
    """The writer failed: a trap, a limit, its deadline, or an answer that isn't the ABI."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SandboxUnavailable(Exception):  # noqa: N818 - said as "the sandbox is unavailable"
    """The sandbox couldn't take the export: try again."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PluginRecord:
    """A record at one revision, as a plugin's writer takes it: what Iki gathered of it,
    which the writer's session encodes for the ABI its module reads."""

    gathered: writer_abi.Record

    @property
    def id(self) -> uuid.UUID:
        return self.gathered.id

    @property
    def hrid(self) -> str:
        return self.gathered.hrid

    @property
    def name(self) -> str:
        return self.gathered.name

    @property
    def revision(self) -> int:
        return self.gathered.revision

    @property
    def revised_at(self) -> datetime:
        return self.gathered.revised_at


# --- What's installed ---------------------------------------------------------------------------


def writers() -> dict[str, Writer]:
    """Every installed plugin's writers, by qualified ID."""
    return {w.qualified_name: w for p in registry.plugins().values() for w in p.writers.values()}


def find(qualified: str) -> Writer | None:
    return writers().get(qualified)


@dataclass(frozen=True)
class Runnable:
    """A writer as the sandbox runs it: which module, by its digest, and where it's from."""

    writer: Writer
    sha256: str
    commit: str
    #: A development server's module file, run by ``IKI_EXPORT_SANDBOX=local``.
    path: str | None = None

    def identity(self, abi: int | None) -> dict[str, str]:
        """Which code wrote a file: the module, and the ABI it was given its requests in."""
        return {
            "plugin": self.writer.plugin,
            "writer": self.writer.qualified_name,
            "commit": self.commit,
            "module_sha256": self.sha256,
            "abi": "" if abi is None else str(abi),
        }


def _commit(plugin: str) -> str:
    for entry in (registry.installed() or {}).get("plugins", ()):
        if entry.get("name") == plugin:
            return str(entry.get("sha") or registry.LOCAL)
    return registry.LOCAL


@cache
def _image_manifest() -> dict[str, Any]:
    file = IMAGE / MANIFEST
    if not file.is_file():
        return {}
    found: dict[str, Any] = json.loads(file.read_text("utf-8")).get("writers", {})
    return found


def _built() -> tuple[Path | None, dict[str, Any]]:
    spec = find_spec("iki_installed_plugins")
    if spec is None or not spec.submodule_search_locations:
        return None, {}
    root = Path(next(iter(spec.submodule_search_locations))) / DIRECTORY
    file = root / BUILD
    if not file.is_file():
        return root, {}
    return root, json.loads(file.read_text("utf-8")).get("writers", {})


def runnable(writer: Writer) -> Runnable:
    """The module that runs as ``writer``. Raises :class:`SandboxUnavailable` when this
    deployment can't run it."""
    address = settings.IKI_EXPORT_SANDBOX
    commit = _commit(writer.plugin)
    if not address:
        raise SandboxUnavailable(
            "sandbox_unavailable",
            f"{writer.label} is a plugin's format, and this deployment runs no export sandbox "
            "to write it in: its administrator turns on the chart's sandbox.",
        )
    if address == "local":
        if not settings.IKI_ALLOW_LOCAL_PLUGINS:
            raise SandboxUnavailable("sandbox_unavailable", "Local writers need local plugins.")
        root, built = _built()
        entry = built.get(writer.qualified_name) or {}
        if root is None or not entry.get("built") or not entry.get("built_sha256"):
            raise SandboxUnavailable(
                "sandbox_unavailable",
                f"{writer.qualified_name}'s module wasn't built: build it, then the plugins.",
            )
        return Runnable(writer, entry["built_sha256"], commit, str(root / entry["built"]))
    entry = _image_manifest().get(writer.qualified_name)
    if entry is None:
        raise SandboxUnavailable(
            "sandbox_unavailable", f"This image has no module of {writer.qualified_name}."
        )
    if entry.get("wasm_sha256") != writer.sha256:
        # The image's build refuses this; a server that finds it anyway writes nothing.
        logger.error("writer_digest_mismatch", writer=writer.qualified_name)
        raise SandboxUnavailable(
            "sandbox_unavailable",
            f"This image's module of {writer.qualified_name} isn't the one its plugin pins.",
        )
    return Runnable(writer, writer.sha256, commit)


# --- The sandbox's streams ----------------------------------------------------------------------


class _Connection:
    """A session's two streams, and how to end them."""

    def __init__(self, reader: IO[bytes], writer: IO[bytes], close: Any) -> None:  # noqa: ANN401
        self.session = Session(reader, writer)
        self._close = close

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._close()


def _connect(address: str, deadline: float) -> _Connection:
    if address == "local":
        from iki_spec.writer_sandbox import start_child

        child = start_child(None)
        assert child.stdin is not None and child.stdout is not None  # noqa: S101 - pipes
        timer = threading.Timer(deadline, child.kill)
        timer.daemon = True
        timer.start()

        def close() -> None:
            timer.cancel()
            assert child.stdin is not None and child.stdout is not None  # noqa: S101 - pipes
            with contextlib.suppress(OSError):
                child.stdin.close()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
            with contextlib.suppress(OSError):
                child.stdout.close()

        return _Connection(child.stdout, child.stdin, close)
    from iki_spec.writer_sandbox import connect

    try:
        sock = connect(address, CONNECT_TIMEOUT)
    except (OSError, ValueError) as error:
        raise SandboxUnavailable(
            "sandbox_unavailable", f"The export sandbox can't be reached: {error}"
        ) from None
    # The sandbox kills a writer at its deadline and says so; a little past it, give up.
    sock.settimeout(deadline + 15)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    reader, writer = sock.makefile("rb"), sock.makefile("wb")

    def close_socket() -> None:
        for one in (writer, reader):
            with contextlib.suppress(OSError):
                one.close()
        sock.close()

    return _Connection(reader, writer, close_socket)


# --- The writer -------------------------------------------------------------------------------


class PluginWriter:
    """One file of a plugin's format, written by its writer in the sandbox: a fresh
    process, for this export alone. Each request is built for the ABI the module reads,
    which the sandbox says when it opens the export. Close it when the file's written."""

    def __init__(self, writer: Writer, *, deadline: float | None = None) -> None:
        self.writer = writer
        self._runnable = runnable(writer)
        self._deadline = inline_deadline() if deadline is None else deadline
        self._connection: _Connection | None = None
        self._encoder: writer_abi.Encoder | None = None
        self._notes: list[str] = []

    @property
    def identity(self) -> Mapping[str, str]:
        return self._runnable.identity(self._encoder.abi if self._encoder else None)

    def _session(self) -> Session:
        if self._connection is None:
            address = settings.IKI_EXPORT_SANDBOX
            connection = _connect(address, self._deadline)
            header: dict[str, Any] = {
                "writer": self.writer.qualified_name,
                "sha256": self._runnable.sha256,
                "memory": self.writer.memory,
                "deadline": self._deadline,
            }
            # The deployment's limits, where they aren't Iki's (ADR 0230).
            if limits := Limits(**settings.IKI_EXPORT_LIMITS).header():
                header["limits"] = limits
            if self._runnable.path is not None:
                header["path"] = self._runnable.path
            try:
                abi = connection.session.open(header)
                self._encoder = writer_abi.encoder(abi)
            except Unavailable as error:
                connection.close()
                raise self._unavailable(error) from None
            except ValueError as error:  # an ABI the sandbox runs and this server doesn't
                connection.close()
                raise self._unavailable(Unavailable("abi", str(error))) from None
            self._connection = connection
        return self._connection.session

    def _encoded(self) -> writer_abi.Encoder:
        self._session()
        assert self._encoder is not None  # noqa: S101 - set when the session opens
        return self._encoder

    def _unavailable(self, error: Unavailable) -> SandboxUnavailable:
        failures.add(1, {"writer": self.writer.qualified_name, "code": error.code})
        logger.warning(
            "writer_unavailable",
            writer=self.writer.qualified_name,
            code=error.code,
            error=str(error),
        )
        return SandboxUnavailable(
            error.code,
            "The export sandbox is busy. Try again in a moment."
            if error.code == "busy"
            else f"The export sandbox can't write {self.writer.label} now: {error}",
        )

    def _call(
        self, entry: str, request: Mapping[str, Any], record: PluginRecord | None = None
    ) -> tuple[bytes, list[str]]:
        try:
            answer = self._session().call(entry, canonical(request))
        except SessionFailed as failed:
            self.close()
            failures.add(1, {"writer": self.writer.qualified_name, "code": failed.code})
            # Which record it failed on, as ADR 0230 has it: by ID in the log, which never
            # holds values, and by HRID in what the person reads.
            logger.warning(
                "writer_failed",
                writer=self.writer.qualified_name,
                module=self._runnable.sha256,
                code=failed.code,
                error=str(failed),
                **({"record": str(record.id), "hrid": record.hrid} if record else {"call": entry}),
            )
            on = f" on {record.hrid}" if record is not None else ""
            raise WriterBroke(
                failed.code, f"{self.writer.label}'s writer failed{on}: {failed}"
            ) from None
        except Unavailable as error:
            self.close()
            raise self._unavailable(error) from None
        if answer.status == 1:
            message = str(answer.notes[0].get("message") if answer.notes else "")
            raise WriterRefused(message or "The writer refused it.")
        return answer.output, [_said(n) for n in answer.notes]

    def begin(self, records: int) -> bytes:
        self._notes = []
        export = writer_abi.Export(self.writer.qualified_name, self.writer.format, records)
        content, notes = self._call("begin", self._encoded().begin(export))
        # Notes on the file as a whole are said with its end's.
        self._notes = notes
        return content

    def record(self, record: PluginRecord) -> Written:
        content, notes = self._call("record", self._encoded().record(record.gathered), record)
        # What its writer wasn't given, said by Iki, so a file is never silently short.
        withheld = [
            f"Its {role.replace('_', ' ')} is a record you can't read, so it wasn't given "
            "to the writer."
            for role in sorted(record.gathered.withheld)
        ]
        return Written(content, [*withheld, *notes])

    def end(self) -> Written:
        content, notes = self._call("end", self._encoded().end())
        said, self._notes = [*self._notes, *notes], []
        return Written(content, said)

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None


def _said(note: Mapping[str, Any]) -> str:
    return str(note.get("message") or note.get("code") or "")


def is_active(media_type: str) -> bool:
    """Whether a browser would run it: HTML, SVG, scripts. No writer may write one."""
    return media_type.partition(";")[0].strip().lower() in ACTIVE_MEDIA_TYPES


# --- What a record gives a writer ---------------------------------------------------------------


def role_value(role_type: str, unit: str | None, field_unit: str | None, value: Any) -> Any:  # noqa: ANN401 - JSON
    """A field's value as the ABI gives a role it: a quantity as ``{value, unit}`` in the
    role's unit."""
    if role_type == "quantity":
        number = float(value)
        if unit and field_unit and unit != field_unit:
            number = units.convert(number, field_unit, unit)
        return {"value": number, "unit": unit or field_unit}
    return value
