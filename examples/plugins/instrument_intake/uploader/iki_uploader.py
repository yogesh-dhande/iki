"""Send an instrument's exports to Iki from the instrument's PC.

Each export becomes one record in the facility's project, made by the uploader's
own identity in Iki. A file is uploaded, or, if it's large, written to the
facility's versioned bucket and registered there. Iki then checks the file's
bytes and scans them for malware. The uploader creates the record that holds the
file only once the scan finds it clean. A trigger in the facility's project then
reads the file into the record.

Run it from the PC's scheduler, every few minutes::

    python iki_uploader.py --config uploader.toml            # every export in the folder
    python iki_uploader.py --config uploader.toml FILE ...   # just these files

Running it again is safe. A journal next to the configuration remembers what each
export became. The record's key field holds the export's SHA-256, so a record is never
made twice, even when the journal is lost or another PC sends the same export.

It exits 0 when every export has a record or is waiting for its scan, 1 when one
failed or is held by the scan, and 2 when the configuration is wrong.
"""

from __future__ import annotations

import argparse
import contextlib
import fnmatch
import hashlib
import json
import logging
import os
import platform
import re
import sys
import time
import tomllib
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx2

import iki

log = logging.getLogger("iki_uploader")

_SIZE = re.compile(r"^(\d+)\s*(B|KiB|MiB|GiB|TiB)?$")
_UNITS = {None: 1, "B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4}
_CHUNK = 1024 * 1024
#: Token exchange for an agent acting for a person (RFC 8693, ADR 0073).
_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
_DELEGATION = "urn:ikisci:params:oauth:token-type:delegation"

Outcome = Literal["recorded", "found", "waiting", "held", "failed", "skipped"]


class ConfigError(Exception):
    """The configuration can't be used, and says how to fix it."""


def _size(text: str | int) -> int:
    if isinstance(text, int):
        return text
    match = _SIZE.match(text.strip())
    if match is None:
        raise ConfigError(f"{text!r} isn't a size such as 512MiB or 2GiB.")
    return int(match[1]) * _UNITS[match[2]]


def _seconds(text: str | int) -> float:
    if isinstance(text, int | float):
        return float(text)
    match = re.match(r"^(\d+)\s*(s|m|h)$", text.strip())
    if match is None:
        raise ConfigError(f"{text!r} isn't a duration such as 90s, 10m or 2h.")
    return int(match[1]) * {"s": 1, "m": 60, "h": 3600}[match[2]]


@dataclass(frozen=True)
class Config:
    url: str
    project: str
    client_id: str
    client_secret: str
    record_type: str
    file_field: str
    key_field: str
    values: Mapping[str, Any] = field(default_factory=dict)
    #: An agent acting for a person: the grant the person's delegation gave (ADR 0073).
    delegation: str = ""
    folder: Path | None = None
    patterns: tuple[str, ...] = ("*",)
    #: A file changed more recently than this is still being written.
    settle: float = 60.0
    mode: Literal["upload", "register", "auto"] = "upload"
    register_over: int = 1024**3
    bucket: str = ""
    endpoint_url: str = ""
    media_type: str = ""
    journal: Path = Path("iki-uploader-journal.json")
    #: How long to wait for Iki to check and scan one file before trying it next time.
    scan_timeout: float = 1800.0
    attempts: int = 5

    @classmethod
    def load(cls, path: Path, environ: Mapping[str, str] = os.environ) -> Config:
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise ConfigError(f"Can't read {path}: {error}") from None
        here = path.parent
        try:
            server, record = data["iki"], data["record"]
            files, bucket = data.get("files", {}), data.get("bucket", {})
            secret = _secret(server, environ, here)
            mode = files.get("mode", "auto" if bucket else "upload")
            if mode not in ("upload", "register", "auto"):
                raise ConfigError("files.mode is upload, register or auto.")
            if mode != "upload" and not bucket.get("uri"):
                raise ConfigError(f"files.mode {mode} needs [bucket] uri.")
            uri = bucket.get("uri", "")
            if uri and not re.match(r"^(s3|gs)://[^/]+/", uri):
                raise ConfigError("bucket.uri is s3://bucket/prefix/ or gs://bucket/prefix/.")
            folder = files.get("folder")
            return cls(
                url=server["url"],
                project=server["project"],
                client_id=server["client_id"],
                client_secret=secret,
                delegation=server.get("delegation", ""),
                record_type=record["type"],
                file_field=record["file_field"],
                key_field=record["key_field"],
                values=dict(record.get("values", {})),
                folder=None if folder is None else here / folder,
                patterns=tuple(files.get("patterns", ["*"])),
                settle=_seconds(files.get("settle", 60)),
                mode=mode,
                register_over=_size(files.get("register_over", "1GiB")),
                bucket=uri if not uri or uri.endswith("/") else uri + "/",
                endpoint_url=bucket.get("endpoint_url", ""),
                media_type=files.get("media_type", ""),
                journal=here / files.get("journal", "iki-uploader-journal.json"),
                scan_timeout=_seconds(files.get("scan_timeout", "30m")),
                attempts=int(files.get("attempts", 5)),
            )
        except KeyError as missing:
            raise ConfigError(f"{path} needs {missing.args[0]}.") from None


def _secret(server: Mapping[str, Any], environ: Mapping[str, str], here: Path) -> str:
    """The credential's secret: never in the configuration itself, which people read."""
    if "client_secret" in server:
        raise ConfigError(
            "Keep the secret out of the configuration: name an environment variable "
            "(client_secret_env) or a file only the uploader's account can read "
            "(client_secret_file)."
        )
    if name := server.get("client_secret_file"):
        try:
            return (here / name).read_text(encoding="utf-8").strip()
        except OSError as error:
            raise ConfigError(f"Can't read the secret from {name}: {error}") from None
    name = server.get("client_secret_env", "IKI_UPLOADER_SECRET")
    if not environ.get(name):
        raise ConfigError(f"Set {name} to the credential's secret.")
    return environ[name]


# --- Signing in -------------------------------------------------------------------------


class Session:
    """The uploader's Iki client, with an access token renewed before it expires.

    A service account's credential gets a token with client credentials. With a
    delegation, the same credential exchanges the person's grant for a token that acts
    for them, so what it adds names the person and the agent (ADR 0073)."""

    def __init__(
        self,
        config: Config,
        *,
        transport: httpx2.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self._transport = transport
        self._clock = clock
        self._sleep = sleep
        self._client: iki.Client | None = None
        self._expires = 0.0

    def client(self) -> iki.Client:
        if self._client is None or self._clock() > self._expires - 120:
            if self._client is not None:
                self._client.close()
            token, lifetime = self._token()
            self._expires = self._clock() + lifetime
            self._client = iki.Client(
                self.config.url, token=token, transport=self._transport, sleep=self._sleep
            )
        return self._client

    def renew(self) -> None:
        self._expires = 0.0

    def _token(self) -> tuple[str, float]:
        form = (
            {
                "grant_type": _EXCHANGE,
                "subject_token": self.config.delegation,
                "subject_token_type": _DELEGATION,
            }
            if self.config.delegation
            else {"grant_type": "client_credentials"}
        )
        with httpx2.Client(base_url=self.config.url, transport=self._transport) as http:
            answer = http.post(
                "/oauth/token/",
                data=form,
                auth=(self.config.client_id, self.config.client_secret),
            )
        if answer.status_code != 200:
            raise iki.NotSignedIn(
                f"{self.config.url} refused the uploader's credential ({answer.status_code}): "
                f"{answer.text[:300]}"
            )
        body = answer.json()
        return str(body["access_token"]), float(body.get("expires_in", 3600))

    def close(self) -> None:
        if self._client is not None:
            self._client.close()


# --- Buckets ------------------------------------------------------------------------------


class Bucket(Protocol):
    def put(self, path: Path, key: str, size: int) -> str:
        """Writes the file at `key`, if it isn't there, and returns the object's version."""
        ...


class S3Bucket:
    """An S3 bucket that keeps versions, with the PC's own AWS credentials."""

    def __init__(self, name: str, endpoint_url: str = "", client: Any = None) -> None:  # noqa: ANN401
        if client is None:
            import boto3

            client = boto3.client("s3", endpoint_url=endpoint_url or None)
        self.name = name
        self.s3 = client

    def put(self, path: Path, key: str, size: int) -> str:
        try:
            found = self.s3.head_object(Bucket=self.name, Key=key)
        except self.s3.exceptions.ClientError as error:
            if error.response.get("Error", {}).get("Code") not in ("404", "NoSuchKey"):
                raise
            found = None
        if found is None or found["ContentLength"] != size:
            self.s3.upload_file(str(path), self.name, key)
            found = self.s3.head_object(Bucket=self.name, Key=key)
        version = found.get("VersionId") or "null"
        if version == "null":
            raise ConfigError(
                f"s3://{self.name} doesn't keep versions: Iki registers only a version "
                "that can't be overwritten. Turn on versioning for the bucket."
            )
        return str(version)


class GcsBucket:
    """A Cloud Storage bucket that keeps versions, with the PC's own Google credentials."""

    def __init__(self, name: str, client: Any = None) -> None:  # noqa: ANN401
        if client is None:
            from google.cloud import storage

            client = storage.Client()
        self.bucket = client.bucket(name)

    def put(self, path: Path, key: str, size: int) -> str:
        blob = self.bucket.get_blob(key)
        if blob is None or blob.size != size:
            blob = self.bucket.blob(key)
            # Content-addressed keys: if another PC wrote it first, its bytes are these.
            with contextlib.suppress(_precondition_failed()):
                blob.upload_from_filename(str(path), if_generation_match=0)
            blob = self.bucket.get_blob(key)
        return str(blob.generation)


def _precondition_failed() -> type[Exception]:
    from google.api_core.exceptions import PreconditionFailed

    return PreconditionFailed  # type: ignore[no-any-return]


def bucket_for(config: Config) -> tuple[Bucket, str, str]:
    """The bucket, its URI's scheme and bucket name, and the prefix keys go under."""
    scheme, _, rest = config.bucket.partition("://")
    name, _, prefix = rest.partition("/")
    if scheme == "s3":
        return S3Bucket(name, config.endpoint_url), f"s3://{name}/", prefix
    return GcsBucket(name), f"gs://{name}/", prefix


# --- The journal -------------------------------------------------------------------------


class Journal:
    """What each export became, kept beside the configuration and replaced whole."""

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            self.data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.data = {}
        except (OSError, ValueError) as error:
            raise ConfigError(
                f"The journal {path} can't be read ({error}). Move it aside: the uploader "
                "finds the records it made in Iki by their key, and starts a new one."
            ) from None
        self.data.setdefault("hashes", {})
        self.data.setdefault("exports", {})

    def export(self, sha256: str) -> dict[str, Any]:
        entry: dict[str, Any] = self.data["exports"].setdefault(sha256, {})
        return entry

    def sha256(self, path: Path) -> str:
        """The file's SHA-256, read once for each size and modification time."""
        stat = path.stat()
        key = str(path.resolve())
        seen = self.data["hashes"].get(key)
        if seen and (seen["size"], seen["mtime_ns"]) == (stat.st_size, stat.st_mtime_ns):
            return str(seen["sha256"])
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(_CHUNK):
                digest.update(chunk)
        sha256 = digest.hexdigest()
        self.data["hashes"][key] = {
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "sha256": sha256,
        }
        return sha256

    def save(self) -> None:
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(self.data, indent=1, sort_keys=True), encoding="utf-8")
        temporary.replace(self.path)


@contextlib.contextmanager
def only_one(journal: Path, stale: float = 6 * 3600) -> Iterator[bool]:
    """Whether this is the only uploader using the journal. A lock older than `stale`
    seconds is left by one that stopped, and taken over."""
    lock = journal.with_name(journal.name + ".lock")
    with contextlib.suppress(FileNotFoundError):
        if time.time() - lock.stat().st_mtime > stale:
            lock.unlink()
    try:
        handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        yield False
        return
    try:
        os.write(handle, str(os.getpid()).encode())
        os.close(handle)
        yield True
    finally:
        lock.unlink(missing_ok=True)


# --- Sending -------------------------------------------------------------------------------


def _transient(error: BaseException) -> bool:
    if isinstance(error, httpx2.TransportError):
        return True
    return isinstance(error, iki.ApiError) and (error.status >= 500 or error.status == 429)


class Uploader:
    def __init__(
        self,
        config: Config,
        session: Session,
        journal: Journal,
        *,
        bucket: tuple[Bucket, str, str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.session = session
        self.journal = journal
        self._bucket = bucket
        self._sleep = sleep
        self._clock = clock

    def _call(self, what: str, step: Callable[[iki.Client], Any]) -> Any:  # noqa: ANN401
        """`step`, again after a pause when the network or Iki failed for now, and with a
        new token if the old one ran out."""
        for attempt in range(1, self.config.attempts + 1):
            try:
                return step(self.session.client())
            except iki.NotSignedIn:
                if attempt == self.config.attempts:
                    raise
                self.session.renew()
            except (httpx2.TransportError, iki.ApiError) as error:
                if not _transient(error) or attempt == self.config.attempts:
                    raise
                pause = min(2.0**attempt, 60.0)
                log.warning("%s failed for now (%s); trying again in %.0f s.", what, error, pause)
                self._sleep(pause)
        raise AssertionError("unreachable")  # pragma: no cover

    def pending(self, paths: list[Path] | None = None) -> list[Path]:
        """The files to send: those given, or the folder's that match and have settled."""
        if paths:
            return paths
        folder = self.config.folder
        if folder is None:
            raise ConfigError("Name the files to send, or set files.folder.")
        now = self._clock()
        found = []
        for path in sorted(folder.iterdir()):
            if not path.is_file() or not any(
                fnmatch.fnmatch(path.name, p) for p in self.config.patterns
            ):
                continue
            if now - path.stat().st_mtime < self.config.settle:
                log.info("%s is still being written; it'll be sent next time.", path.name)
                continue
            found.append(path)
        return found

    def send(self, path: Path) -> Outcome:
        """Make sure the export has its record: find it, or add the file and make it."""
        config = self.config
        sha256 = self.journal.sha256(path)
        entry = self.journal.export(sha256)
        if entry.get("record"):
            return "skipped"
        existing = self._existing(sha256)
        if existing is not None:
            entry["record"] = existing
            log.info("%s is already %s.", path.name, existing)
            return "found"

        file = self._file(path, sha256, entry)
        if file is None:
            return "waiting"
        scan = file.get("scan") or {"state": "clean", "usable": True}
        if not scan["usable"]:
            if scan["state"] == "pending":
                log.info("%s is waiting for its malware scan.", path.name)
                return "waiting"
            entry["held"] = {"state": scan["state"], "reason": scan.get("reason", "")}
            log.error(
                "%s (file %s) is held by the malware scan: %s %s. A Quarantine Admin can "
                "review the finding in Iki. No record was made.",
                path.name,
                file["id"],
                scan["state"],
                scan.get("reason", ""),
            )
            return "held"
        entry.pop("held", None)
        values = {**config.values, config.file_field: file["id"], config.key_field: sha256}
        reason = f"Sent by the uploader on {platform.node() or 'the instrument PC'} from {path}"

        def create(client: iki.Client) -> Any:  # noqa: ANN401
            return client.entities.create(
                config.record_type, values, project=config.project, reason=reason
            )

        try:
            made = self._call(f"Making {path.name}'s record", create)
        except iki.Invalid:
            # Another uploader made it at the same moment: the key is taken.
            existing = self._existing(sha256)
            if existing is None:
                raise
            entry["record"] = existing
            return "found"
        entry["record"] = made["hrid"]
        log.info("%s is %s.", path.name, made["hrid"])
        return "recorded"

    def _existing(self, sha256: str) -> str | None:
        def lookup(client: iki.Client) -> Any:  # noqa: ANN401
            return client.entities.lookup(
                self.config.record_type, self.config.key_field, sha256, project=self.config.project
            ).first()

        found = self._call("Looking for the export's record", lookup)
        return str(found["hrid"]) if found else None

    def _file(self, path: Path, sha256: str, entry: dict[str, Any]) -> dict[str, Any] | None:
        """The export's file in Iki once it's checked and scanned, or None to try again
        next time."""
        if entry.get("file"):
            known = self._call("Reading the file", lambda c: c.files.get(entry["file"]))
            if known["sha256"] != sha256:
                raise ValueError(f"File {entry['file']} in the journal isn't {path.name}.")
            return dict(known)
        size = path.stat().st_size
        register = self.config.mode == "register" or (
            self.config.mode == "auto" and size > self.config.register_over
        )
        try:
            if entry.get("pending"):
                # Sent before: Iki was still checking it. Wait for that, never send it again.
                pending = entry["pending"]
                file = dict(
                    self._call(
                        f"Waiting for {path.name}",
                        lambda c: c.files.wait(pending, timeout=self.config.scan_timeout),
                    )
                )
            else:
                file = self._register(path, sha256, size) if register else self._upload(path)
        except iki.TimedOut as error:
            if error.pending:
                entry["pending"] = error.pending
                self.journal.save()
            log.info("Iki is still checking %s; it'll be looked at next time.", path.name)
            return None
        except (iki.NotFound, iki.IkiError) as error:
            if isinstance(error, iki.ApiError) and not isinstance(error, iki.NotFound):
                raise
            # Its check failed (the bytes didn't match, say), or the upload is gone: send
            # it afresh next time.
            entry.pop("pending", None)
            raise
        entry.pop("pending", None)
        if file["sha256"] != sha256:
            raise ValueError(
                f"Iki read a different SHA-256 for {path.name} ({file['sha256']}): the "
                "file changed while it was sent, or the bucket's object isn't this file."
            )
        entry["file"] = file["id"]
        self.journal.save()
        return file

    def _upload(self, path: Path) -> dict[str, Any]:
        def upload(client: iki.Client) -> Any:  # noqa: ANN401
            return client.files.upload(
                path,
                project=self.config.project,
                media_type=self.config.media_type or None,
                timeout=self.config.scan_timeout,
            )

        log.info("Uploading %s.", path.name)
        return dict(self._call(f"Uploading {path.name}", upload))

    def _register(self, path: Path, sha256: str, size: int) -> dict[str, Any]:
        if self._bucket is None:
            self._bucket = bucket_for(self.config)
        bucket, root, prefix = self._bucket
        # The key is the content's, so the same export always goes to the same object
        # and nothing else is ever written over it.
        key = f"{prefix}{sha256}/{path.name}"
        log.info("Writing %s to %s%s.", path.name, root, key)
        version = bucket.put(path, key, size)

        def register(client: iki.Client) -> Any:  # noqa: ANN401
            return client.files.register(
                root + key,
                project=self.config.project,
                version=version,
                name=path.name,
                media_type=self.config.media_type or None,
                timeout=self.config.scan_timeout,
            )

        return dict(self._call(f"Registering {path.name}", register))


def run(uploader: Uploader, paths: list[Path] | None = None) -> int:
    outcomes: dict[Outcome, int] = {}
    for path in uploader.pending(paths):
        try:
            outcome = uploader.send(path)
        except (iki.IkiError, httpx2.HTTPError, OSError, ValueError) as error:
            log.error("%s wasn't sent: %s", path.name, error)
            outcome = "failed"
        finally:
            uploader.journal.save()
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    log.info(
        "Done: %s.", ", ".join(f"{n} {o}" for o, n in sorted(outcomes.items())) or "nothing new"
    )
    return 1 if outcomes.get("failed") or outcomes.get("held") else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("files", type=Path, nargs="*")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = Config.load(args.config)
        journal = Journal(config.journal)
    except ConfigError as error:
        log.error("%s", error)
        return 2
    with only_one(config.journal) as alone:
        if not alone:
            log.info("Another uploader is sending these exports; leaving them to it.")
            return 0
        session = Session(config)
        try:
            return run(Uploader(config, session, journal), args.files or None)
        except ConfigError as error:
            log.error("%s", error)
            return 2
        finally:
            session.close()


if __name__ == "__main__":
    sys.exit(main())
