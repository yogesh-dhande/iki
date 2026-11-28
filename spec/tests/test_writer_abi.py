"""The writer ABIs this Iki runs (ADR 0230): a range, from the oldest its LTS line supports
to the newest, each with its own encoder, so a module keeps getting the requests it was
built for.

A module of an ABI below the range or above it doesn't start, saying which way it's out.
Each ABI's requests are fixed: the bytes ABI 1 gets for a record at a revision are pinned
here, so a new request field can't change them, and has to be a new ABI. Adding one is an
encoder and its test; the last test here adds one, as the next ABI will be added, and checks
that both ABIs' modules then run, each given its own requests."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import wasmtime
import writer_modules as wat
from writer_modules import compiled

from iki_spec import writer_sandbox
from iki_spec.writers import abi
from iki_spec.writers.abi import Record
from iki_spec.writers.protocol import Session, Unavailable, canonical
from iki_spec.writers.runtime import Instance, WriterFailed, engine

ENGINE = engine()
MiB = 1024 * 1024
RECORD = Record(
    id=uuid.UUID("6f1c1b7e-2a6d-4c1e-9b0a-3f6f5f9b2d41"),
    hrid="TRF-000042",
    name="Dose-response, compound 12",
    type="acme/liquid_handling.Transfer",
    revision=3,
    revised_at=datetime(2026, 11, 20, 9, 14, 3, 250_000, tzinfo=UTC),
    roles={"source_well": "A1", "volume": {"unit": "uL", "value": 2.5}},
    withheld=("destination_plate",),
)
EXPORT = abi.Export(writer="acme/liquid_handling.tecan-worklist", format="acme/x.gwl", records=96)


def instance(source: str) -> Instance:
    return Instance(wasmtime.Module(ENGINE, compiled(source)), ENGINE, 16 * MiB)


def test_every_abi_in_the_range_has_its_encoder() -> None:
    assert abi.OLDEST <= abi.NEWEST
    assert sorted(abi.ENCODERS) == list(range(abi.OLDEST, abi.NEWEST + 1))
    assert all(encoder.abi == number for number, encoder in abi.ENCODERS.items())


def test_abi_1s_requests_are_the_bytes_they_always_were() -> None:
    one = abi.encoder(1)
    assert canonical(one.begin(EXPORT)) == (
        b'{"abi":1,"format":"acme/x.gwl","records":96,'
        b'"writer":"acme/liquid_handling.tecan-worklist"}'
    )
    assert canonical(one.record(RECORD)) == (
        b'{"hrid":"TRF-000042","id":"6f1c1b7e-2a6d-4c1e-9b0a-3f6f5f9b2d41",'
        b'"name":"Dose-response, compound 12","revised_at":"2026-11-20T09:14:03Z",'
        b'"revision":3,"roles":{"source_well":"A1","volume":{"unit":"uL","value":2.5}},'
        b'"type":"acme/liquid_handling.Transfer","withheld":["destination_plate"]}'
    )
    assert canonical(one.end()) == b"{}"


@pytest.mark.parametrize("number", range(abi.OLDEST, abi.NEWEST + 1))
def test_a_module_of_each_abi_in_the_range_starts_and_says_which(number: int) -> None:
    started = instance(wat.reading(number))
    assert started.abi == number
    assert started.call("record", canonical(abi.encoder(number).record(RECORD))).status == 0


def test_a_module_of_an_abi_older_than_the_range_doesnt_start() -> None:
    with pytest.raises(WriterFailed) as failed:
        instance(wat.ABI_0)
    assert failed.value.code == "abi"
    assert "reads ABI 0, which this Iki no longer runs" in str(failed.value)
    assert "newer iki-writer" in str(failed.value)
    with pytest.raises(ValueError, match="no longer runs"):
        abi.encoder(abi.OLDEST - 1)


def test_a_module_of_an_abi_newer_than_the_range_doesnt_start() -> None:
    with pytest.raises(WriterFailed) as failed:
        instance(wat.ABI_2)
    assert failed.value.code == "abi"
    assert f"reads ABI {abi.NEWEST + 1}, newer than this Iki runs" in str(failed.value)
    assert "needs a newer Iki" in str(failed.value)
    with pytest.raises(ValueError, match="newer than this Iki runs"):
        abi.encoder(abi.NEWEST + 1)


def test_the_sandbox_says_the_abi_its_module_reads(tmp_path: Path) -> None:
    module = tmp_path / "echo.wasm"
    module.write_bytes(compiled(wat.ECHO))
    started = writer_sandbox.start_child(None)
    assert started.stdin is not None and started.stdout is not None
    talk = Session(started.stdout, started.stdin)
    header = {
        "writer": "acme/x.echo",
        "sha256": hashlib.sha256(module.read_bytes()).hexdigest(),
        "memory": 16 * MiB,
        "deadline": 30,
        "path": str(module),
    }
    try:
        assert talk.open(header) == 1
    finally:
        started.stdin.close()
        started.wait(timeout=10)
        started.stdout.close()


def test_a_sandbox_that_doesnt_say_the_abi_is_unavailable() -> None:
    class Quiet:
        def __init__(self, answer: bytes) -> None:
            self.answer, self.sent = answer, b""

        def write(self, data: bytes) -> None:
            self.sent += data

        def flush(self) -> None: ...

        def read(self, n: int) -> bytes:
            out, self.answer = self.answer[:n], self.answer[n:]
            return out

    ready = canonical({"ready": True})
    stream: Any = Quiet(len(ready).to_bytes(4, "big") + ready)
    with pytest.raises(Unavailable, match="didn't say the ABI"):
        Session(stream, stream).open({})


def test_the_next_abi_is_an_encoder_and_both_abis_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """ABI 2, as it will be added: a request field ABI 1 doesn't have. Its modules start,
    ABI 1's still do, and each is given its own requests: ABI 1's bytes don't change."""

    class Abi2(abi.Abi1):
        abi = 2

        def record(self, record: Record) -> dict[str, Any]:
            return {**super().record(record), "project": "Kinase panel"}

    before = canonical(abi.encoder(1).record(RECORD))
    monkeypatch.setitem(abi.ENCODERS, 2, Abi2())
    monkeypatch.setattr(abi, "NEWEST", 2)
    test_every_abi_in_the_range_has_its_encoder()

    old, new = instance(wat.reading(1)), instance(wat.reading(2))
    one, two = abi.encoder(old.abi), abi.encoder(new.abi)
    assert canonical(one.record(RECORD)) == before
    assert canonical(one.begin(EXPORT)).startswith(b'{"abi":1,')
    assert canonical(two.begin(EXPORT)).startswith(b'{"abi":2,')
    # The echo writes back what it was given: each module its own ABI's request.
    assert old.call("record", canonical(one.record(RECORD))).output == b"> " + before + b"\n"
    given = new.call("record", canonical(two.record(RECORD))).output
    assert b'"project":"Kinase panel"' in given and b'"project"' not in before
    with pytest.raises(WriterFailed, match="newer than this Iki runs"):
        instance(wat.reading(3))
