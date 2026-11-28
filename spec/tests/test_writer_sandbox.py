"""Plugins' writers in the sandbox (ADR 0230): the checker, Wasmtime's limits, the child
process that runs one export, the launcher that hands out children, and the image's
install step.

The limits are what stop a writer that's wrong, slow or hostile: fuel stops one that never
ends after exactly its budget, the memory cap one that hoards, the small stack one that
recurses, and the checker one that imports anything or carries the inputs of Wasmtime's
Critical advisories. A writer is deterministic: the same module and records make the same
bytes, using the same fuel, in any process."""

from __future__ import annotations

import contextlib
import hashlib
import json
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import IO, Any

import pytest
import wasmtime
import writer_modules as wat
from writer_modules import compiled

from iki_spec import writer_sandbox
from iki_spec.writers import BUILD, child, install
from iki_spec.writers import build as writer_build
from iki_spec.writers.check import (
    MAX_BR_TABLE_TARGETS,
    MAX_FUNCTION_BODY,
    MAX_MODULE_BYTES,
    check_module,
)
from iki_spec.writers.protocol import (
    Session,
    SessionFailed,
    Unavailable,
    canonical,
    read_json,
    write_json,
)
from iki_spec.writers.runtime import (
    FUEL_BASE,
    FUEL_PER_BYTE,
    MAX_MEMORY,
    Instance,
    Limits,
    WriterFailed,
    engine,
)

MiB = 1024 * 1024
ENGINE = engine()
RECORD = canonical({"hrid": "TRF-000001", "roles": {"volume": {"unit": "uL", "value": 2.5}}})
ROOT = Path(__file__).resolve().parents[2]
TECAN = ROOT / "examples" / "plugins" / "liquid_handling" / "writers" / "tecan-worklist.wasm"


def instance(source: str, memory: int = 16 * MiB) -> Instance:
    return Instance(wasmtime.Module(ENGINE, compiled(source)), ENGINE, memory)


def codes(binary: bytes) -> list[str]:
    return [p.code for p in check_module(binary)]


# --- The checker ---------------------------------------------------------------------------------


def test_a_writer_passes_the_checker() -> None:
    assert check_module(compiled(wat.ECHO)) == []


def test_any_import_is_refused_before_anything_runs() -> None:
    assert "imports" in codes(compiled(wat.IMPORTS_WASI))
    with pytest.raises(WriterFailed) as failed:
        instance(wat.IMPORTS_WASI)
    assert failed.value.code == "abi"


def test_a_module_missing_an_export_of_the_abi_is_refused() -> None:
    assert codes(compiled(wat.INCOMPLETE)) == ["abi"]


def test_a_second_memory_or_a_shared_one_is_refused() -> None:
    two = wat.recording("(i32.const 0)", rest=wat.REST.replace("(memory", "(memory 1) (memory", 1))
    assert "memory" in codes(compiled(two))
    shared = wat.recording(
        "(i32.const 0)",
        rest=wat.REST.replace(
            '(memory (export "memory") 1)', '(memory (export "memory") 1 1 shared)'
        ),
    )
    assert "memory" in codes(compiled(shared))


def test_the_inputs_of_wasmtimes_critical_advisories_are_refused() -> None:
    # A function body over 1 MiB: half a million `nop`s and a little.
    body = "(i32.const 0) " + "nop " * (MAX_FUNCTION_BODY + 16)
    assert "function_too_large" in codes(compiled(wat.recording(body)))
    # A br_table of more targets than the cap.
    targets = " ".join(["0"] * (MAX_BR_TABLE_TARGETS + 1))
    jumps = wat.recording(f"(block (br_table {targets} 0 (i32.const 0))) (i32.const 0)")
    assert "br_table_too_large" in codes(compiled(jumps))
    assert codes(b"\0asm\1\0\0\0" + b"\0" * MAX_MODULE_BYTES) == ["too_large"]


def test_the_checker_reads_instructions_not_bytes() -> None:
    # 0xfd, SIMD's prefix, inside an immediate is a number, not an instruction.
    assert codes(compiled(wat.recording("(drop (i32.const 253)) (i32.const 0)"))) == []
    simd = wat.recording("(drop (v128.const i32x4 0 0 0 0)) (i32.const 0)")
    assert "proposal" in codes(compiled(simd))


def test_what_isnt_webassembly_is_refused() -> None:
    assert codes(b"#!/bin/sh\n") == ["not_wasm"]
    assert "malformed" in codes(compiled(wat.ECHO)[:40])


# --- Wasmtime's limits ---------------------------------------------------------------------------


def test_a_writer_writes_bytes_and_notes() -> None:
    writer = instance(wat.ECHO)
    assert writer.call("begin", b"{}").output == b""
    done = writer.call("record", RECORD)
    assert (done.status, done.output) == (0, b"> " + RECORD + b"\n")
    assert done.notes == [wat.NOTE]


def test_a_refusal_is_one_note_and_no_bytes() -> None:
    done = instance(wat.REFUSES).call("record", RECORD)
    assert (done.status, done.output) == (1, b"")
    assert done.notes == [{"code": "refused", "message": "Not this one."}]


def test_a_writer_that_never_stops_runs_out_of_fuel_after_exactly_its_budget() -> None:
    writer = instance(wat.SPINS)
    started = time.monotonic()
    with pytest.raises(WriterFailed) as failed:
        writer.call("record", RECORD)
    assert failed.value.code == "out_of_fuel"
    assert writer.store.get_fuel() == 0
    # A billion and a little: seconds, not the deadline's half hour.
    assert time.monotonic() - started < 30
    assert FUEL_BASE + FUEL_PER_BYTE * len(RECORD) > FUEL_BASE


def test_memory_stops_growing_at_its_cap_and_the_writer_traps() -> None:
    writer = instance(wat.HOARDS, memory=8 * MiB)
    with pytest.raises(WriterFailed) as failed:
        writer.call("record", RECORD)
    assert failed.value.code == "panicked"
    assert writer.memory.data_len(writer.store) == 8 * MiB


def test_a_module_declaring_more_memory_than_its_cap_doesnt_start() -> None:
    with pytest.raises(WriterFailed) as failed:
        instance(wat.TOO_BIG, memory=64 * MiB)
    assert failed.value.code == "instantiate"
    with pytest.raises(WriterFailed) as failed:
        instance(wat.ECHO, memory=MAX_MEMORY + 1)
    assert failed.value.code == "limits"


def test_runaway_recursion_is_a_trap_not_a_crash() -> None:
    with pytest.raises(WriterFailed) as failed:
        instance(wat.RECURSES).call("record", RECORD)
    assert failed.value.code == "stack_overflow"


def test_bulk_memory_costs_fuel_for_each_byte() -> None:
    # One instruction over 48 MiB costs far more than one instruction's fuel.
    done = instance(wat.FILLS, memory=64 * MiB).call("record", RECORD)
    assert done.fuel > 48 * MiB // 8


def test_output_beyond_its_memory_is_refused() -> None:
    with pytest.raises(WriterFailed) as failed:
        instance(wat.OVERREACHES).call("record", RECORD)
    assert failed.value.code == "abi"


def test_a_request_over_its_limit_is_refused_before_its_written() -> None:
    writer = instance(wat.ECHO)
    with pytest.raises(WriterFailed) as failed:
        writer.call("record", b" " * (64 * MiB + 1))
    assert failed.value.code == "request_limit"


def test_a_deployment_may_lower_a_writers_limits_or_raise_its_fuel() -> None:
    """ADR 0230: a deployment's limits, within Iki's: lowered, a writer meets them sooner;
    fuel raised, a writer gets more of it; any other limit raised is refused."""
    module = wasmtime.Module(ENGINE, compiled(wat.ECHO))
    lowered = Instance(module, ENGINE, 16 * MiB, Limits(max_output=len(RECORD) + 2))
    with pytest.raises(WriterFailed) as failed:
        lowered.call("record", RECORD)
    assert failed.value.code == "output_limit"
    assert f"{len(RECORD) + 2:,} bytes" in str(failed.value)
    with pytest.raises(WriterFailed) as failed:
        Instance(module, ENGINE, 16 * MiB, Limits(max_request=10)).call("record", RECORD)
    assert failed.value.code == "request_limit"
    with pytest.raises(WriterFailed) as failed:
        Instance(module, ENGINE, 16 * MiB, Limits(max_note_chars=10)).call("record", RECORD)
    assert failed.value.code == "notes"
    # One memory.fill over 48 MiB costs millions of fuel: within Iki's, not within less.
    fills = wasmtime.Module(ENGINE, compiled(wat.FILLS))
    needs = Instance(fills, ENGINE, 64 * MiB).call("record", RECORD).fuel
    short = Instance(fills, ENGINE, 64 * MiB, Limits(fuel_base=needs // 2, fuel_per_byte=0))
    with pytest.raises(WriterFailed) as failed:
        short.call("record", RECORD)
    assert failed.value.code == "out_of_fuel"
    raised = Limits(fuel_base=FUEL_BASE * 4, fuel_per_byte=FUEL_PER_BYTE * 4)
    assert Instance(fills, ENGINE, 64 * MiB, raised).call("record", RECORD).fuel == needs
    for too_much in (
        {"max_output": 64 * MiB + 1},
        {"max_request": 64 * MiB + 1},
        {"max_notes": 1_001},
        {"max_note_chars": 501},
        {"fuel_base": 0},
        {"fuel_per_byte": -1},
    ):
        with pytest.raises(ValueError, match=r"A writer.s"):
            Limits(**too_much)
    assert Limits.of(None) == Limits()
    assert Limits.of({"fuel_base": 5}).header() == {"fuel_base": 5}
    with pytest.raises(ValueError):
        Limits.of({"wall_clock": 5})


def test_a_child_runs_a_writer_under_the_limits_its_header_gives(
    module_file: tuple[Path, str],
) -> None:
    path, sha256 = module_file
    started = writer_sandbox.start_child(None)
    assert started.stdout is not None and started.stdin is not None
    talk = Session(started.stdout, started.stdin)
    talk.open(
        {
            "writer": "acme/x.echo",
            "sha256": sha256,
            "memory": 16 * MiB,
            "deadline": 30,
            "path": str(path),
            "limits": {"max_output": 4},
        }
    )
    talk.call("begin", canonical({"abi": 1}))
    with pytest.raises(SessionFailed) as failed:
        talk.call("record", RECORD)
    assert failed.value.code == "output_limit"
    finish(started)
    # A header that raises a limit past Iki's isn't served.
    started = writer_sandbox.start_child(None)
    assert started.stdout is not None and started.stdin is not None
    talk = Session(started.stdout, started.stdin)
    with pytest.raises(Unavailable) as refused:
        talk.open(
            {
                "writer": "acme/x.echo",
                "sha256": sha256,
                "memory": 16 * MiB,
                "deadline": 30,
                "path": str(path),
                "limits": {"max_output": 65 * MiB},
            }
        )
    assert refused.value.code == "malformed"
    finish(started)


# --- One export's process ------------------------------------------------------------------------


@pytest.fixture
def module_file(tmp_path: Path) -> Iterator[tuple[Path, str]]:
    path = tmp_path / "echo.wasm"
    path.write_bytes(compiled(wat.ECHO))
    yield path, hashlib.sha256(path.read_bytes()).hexdigest()


def finish(started: Any) -> int:
    """Ends a child's export, as the launcher does, and its pipes."""
    started.stdin.close()
    try:
        return int(started.wait(timeout=10))
    finally:
        started.stdout.close()


def session(path: Path, sha256: str, memory: int = 16 * MiB) -> tuple[Session, Any]:
    started = writer_sandbox.start_child(None)
    assert started.stdout is not None and started.stdin is not None
    talk = Session(started.stdout, started.stdin)
    try:
        talk.open(
            {
                "writer": "acme/x.echo",
                "sha256": sha256,
                "memory": memory,
                "deadline": 30,
                "path": str(path),
            }
        )
    except Unavailable:
        finish(started)  # it refused, and exits
        raise
    return talk, started


def test_a_child_writes_one_export_and_exits(module_file: tuple[Path, str]) -> None:
    talk, started = session(*module_file)
    assert talk.call("begin", canonical({"abi": 1})).output == b""
    answer = talk.call("record", RECORD)
    assert answer.output == b"> " + RECORD + b"\n"
    assert answer.notes == [wat.NOTE]
    assert talk.call("end", b"{}").status == 0
    assert finish(started) == 0


def test_a_child_starts_with_nothing_in_its_environment(module_file: tuple[Path, str]) -> None:
    started = writer_sandbox.start_child(None)
    try:
        if Path(f"/proc/{started.pid}/environ").exists():
            assert Path(f"/proc/{started.pid}/environ").read_bytes() == b""
        else:
            shown = subprocess.run(  # noqa: S603 - ps, on our own child
                ["ps", "eww", "-o", "command=", "-p", str(started.pid)],  # noqa: S607
                capture_output=True,
                text=True,
                check=False,
            ).stdout
            assert "PATH=" not in shown and "HOME=" not in shown
    finally:
        started.kill()
        finish(started)


def test_a_child_refuses_a_module_that_isnt_the_one_named(module_file: tuple[Path, str]) -> None:
    path, _ = module_file
    with pytest.raises(Unavailable) as refused:
        session(path, "0" * 64)
    assert refused.value.code == "digest_mismatch"


def test_a_child_refuses_a_module_the_checker_refuses(tmp_path: Path) -> None:
    path = tmp_path / "wasi.wasm"
    path.write_bytes(compiled(wat.IMPORTS_WASI))
    with pytest.raises(Unavailable) as refused:
        session(path, hashlib.sha256(path.read_bytes()).hexdigest())
    assert refused.value.code == "rejected"


def test_a_trap_ends_the_export_saying_why(tmp_path: Path) -> None:
    path = tmp_path / "spins.wasm"
    path.write_bytes(compiled(wat.SPINS))
    talk, started = session(path, hashlib.sha256(path.read_bytes()).hexdigest())
    with pytest.raises(SessionFailed) as failed:
        talk.call("record", RECORD)
    assert failed.value.code == "out_of_fuel"
    assert finish(started) == 1


def test_each_file_starts_from_a_fresh_instance(tmp_path: Path) -> None:
    # A writer that counts its records: a ZIP's second file starts from one again.
    counts = wat.recording(
        "(global.set $n (i32.add (global.get $n) (i32.const 1)))"
        " (i32.store8 (i32.const 0) (i32.add (i32.const 48) (global.get $n))) (i32.const 0)",
        rest=wat.REST.replace(
            '(func (export "iki_output_len") (result i32) (i32.const 0))',
            '(func (export "iki_output_len") (result i32) (i32.const 1))',
        ),
        extra="(global $n (mut i32) (i32.const 0))",
    )
    path = tmp_path / "counts.wasm"
    path.write_bytes(compiled(counts))
    talk, started = session(path, hashlib.sha256(path.read_bytes()).hexdigest())
    written = []
    for _ in range(2):
        talk.call("begin", b"{}")
        written += [talk.call("record", RECORD).output, talk.call("record", RECORD).output]
        talk.call("end", b"{}")
    assert written == [b"1", b"2", b"1", b"2"]
    finish(started)


def test_the_same_records_make_the_same_bytes_and_fuel_in_two_processes(
    module_file: tuple[Path, str],
) -> None:
    def once() -> list[tuple[bytes, int]]:
        talk, started = session(*module_file)
        answers = [
            talk.call(e, r) for e, r in (("begin", b"{}"), ("record", RECORD), ("end", b"{}"))
        ]
        finish(started)
        return [(a.output, a.fuel) for a in answers]

    assert once() == once()


@pytest.mark.skipif(not TECAN.is_file(), reason="make test-wasm builds the Tecan writer")
def test_the_tecan_writer_writes_the_same_worklist_in_two_processes() -> None:
    sha256 = hashlib.sha256(TECAN.read_bytes()).hexdigest()
    plate = {
        "fields": {"label": "Compound source 1", "wells": "plate_384"},
        "hrid": "PLT-000001",
        "id": "0190e9a4-0000-7000-8000-000000000001",
        "name": "Compound source 1",
        "revision": 1,
        "type": "acme/liquid_handling.Plate",
    }
    record = canonical(
        {
            "hrid": "TRF-000001",
            "id": "0190e9a4-0000-7000-8000-000000000002",
            "latest": True,
            "name": "B3 to A1",
            "revised_at": "2026-11-25T09:00:00Z",
            "revision": 1,
            "roles": {
                "destination_plate": {**plate, "fields": {"label": "Assay", "wells": "plate_96"}},
                "destination_well": "A1",
                "source_plate": plate,
                "source_well": "B3",
                "volume": {"unit": "uL", "value": 2.5},
            },
            "type": "acme/liquid_handling.Transfer",
            "withheld": [],
        }
    )

    def once() -> bytes:
        talk, started = session(TECAN, sha256)
        talk.call("begin", canonical({"abi": 1, "format": "f", "records": 1, "writer": "w"}))
        out = talk.call("record", record).output
        finish(started)
        return out

    first = once()
    assert first == once()
    assert first.split(b"\r\n")[0] == b"A;Compound source 1;;;34;;2.5;;;;"


# --- The launcher --------------------------------------------------------------------------------


@pytest.fixture
def sandbox() -> Iterator[writer_sandbox.Sandbox]:
    box = writer_sandbox.Sandbox(("127.0.0.1", 0), modules=None, concurrency=1, max_deadline=3)
    threading.Thread(target=box.serve_forever, daemon=True).start()
    yield box
    box.shutdown()
    box.server_close()


@contextlib.contextmanager
def connected(box: writer_sandbox.Sandbox) -> Iterator[tuple[IO[bytes], IO[bytes], Session]]:
    """A connection to the launcher, its two streams and a session over them; all closed
    after."""
    host, port = box.server_address[:2]
    assert isinstance(host, str)
    with (
        writer_sandbox.connect(f"tcp://{host}:{port}", 10) as sock,
        sock.makefile("rb") as reader,
        sock.makefile("wb") as writer,
    ):
        yield reader, writer, Session(reader, writer)


def test_the_launcher_relays_one_export_to_a_fresh_child(
    sandbox: writer_sandbox.Sandbox, module_file: tuple[Path, str]
) -> None:
    path, sha256 = module_file
    with connected(sandbox) as (_, _, talk):
        talk.open(
            {
                "writer": "acme/x.echo",
                "sha256": sha256,
                "memory": MiB,
                "deadline": 3,
                "path": str(path),
            }
        )
        assert talk.call("record", RECORD).output == b"> " + RECORD + b"\n"


def test_past_its_concurrency_the_launcher_says_its_busy(
    sandbox: writer_sandbox.Sandbox, module_file: tuple[Path, str]
) -> None:
    path, sha256 = module_file
    header = {
        "writer": "acme/x.echo",
        "sha256": sha256,
        "memory": MiB,
        "deadline": 3,
        "path": str(path),
    }
    with connected(sandbox) as (_, _, talk):
        talk.open(header)
        with connected(sandbox) as (_, _, other), pytest.raises(Unavailable) as busy:
            other.open(header)
        assert busy.value.code == "busy"


def test_a_writer_past_its_deadline_is_killed_and_the_server_told(
    sandbox: writer_sandbox.Sandbox, tmp_path: Path
) -> None:
    # Fuel stops a spinning writer in seconds; a host call can't hang one, so the deadline
    # is what a slow one, many records long, meets. A sleeping export is as good.
    path = tmp_path / "echo.wasm"
    path.write_bytes(compiled(wat.ECHO))
    with connected(sandbox) as (_, _, talk):
        talk.open(
            {
                "writer": "acme/x.echo",
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "memory": MiB,
                "deadline": 1,
                "path": str(path),
            }
        )
        time.sleep(1.5)
        with pytest.raises(SessionFailed) as failed:
            talk.call("record", RECORD)
        assert failed.value.code == "deadline"


def test_a_malformed_header_is_refused(sandbox: writer_sandbox.Sandbox) -> None:
    with connected(sandbox) as (reader, writer, _):
        writer.write(b"\0\0\0\2[]")
        writer.flush()
        assert read_json(reader)["error"]["code"] == "malformed"


# --- The image's install step --------------------------------------------------------------------


def built(tmp_path: Path, source: str, *, tracked: str | None = None) -> tuple[Path, Path]:
    binary = compiled(source)
    build, made = tmp_path / "build", tmp_path / "built"
    (made / "acme__x").mkdir(parents=True)
    (made / "acme__x" / "echo.wasm").write_bytes(binary)
    build.mkdir()
    entry = {
        "plugin": "acme/x",
        "name": "echo",
        "crate": "acme__x/echo",
        "sha256": tracked or hashlib.sha256(binary).hexdigest(),
        "memory": 16 * MiB,
    }
    write = {"format": 1, "writers": {"acme/x.echo": entry}}
    (build / BUILD).write_text(json.dumps(write))
    return build, made


def test_the_image_installs_a_writer_whose_digest_is_the_tracked_one(tmp_path: Path) -> None:
    build, made = built(tmp_path, wat.ECHO)
    found = install.install(build, made, tmp_path / "out")
    entry = found["writers"]["acme/x.echo"]
    out = tmp_path / "out"
    assert hashlib.sha256((out / entry["wasm"]).read_bytes()).hexdigest() == entry["wasm_sha256"]
    assert hashlib.sha256((out / entry["cwasm"]).read_bytes()).hexdigest() == entry["cwasm_sha256"]
    assert json.loads((out / "manifest.json").read_text())["writers"] == found["writers"]


SDK_PIN = "0.1.0 " + "5" * 64


def recorded(tmp_path: Path, **changed: str) -> tuple[Path, Path]:
    """A build of a writer whose tracked digest isn't its module's, with what the digest
    was written with in its file, and the inputs of the build here, which differ by
    ``changed``: the crate's ``source``, its ``sdk``, the ``builder`` or the ``script``."""
    build, made = built(tmp_path, wat.ECHO, tracked="ab" * 32)
    crate = build / "acme__x" / "echo"
    (crate / "src").mkdir(parents=True)
    (crate / "src" / "lib.rs").write_text("// The writer.\n")
    (crate / "Cargo.lock").write_text(
        '[[package]]\nname = "iki-writer"\nversion = "0.1.0"\n'
        'source = "registry+https://github.com/rust-lang/crates.io-index"\n'
        f'checksum = "{"5" * 64}"\n'
    )
    sdk = build / "sdk" / "iki-writer"
    sdk.mkdir(parents=True)
    (sdk / "build-writer.sh").write_text("#!/bin/sh\n")
    (made / writer_build.BUILT_BY).write_text(writer_build.BUILDER + "\n")
    then = writer_build.built_with(crate, sdk=sdk)
    assert then.sdk == SDK_PIN
    then = writer_build.BuiltWith(
        changed.get("source", then.source),
        changed.get("sdk", then.sdk),
        changed.get("builder", then.builder),
        changed.get("script", then.script),
    )
    fake = SimpleNamespace(module="writers/echo")
    text = writer_build.digest_text(fake, b"", then)  # type: ignore[arg-type]
    (build / "acme__x" / "echo.wasm.sha256").write_text(("ab" * 32) + text[64:])
    return build, made


def refusal(build: Path, made: Path, out: Path) -> str:
    with pytest.raises(install.InstallError) as refused:
        install.install(build, made, out)
    assert install.main([str(build), str(made), str(out)]) == 1
    said = str(refused.value)
    module = hashlib.sha256((made / "acme__x" / "echo.wasm").read_bytes()).hexdigest()
    tracked = "ab" * 32
    assert said.startswith(
        f"acme/x.echo: the module built here is {module}, but echo.wasm.sha256 says {tracked}"
    )
    return said


def test_the_image_refuses_a_module_that_isnt_the_reviewed_one_saying_its_source_changed(
    tmp_path: Path,
) -> None:
    said = refusal(*recorded(tmp_path, source="0" * 64), tmp_path / "out")
    assert "its source (the Rust, or its Cargo.lock) isn't what the digest was written from" in said
    assert "the source isn't what was reviewed" in said
    assert "builder" not in said


def test_a_module_built_against_another_iki_writer_says_so(tmp_path: Path) -> None:
    said = refusal(*recorded(tmp_path, sdk="0.0.9 " + "4" * 64), tmp_path / "out")
    assert "its Cargo.lock pins iki-writer 0.1.0 (555555555555…)" in said
    assert "the digest was written with iki-writer 0.0.9 (444444444444…)" in said
    assert "isn't what was reviewed" not in said


def test_a_module_built_in_another_builder_says_which(tmp_path: Path) -> None:
    said = refusal(
        *recorded(tmp_path, builder="rust:1.92.0-slim@sha256:" + "1" * 64), tmp_path / "out"
    )
    assert "written with the writer builder rust:1.92.0-slim@sha256:" in said
    assert f"this build ran in {writer_build.BUILDER}" in said
    assert "--build-arg IKI_WRITER_BUILDER=rust:1.92.0-slim" in said
    assert "source" not in said


def test_a_module_built_by_another_build_script_says_so(tmp_path: Path) -> None:
    said = refusal(*recorded(tmp_path, script="2" * 64), tmp_path / "out")
    assert "another Iki's build-writer.sh" in said


def test_a_module_whose_inputs_are_all_the_same_is_a_build_that_didnt_reproduce(
    tmp_path: Path,
) -> None:
    said = refusal(*recorded(tmp_path), tmp_path / "out")
    assert "the build didn't reproduce the module, which is Iki's bug" in said


def test_a_digest_file_that_doesnt_say_what_it_was_built_with_says_that(tmp_path: Path) -> None:
    build, made = built(tmp_path, wat.ECHO, tracked="ab" * 32)
    said = refusal(build, made, tmp_path / "out")
    assert "doesn't say what the module was built with" in said


@pytest.mark.parametrize(
    "source", [wat.IMPORTS_WASI, wat.INCOMPLETE, wat.ABI_0, wat.ABI_2, wat.TOO_BIG]
)
def test_the_image_refuses_a_module_that_cant_be_a_writer(tmp_path: Path, source: str) -> None:
    build, made = built(tmp_path, source)
    with pytest.raises(install.InstallError, match=r"acme/x\.echo"):
        install.install(build, made, tmp_path / "out")


def test_the_sandbox_loads_what_the_image_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build, made = built(tmp_path, wat.ECHO)
    found = install.install(build, made, tmp_path / "out")
    sha256 = found["writers"]["acme/x.echo"]["wasm_sha256"]
    # The image's files are root's; a test's are its own.
    monkeypatch.setattr(child, "_image_owned", lambda *_, **__: None)
    module = child.load_image(ENGINE, tmp_path / "out", "acme/x.echo", sha256)
    assert Instance(module, ENGINE, MiB).call("record", RECORD).status == 0
    with pytest.raises(child.Refused) as refused:
        child.load_image(ENGINE, tmp_path / "out", "acme/x.echo", "0" * 64)
    assert refused.value.code == "digest_mismatch"
    with pytest.raises(child.Refused) as refused:
        child.load_image(ENGINE, tmp_path / "out", "acme/x.other", sha256)
    assert refused.value.code == "no_writer"
    # Code swapped in after the build is refused, and the module compiled instead.
    (tmp_path / "out" / found["writers"]["acme/x.echo"]["wasm"]).write_bytes(compiled(wat.SPINS))
    with pytest.raises(child.Refused):
        child.load_image(ENGINE, tmp_path / "out", "acme/x.echo", sha256)


def test_the_sandbox_refuses_files_that_arent_the_images(tmp_path: Path) -> None:
    build, made = built(tmp_path, wat.ECHO)
    install.install(build, made, tmp_path / "out")
    with pytest.raises(child.Refused, match="root-owned"):
        child.manifest(tmp_path / "out")


def test_the_launcher_clears_its_own_environment_and_serves(tmp_path: Path) -> None:
    # Run as the chart runs it, with the image's variables set: it re-executes itself
    # once, with none, so not even the launcher holds them; and then it serves.
    ports = []
    for _ in range(2):
        with socket.socket() as free:
            free.bind(("127.0.0.1", 0))
            ports.append(free.getsockname()[1])
    started = subprocess.Popen(  # noqa: S603 - this interpreter, Iki's own module
        [
            *(sys.executable, "-I", "-m", "iki_spec.writer_sandbox", "--host", "127.0.0.1"),
            *("--port", str(ports[0]), "--health-port", str(ports[1])),
            *("--modules", str(tmp_path / "none")),
        ],
        env={"IKI_STATIC_ROOT": "/opt/iki/static", "PATH": "/usr/bin"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{ports[1]}/healthz/live", timeout=2
                ) as live:
                    assert live.status == 200
                    break
            except OSError:
                assert started.poll() is None, "the launcher exited"
                assert time.monotonic() < deadline, "the launcher never served"
                time.sleep(0.2)
        environ = Path(f"/proc/{started.pid}/environ")
        if environ.exists():
            assert environ.read_bytes() == b""
        else:
            shown = subprocess.run(  # noqa: S603 - ps, on our own child
                ["ps", "eww", "-o", "command=", "-p", str(started.pid)],  # noqa: S607
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            assert writer_sandbox.CLEARED in shown
            assert "IKI_STATIC_ROOT" not in shown
    finally:
        started.terminate()
        started.wait(timeout=30)


def test_write_json_frames_are_length_prefixed(tmp_path: Path) -> None:
    out = tmp_path / "frame"
    with out.open("wb") as stream:
        write_json(stream, {"b": 1, "a": 2})
    assert out.read_bytes() == b"\0\0\0\x0d" + b'{"a":2,"b":1}'
