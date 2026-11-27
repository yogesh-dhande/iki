"""The instrument PC's uploader, against a fake Iki and bucket: what it sends, what it
waits for, and that sending an export again never makes a second record.

Iki's own tests run the same uploader against the real API (server/tests/test_instrument_intake.py).
"""

import hashlib
import importlib.util
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx2
import pytest

import iki

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location(
    "iki_uploader", HERE.parent / "uploader" / "iki_uploader.py"
)
assert spec is not None and spec.loader is not None
uploader = importlib.util.module_from_spec(spec)
sys.modules["iki_uploader"] = uploader
spec.loader.exec_module(uploader)

EXPORT = HERE / "data" / "qs7-flex-3utr-standard-curve.txt"
TYPE = "acme/instrument_intake.QPCRRun"
CLEAN = {"state": "clean", "usable": True, "reason": ""}


class Found(list[Any]):
    """Matches as :class:`iki.Results` gives them."""

    def first(self) -> Any:
        return self[0] if self else None


class FakeIki:
    """The parts of iki.Client the uploader uses, keeping what's in the project."""

    def __init__(self) -> None:
        self.files_by_id: dict[str, dict[str, Any]] = {}
        self.records: list[dict[str, Any]] = []
        self.calls: list[str] = []
        self.scan: dict[str, Any] = CLEAN
        self.fail: dict[str, list[BaseException]] = {}
        self.files = self
        self.entities = self

    def _maybe_fail(self, name: str) -> None:
        self.calls.append(name)
        if self.fail.get(name):
            raise self.fail[name].pop(0)

    def _add(self, name: str, sha256: str, external: bool) -> dict[str, Any]:
        file = {
            "id": f"file-{len(self.files_by_id) + 1}",
            "name": name,
            "sha256": sha256,
            "external": external,
            "scan": dict(self.scan),
        }
        self.files_by_id[file["id"]] = file
        return file

    # files
    def upload(self, path: Path, *, project: str, media_type: str | None, timeout: float) -> Any:
        self._maybe_fail("upload")
        return self._add(path.name, hashlib.sha256(path.read_bytes()).hexdigest(), False)

    def register(self, uri: str, *, project: str, version: str, **rest: Any) -> Any:
        self._maybe_fail("register")
        self.registered = (uri, version, rest)
        return self._add(rest["name"], self.bucket_sha256, True)

    def wait(self, pending: dict[str, str], *, timeout: float) -> Any:
        self._maybe_fail("wait")
        assert list(pending) == ["upload"]
        return self._add(EXPORT.name, hashlib.sha256(EXPORT.read_bytes()).hexdigest(), False)

    def get(self, file_id: str) -> Any:
        self._maybe_fail("get")
        return dict(self.files_by_id[file_id], scan=dict(self.scan))

    # entities
    def lookup(self, type: str, field: str, value: str, *, project: str) -> Any:
        self._maybe_fail("lookup")
        return Found([r for r in self.records if r["values"][field] == value])

    def create(self, type: str, values: dict[str, Any], *, project: str, reason: str) -> Any:
        self._maybe_fail("create")
        if any(r["values"]["source_sha256"] == values["source_sha256"] for r in self.records):
            raise iki.Invalid(422, "Invalid.", code="validation_failed")
        record = {"hrid": f"QPCR-{len(self.records) + 1:06d}", "values": values, "reason": reason}
        self.records.append(record)
        return record


class FakeSession:
    def __init__(self, fake: FakeIki) -> None:
        self.fake = fake
        self.renewed = 0

    def client(self) -> FakeIki:
        return self.fake

    def renew(self) -> None:
        self.renewed += 1


class FakeBucket:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[Path, int]] = {}

    def put(self, path: Path, key: str, size: int) -> str:
        self.objects.setdefault(key, (path, size))
        return "v1"


def config(tmp_path: Path, **more: Any) -> Any:
    return uploader.Config(
        url="https://iki.example.org",
        project="qpcr-core",
        client_id="client",
        client_secret="secret",
        record_type=TYPE,
        file_field="results_file",
        key_field="source_sha256",
        values={"instrument": "QuantStudio 7 Flex, bay 3"},
        folder=tmp_path / "exports",
        patterns=("*.txt",),
        journal=tmp_path / "journal.json",
        **more,
    )


@pytest.fixture
def exports(tmp_path: Path) -> Path:
    folder = tmp_path / "exports"
    folder.mkdir()
    copy = folder / "01OCT2024 3UTR.txt"
    copy.write_bytes(EXPORT.read_bytes())
    old = time.time() - 600
    os.utime(copy, (old, old))
    return copy


def make(tmp_path: Path, fake: FakeIki, **more: Any) -> Any:
    settings = config(tmp_path, **more)
    pauses: list[float] = []
    made = uploader.Uploader(
        settings,
        FakeSession(fake),
        uploader.Journal(settings.journal),
        bucket=(FakeBucket(), "s3://acme-qpcr-core/", "bay-3/"),
        sleep=pauses.append,
    )
    made.pauses = pauses
    return made


def test_an_export_is_uploaded_then_its_record_made(tmp_path: Path, exports: Path) -> None:
    fake = FakeIki()
    assert uploader.run(make(tmp_path, fake)) == 0
    assert fake.calls == ["lookup", "upload", "create"]
    (record,) = fake.records
    assert record["values"] == {
        "instrument": "QuantStudio 7 Flex, bay 3",
        "results_file": "file-1",
        "source_sha256": hashlib.sha256(EXPORT.read_bytes()).hexdigest(),
    }
    assert str(exports) in record["reason"]


def test_sending_again_never_makes_a_second_record(tmp_path: Path, exports: Path) -> None:
    fake = FakeIki()
    uploader.run(make(tmp_path, fake))
    fake.calls.clear()
    # The journal says it's done: nothing is asked of Iki.
    assert uploader.run(make(tmp_path, fake)) == 0
    assert fake.calls == []
    # Without the journal (a new PC, say), the key finds the record: no upload, no record.
    (tmp_path / "journal.json").unlink()
    assert uploader.run(make(tmp_path, fake)) == 0
    assert fake.calls == ["lookup"]
    assert len(fake.records) == 1 and len(fake.files_by_id) == 1


def test_a_record_made_at_the_same_moment_elsewhere_is_found(tmp_path: Path, exports: Path) -> None:
    fake = FakeIki()
    sent = make(tmp_path, fake)
    sha256 = sent.journal.sha256(exports)
    original = fake.create

    def create(type: str, values: dict[str, Any], **rest: Any) -> Any:
        # Another PC sends the same export between this one's lookup and its create.
        original(type, {**values, "results_file": "file-elsewhere"}, **rest)
        return original(type, values, **rest)

    fake.create = create  # type: ignore[method-assign]
    assert sent.send(exports) == "found"
    assert [r["values"]["results_file"] for r in fake.records] == ["file-elsewhere"]
    assert sent.journal.export(sha256)["record"] == "QPCR-000001"


def test_a_held_file_gets_no_record_until_a_quarantine_admin_releases_it(
    tmp_path: Path, exports: Path
) -> None:
    fake = FakeIki()
    fake.scan = {"state": "infected", "usable": False, "reason": "Win.Test.EICAR_HDB-1"}
    assert uploader.run(make(tmp_path, fake)) == 1
    assert fake.records == []
    # Each later pass looks at the same file again, and uploads nothing more.
    fake.calls.clear()
    assert uploader.run(make(tmp_path, fake)) == 1
    assert fake.calls == ["lookup", "get"]
    fake.scan = {"state": "infected", "usable": True, "released": True, "reason": "False positive"}
    assert uploader.run(make(tmp_path, fake)) == 0
    assert len(fake.records) == 1 and len(fake.files_by_id) == 1


def test_a_scan_still_going_is_waited_for_next_time(tmp_path: Path, exports: Path) -> None:
    fake = FakeIki()
    fake.fail["upload"] = [iki.TimedOut("Iki is still scanning it.", pending={"upload": "u1"})]
    assert uploader.run(make(tmp_path, fake)) == 0
    assert fake.records == []
    fake.fail["wait"] = [iki.TimedOut("Still.", pending={"upload": "u1"})]
    assert uploader.run(make(tmp_path, fake)) == 0
    assert fake.records == []
    # The next pass waits for the same upload: the file is never sent twice.
    assert uploader.run(make(tmp_path, fake)) == 0
    assert fake.calls.count("upload") == 1 and fake.calls.count("wait") == 2
    assert len(fake.records) == 1 and len(fake.files_by_id) == 1


@pytest.mark.parametrize(
    "ended",
    [
        iki.IkiError("The bytes that arrived don't match the SHA-256 you sent."),
        iki.NotFound(404, "No such upload."),
    ],
    ids=["its check failed", "it is gone"],
)
def test_a_waited_for_upload_that_ended_is_sent_afresh(
    tmp_path: Path, exports: Path, ended: BaseException
) -> None:
    fake = FakeIki()
    fake.fail["upload"] = [iki.TimedOut("Still scanning.", pending={"upload": "u1"})]
    fake.fail["wait"] = [ended]
    assert uploader.run(make(tmp_path, fake)) == 0
    assert uploader.run(make(tmp_path, fake)) == 1
    assert uploader.run(make(tmp_path, fake)) == 0
    assert fake.calls.count("upload") == 2 and len(fake.records) == 1


def test_a_pending_scan_of_a_journaled_file_waits(tmp_path: Path, exports: Path) -> None:
    fake = FakeIki()
    fake.scan = {"state": "pending", "usable": False, "reason": ""}
    assert uploader.run(make(tmp_path, fake)) == 0
    assert uploader.run(make(tmp_path, fake)) == 0
    assert fake.records == [] and len(fake.files_by_id) == 1
    fake.scan = CLEAN
    assert uploader.run(make(tmp_path, fake)) == 0
    assert len(fake.records) == 1


def test_large_files_are_written_to_the_bucket_and_registered(
    tmp_path: Path, exports: Path
) -> None:
    import hashlib

    fake = FakeIki()
    sha256 = hashlib.sha256(EXPORT.read_bytes()).hexdigest()
    fake.bucket_sha256 = sha256
    sent = make(tmp_path, fake, mode="auto", register_over=1024)
    assert uploader.run(sent) == 0
    assert "upload" not in fake.calls
    uri, version, rest = fake.registered
    assert uri == f"s3://acme-qpcr-core/bay-3/{sha256}/{exports.name}"
    assert (version, rest["name"]) == ("v1", exports.name)
    assert fake.records[0]["values"]["results_file"] == "file-1"


def test_small_files_are_uploaded_in_auto_mode(tmp_path: Path, exports: Path) -> None:
    fake = FakeIki()
    assert uploader.run(make(tmp_path, fake, mode="auto", register_over=1024**2)) == 0
    assert "upload" in fake.calls and "register" not in fake.calls


def test_a_bucket_object_that_is_not_the_export_is_refused(tmp_path: Path, exports: Path) -> None:
    fake = FakeIki()
    fake.bucket_sha256 = "0" * 64
    assert uploader.run(make(tmp_path, fake, mode="register")) == 1
    assert fake.records == []


def test_failures_for_now_are_tried_again(tmp_path: Path, exports: Path) -> None:
    fake = FakeIki()
    request = httpx2.Request("POST", "https://iki.example.org/api/v1/entities")
    fake.fail["create"] = [
        httpx2.ConnectError("Network unreachable", request=request),
        iki.ApiError(503, "Iki is restarting."),
    ]
    fake.fail["lookup"] = [iki.NotSignedIn("expired")]
    sent = make(tmp_path, fake)
    assert uploader.run(sent) == 0
    assert len(fake.records) == 1
    assert sent.pauses == [2.0, 4.0]
    assert sent.session.renewed == 1


def test_a_refusal_is_not_tried_again(tmp_path: Path, exports: Path) -> None:
    fake = FakeIki()
    fake.fail["create"] = [iki.Forbidden(403, "You can't edit qpcr-core.", code="forbidden")]
    sent = make(tmp_path, fake)
    assert uploader.run(sent) == 1
    assert fake.calls.count("create") == 1 and sent.pauses == []


def test_an_export_still_being_written_waits(tmp_path: Path, exports: Path) -> None:
    exports.touch()
    fake = FakeIki()
    assert uploader.run(make(tmp_path, fake)) == 0
    assert fake.calls == []


def test_the_configuration_keeps_the_secret_elsewhere(tmp_path: Path) -> None:
    path = tmp_path / "uploader.toml"
    base = """
[iki]
url = "https://iki.example.org"
project = "qpcr-core"
client_id = "abc"
{secret}
[record]
type = "acme/instrument_intake.QPCRRun"
file_field = "results_file"
key_field = "source_sha256"
[record.values]
instrument = "QuantStudio 7 Flex, bay 3"
[files]
folder = "exports"
{files}
"""
    path.write_text(base.format(secret='client_secret = "s3cret"', files=""))
    with pytest.raises(uploader.ConfigError, match="Keep the secret out"):
        uploader.Config.load(path, {})
    path.write_text(base.format(secret="", files=""))
    with pytest.raises(uploader.ConfigError, match="Set IKI_UPLOADER_SECRET"):
        uploader.Config.load(path, {})
    loaded = uploader.Config.load(path, {"IKI_UPLOADER_SECRET": "s3cret"})
    assert (loaded.client_secret, loaded.mode, loaded.folder) == (
        "s3cret",
        "upload",
        tmp_path / "exports",
    )
    path.write_text(base.format(secret="", files='mode = "register"'))
    with pytest.raises(uploader.ConfigError, match=r"needs \[bucket\] uri"):
        uploader.Config.load(path, {"IKI_UPLOADER_SECRET": "s3cret"})


def test_the_example_configuration_loads() -> None:
    example = HERE.parent / "uploader" / "uploader.example.toml"
    loaded = uploader.Config.load(example, {"IKI_UPLOADER_SECRET": "s3cret"})
    assert loaded.record_type == TYPE
    assert loaded.patterns == ("*.txt",) and loaded.settle == 60


def test_only_one_uploader_uses_a_journal(tmp_path: Path) -> None:
    journal = tmp_path / "journal.json"
    with uploader.only_one(journal) as first, uploader.only_one(journal) as second:
        assert (first, second) == (True, False)
    with uploader.only_one(journal) as again:
        assert again


class FakeS3:
    class exceptions:  # noqa: N801 - as boto3 names it
        class ClientError(Exception):
            def __init__(self, code: str) -> None:
                self.response = {"Error": {"Code": code}}

    def __init__(self, versioned: bool = True) -> None:
        self.versioned = versioned
        self.objects: dict[str, int] = {}
        self.uploads = 0

    def head_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        if Key not in self.objects:
            raise self.exceptions.ClientError("404")
        found: dict[str, Any] = {"ContentLength": self.objects[Key]}
        if self.versioned:
            found["VersionId"] = f"version-{self.uploads}"
        return found

    def upload_file(self, filename: str, bucket: str, key: str) -> None:
        self.objects[key] = Path(filename).stat().st_size
        self.uploads += 1


def test_s3_writes_once_and_needs_versions() -> None:
    s3 = FakeS3()
    bucket = uploader.S3Bucket("acme-qpcr-core", client=s3)
    size = EXPORT.stat().st_size
    assert bucket.put(EXPORT, "bay-3/key", size) == "version-1"
    assert bucket.put(EXPORT, "bay-3/key", size) == "version-1"
    assert s3.uploads == 1
    unversioned = uploader.S3Bucket("lab-scratch", client=FakeS3(versioned=False))
    with pytest.raises(uploader.ConfigError, match="doesn't keep versions"):
        unversioned.put(EXPORT, "key", size)
