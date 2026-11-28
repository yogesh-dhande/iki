"""The plugin runtime, iki-plugin-runtime (ADR 0288): its readers, and the wheel each
release ships, which iki-spec carries."""

from __future__ import annotations

import ast
import bz2
import gc
import gzip
import hashlib
import lzma
import shutil
import subprocess
import sys
import threading
import tomllib
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from iki_runtime import airr, fastq, helm, text
from iki_spec import helm as iki_spec_helm
from iki_spec import plugin_runtime

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "runtime"
SOURCES = sorted((RUNTIME / "src" / "iki_runtime").glob("*.py"))


# --- Compressed text ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("compress", "name"),
    [(gzip.compress, "gzip"), (bz2.compress, "bzip2"), (lzma.compress, "xz"), (bytes, None)],
)
def test_a_files_compression_is_read_from_its_first_bytes(
    tmp_path: Path, compress: Callable[[bytes], bytes], name: str | None
) -> None:
    # Named as if plain, or as if gzipped: the name says nothing.
    for filename in ("report.tsv", "report.tsv.gz"):
        path = tmp_path / filename
        path.write_bytes(compress("﻿sequence_id\tv_call\r\ns1\tIGHV1-2*02\r\n".encode()))
        assert text.compression(path) == name
        with text.open_text(path) as source:
            assert source.read() == "sequence_id\tv_call\r\ns1\tIGHV1-2*02\r\n"
        with text.open_text(path, newline=None) as source:
            assert source.readlines() == ["sequence_id\tv_call\n", "s1\tIGHV1-2*02\n"]
        with text.open_bytes(path) as source:
            assert source.read().startswith(b"\xef\xbb\xbfsequence_id")


def test_bgzf_reads_as_the_gzip_it_is(tmp_path: Path) -> None:
    # BGZF (BAM, tabix) is gzip members one after another, ending with an empty one.
    path = tmp_path / "reads.fq.bgz"
    path.write_bytes(
        gzip.compress(b"@a\nAC\n+\nII\n") + gzip.compress(b"@b\nG\n+\nI\n") + gzip.compress(b"")
    )
    assert [r.title for r in fastq.read(path)] == [b"a", b"b"]


def test_a_cut_off_compressed_file_is_unreadable_and_says_so(tmp_path: Path) -> None:
    path = tmp_path / "cut.fastq.gz"
    path.write_bytes(gzip.compress(b"@a\nACGT\n+\nIIII\n" * 100)[:-20])
    with pytest.raises(text.UNREADABLE):
        list(fastq.read(path))


# --- FASTQ -------------------------------------------------------------------------------


def test_fastq_records_are_read_however_they_are_wrapped(tmp_path: Path) -> None:
    # Cock et al. (2010): bases and qualities may each be on several lines, the + line may
    # repeat the title, and a quality line may start with @ (Phred 31) or + (Phred 10).
    path = tmp_path / "wrapped.fastq"
    path.write_bytes(
        b"@r1 sample=7\r\nACGT\r\nAC\r\n+r1 sample=7\r\n@III\r\n+I\r\n\n@r2\nGG\nCC\n+\nII\nII\n\n"
    )
    reads = list(fastq.read(path))
    assert reads == [
        fastq.Read(b"r1 sample=7", b"ACGTAC", b"@III+I"),
        fastq.Read(b"r2", b"GGCC", b"IIII"),
    ]
    assert [r.id for r in reads] == [b"r1", b"r2"]


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"r1\nA\n+\nI\n", "read 1: expected an @ title"),
        (b"@\nA\n+\nI\n", "read 1: expected an @ title"),
        (b"@a\nA\n+\nI\n@b\n+\n\n", "read 2: the read has no bases"),
        (b"@a\nAC\n+\nI\n", "read 1: expected printable"),
        (b"@a\nA\n+\nII\n", "read 1: the base and quality counts differ"),
        (b"@a\nA\n+b\nI\n", "read 1: the \\+ title differs"),
        (b"@a\nA*\n+\nII\n", "read 1: expected sequence bases"),
        (b"@a\nACGT\n", "read 1: expected sequence bases"),
        (b"@a\nAC\n+\nI", "read 1: expected printable"),
    ],
)
def test_what_isnt_fastq_is_refused_saying_which_read(
    tmp_path: Path, data: bytes, message: str
) -> None:
    path = tmp_path / "bad.fastq"
    path.write_bytes(data)
    with pytest.raises(ValueError, match=message):
        list(fastq.read(path))


def test_a_read_too_long_to_hold_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fastq, "MAX_READ_LENGTH", 8)
    path = tmp_path / "long.fastq"
    path.write_bytes(b"@a\nACGTA\nACGTA\n+\nIIIIIIIIII\n")
    with pytest.raises(ValueError, match="exceeds 10 million bases"):
        list(fastq.read(path))


# --- AIRR rearrangements -----------------------------------------------------------------


def test_airr_rows_are_read_by_their_header(tmp_path: Path) -> None:
    path = tmp_path / "airr.tsv.gz"
    path.write_bytes(
        gzip.compress(
            b"\xef\xbb\xbfsequence_id\tlocus\tv_call\tj_call\r\n\r\n"
            b"s1\t\tIGHV3-23*01\tIGHJ4*02\r\ns2\tIGK\tIGKV1-39*01\tIGKJ1*01\r\n"
        )
    )
    rows = list(airr.read(path, required=("sequence_id", "v_call")))
    assert rows[0] == {
        "sequence_id": "s1",
        "locus": "",
        "v_call": "IGHV3-23*01",
        "j_call": "IGHJ4*02",
    }
    assert [airr.locus(r) for r in rows] == ["IGH", "IGK"]


@pytest.mark.parametrize(
    ("given", "message"),
    [
        ("", "must name distinct, nonempty columns"),
        ("sequence_id\tsequence_id\n", "must name distinct"),
        ("sequence_id\t\tv_call\n", "must name distinct"),
        ("v_call\tj_call\n", "has no sequence_id column"),
        ("sequence_id\tv_call\ns1\n", "AIRR row 1 has 1 columns, expected 2"),
    ],
)
def test_what_isnt_an_airr_file_is_refused(given: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        list(airr.rearrangements(given.splitlines(keepends=True)))


@pytest.mark.parametrize(
    ("row", "locus"),
    [
        ({"locus": "IGL", "v_call": "IGHV1-2*02", "j_call": "IGHJ4*02"}, "IGL"),
        ({"locus": "", "v_call": "TRBV20-1*01", "j_call": "TRBJ2-7*01"}, "TRB"),
        ({"v_call": "IGHV1-2*02,IGHV1-3*01", "j_call": "IGHJ4*02"}, "IGH"),
        # V and J of different loci, a call missing, or a gene of no AIRR locus.
        ({"v_call": "IGHV1-2*02", "j_call": "IGKJ1*01"}, None),
        ({"v_call": "IGHV1-2*02", "j_call": ""}, None),
        ({"v_call": "ABCV1*01", "j_call": "ABCJ1*01"}, None),
    ],
)
def test_a_rows_locus_is_its_own_or_the_one_its_calls_share(
    row: dict[str, str], locus: str | None
) -> None:
    assert airr.locus(row) == locus


# --- HELM --------------------------------------------------------------------------------


@pytest.fixture
def fresh(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The HELM reader as a new process has it: no module compiled, no instance."""
    kept = (helm._compiled, helm._local)
    helm._compiled, helm._local = None, threading.local()
    monkeypatch.delenv(helm.ENVIRONMENT_VARIABLE, raising=False)
    yield
    helm._compiled, helm._local = kept
    # What the test compiled is freed now, while wasmtime can free it, not at exit.
    gc.collect()


@pytest.mark.wasm
@pytest.mark.usefixtures("fresh")
def test_helm_is_read_as_iki_reads_it_canonical_and_in_canonical_order() -> None:
    """The reading iki-spec's (and so the server's) is, as values: polymers in canonical
    order and numbered again, a repeat written out, each connection's ends."""
    text = (
        "CHEM1{[BHQ1]}|RNA2{P.([dR](A)P)'2'.[dR](C)}\"probe\"|CHEM2{[5FAM]}"
        "$RNA2,CHEM2,1:R1-1:R1|RNA2,CHEM1,3:R2-1:R1$$$V2.0"
    )
    reading = helm.read(text)
    assert reading.canonical == iki_spec_helm.canonical(text)
    assert [(p.id, p.type) for p in reading.polymers] == [
        ("RNA1", "RNA"),
        ("CHEM1", "CHEM"),
        ("CHEM2", "CHEM"),
    ]
    strand = reading.polymer("RNA1")
    assert [(m.position, m.id, m.branch, m.unit) for m in strand.monomers] == [
        (1, "P", False, 1),
        (2, "dR", False, 2),
        (3, "A", True, 2),
        (4, "P", False, 2),
        (5, "dR", False, 3),
        (6, "A", True, 3),
        (7, "P", False, 3),
        (8, "dR", False, 4),
        (9, "C", True, 4),
    ]
    assert (strand.units, strand.length, strand.ambiguous) == (4, helm.Length(3, 3), False)
    # As written, the 3′ quencher hangs from position 3, the repeated group counting once
    # (HELM 2.04, 5.2.3); it's the last sugar, 8, once the repeat is written out. 5FAM and
    # BHQ1 are numbered in their canonical order, the other way round.
    assert [c.text for c in reading.connections] == [
        "RNA1,CHEM1,1:R1-1:R1",
        "RNA1,CHEM2,8:R2-1:R1",
    ]
    _, three = reading.connections
    assert three.one == helm.End("RNA1", 8, "R2", "8")
    assert three.other == helm.End("CHEM2", 1, "R1", "1")
    assert not three.pair
    assert [reading.polymer(c).monomers[0].id for c in ("CHEM1", "CHEM2")] == ["5FAM", "BHQ1"]
    assert (reading.groups, reading.version1) == ((), False)


@pytest.mark.wasm
@pytest.mark.usefixtures("fresh")
def test_what_has_no_one_structure_is_read_and_says_so() -> None:
    """HELM a plugin may refuse is still HELM: a list, a range of repeats, a base pair in
    HELM 1's third section, an end that names monomers rather than a position, a group."""
    pairs = helm.read("RNA1{R(A)P.R(C)}|RNA2{R(G)P.R(U)}$$RNA1,RNA2,2:pair-5:pair$$")
    assert pairs.version1
    assert pairs.connections[0].pair
    ranged = helm.read("RNA1{R(A)P.(R(C)P)'2-3'}$$$$V2.0").polymers[0]
    assert ranged.ambiguous
    assert ranged.length == helm.Length(3, 4)
    listed = helm.read("RNA1{[dR](A,C)P}$$$$V2.0").polymers[0].monomers[1]
    assert (listed.id, listed.list, listed.branch) == ("(A,C)", True, True)
    unknown = helm.read("RNA1{R(A)P}|CHEM1{[X]}$RNA1,CHEM1,(A,P):R1-1:?$$$V2.0")
    (end, other) = (unknown.connections[0].one, unknown.connections[0].other)
    assert (end.position, end.at, end.attachment) == (None, "(A,P)", "R1")
    assert (other.position, other.attachment) == (1, "?")
    grouped = helm.read("RNA1{R(A)}|RNA2{R(G)}$$G1(RNA1+RNA2:0.50)$$V2.0")
    assert grouped.groups == (
        helm.Group("G1", True, (helm.GroupMember("RNA1", None), helm.GroupMember("RNA2", "0.5"))),
    )


@pytest.mark.wasm
@pytest.mark.usefixtures("fresh")
def test_what_isnt_helm_is_refused_saying_where() -> None:
    with pytest.raises(helm.HelmError, match="Expected a monomer") as refused:
        helm.read("RNA1{R(A)P..R(C)}$$$$V2.0")
    assert refused.value.at == 11
    (good, bad) = helm.read_all(["RNA1{R(A)}$$$$V2.0", "RNA1{R(A)}$$$"])
    assert isinstance(good, helm.Reading)
    assert isinstance(bad, helm.HelmError)
    assert "four sections" in bad.message
    assert helm.read_all([]) == []


@pytest.mark.usefixtures("fresh")
def test_the_module_is_the_environments_then_iki_specs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An environment Iki builds has it under its prefix; a lab's image names it; a Python
    with iki-spec (Iki's checkout, `iki plugin test`) has iki-spec's."""
    assert helm.module_path() == plugin_runtime.WASM
    own = tmp_path / "env" / helm.IN_ENVIRONMENT
    own.parent.mkdir(parents=True)
    own.write_bytes(b"\0asm")
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "env"))
    assert helm.module_path() == own
    named = tmp_path / "named.wasm"
    monkeypatch.setenv(helm.ENVIRONMENT_VARIABLE, str(named))
    with pytest.raises(helm.UnavailableError, match="isn't a file"):
        helm.module_path()
    named.write_bytes(b"\0asm")
    assert helm.module_path() == named


@pytest.mark.usefixtures("fresh")
def test_without_wasmtime_it_says_to_install_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "wasmtime", None)
    with pytest.raises(helm.UnavailableError, match=r"add it to the environment's requirements"):
        helm.read("RNA1{R(A)}$$$$V2.0")


@pytest.mark.wasm
@pytest.mark.usefixtures("fresh")
def test_an_environments_build_compiles_the_module_once_for_its_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Compiling iki-wasm takes seconds, which every run would spend: the environment's
    build compiles it beside the module, named by the module's digest, and a run loads
    that. Compiled code no one else could have written is used, and anything else isn't."""
    module = tmp_path / "iki_wasm.wasm"
    shutil.copyfile(plugin_runtime.WASM, module)
    done = subprocess.run(  # noqa: S603 - this Python, the runtime's own command
        [sys.executable, "-m", "iki_runtime.helm", "precompile", str(module)],
        check=True,
        capture_output=True,
        text=True,
    )
    compiled = Path(done.stdout.strip())
    digest = hashlib.sha256(module.read_bytes()).hexdigest()
    assert compiled == tmp_path / f"iki_wasm.{digest[:32]}.cwasm"
    assert compiled == helm.precompiled(module)
    monkeypatch.setenv(helm.ENVIRONMENT_VARIABLE, str(module))
    loads: list[str] = []
    wasmtime = helm._wasmtime()
    real = wasmtime.Module.deserialize_file

    def deserialize_file(engine: object, path: str) -> object:
        loads.append(path)
        return real(engine, path)

    monkeypatch.setattr(wasmtime.Module, "deserialize_file", staticmethod(deserialize_file))
    assert helm.canonical("RNA1{[R](A)}$$$$") == "RNA1{R(A)}$$$$V2.0"
    assert loads == [str(compiled)]
    # Code others could have changed isn't run: the module is compiled again instead.
    compiled.chmod(0o666)
    helm._compiled, helm._local = None, threading.local()
    assert helm.canonical("RNA1{[R](A)}$$$$") == "RNA1{R(A)}$$$$V2.0"
    assert loads == [str(compiled)]


# --- The package -------------------------------------------------------------------------


def test_it_is_pythons_standard_library_for_python_3_8_or_later() -> None:
    """It installs into environments Iki builds and into labs' own images, as the action
    helper runs in them (ADR 0287). Only its HELM reader imports anything else: wasmtime,
    declared as its optional `helm` extra, and imported only when HELM is read."""
    config = tomllib.loads((RUNTIME / "pyproject.toml").read_text())
    assert config["project"]["requires-python"] == ">=3.8"
    assert config["project"]["dependencies"] == []
    optional = config["project"]["optional-dependencies"]
    assert optional == {"helm": ["wasmtime>=51.0.0"]}
    lazily = {"helm.py": {"wasmtime"}}
    for source in SOURCES:
        tree = ast.parse(source.read_text(), feature_version=(3, 8))
        # What's imported when the module is: at its top level, not in a function.
        top_level = {
            id(node)
            for statement in tree.body
            for node in ast.walk(statement)
            if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                names = [node.module or ""]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                if top in lazily.get(source.name, set()):
                    assert id(node) not in top_level, (source.name, name, "imported eagerly")
                    continue
                assert top in sys.stdlib_module_names or top in {"iki_runtime", "__future__"}, (
                    source.name,
                    name,
                )


def test_its_version_is_ikis() -> None:
    """Each release ships it at its own version, and the release refuses a tag that the
    committed versions don't match."""
    config = tomllib.loads((RUNTIME / "pyproject.toml").read_text())
    chart = (ROOT / "deploy" / "helm" / "iki" / "Chart.yaml").read_text()
    (version,) = [line.split()[1] for line in chart.splitlines() if line.startswith("version:")]
    assert config["project"]["version"] == version
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert "runtime/pyproject.toml" in release


def test_its_build_backend_is_pinned_so_every_build_is_the_same_file() -> None:
    config = tomllib.loads((RUNTIME / "pyproject.toml").read_text())
    (backend,) = config["build-system"]["requires"]
    assert backend.startswith("uv_build=="), backend


def test_iki_spec_carries_the_wheel_this_checkout_builds(tmp_path: Path) -> None:
    """The wheel iki-spec carries is this source's, file for file; and building it again
    gives the same bytes, so the release's PyPI copy is the one Iki ships."""
    carried = plugin_runtime.wheel()
    assert carried.sha256 == hashlib.sha256(carried.path.read_bytes()).hexdigest()
    with zipfile.ZipFile(carried.path) as wheel:
        packaged = {
            Path(n).name: wheel.read(n) for n in wheel.namelist() if n.startswith("iki_runtime/")
        }
        metadata = next(n for n in wheel.namelist() if n.endswith(".dist-info/METADATA"))
        assert "Name: iki-plugin-runtime" in wheel.read(metadata).decode()
    assert {name: packaged[name] for name in packaged if name.endswith(".py")} == {
        source.name: source.read_bytes() for source in SOURCES
    }
    subprocess.run(  # noqa: S603 - the checkout's own script
        [str(ROOT / "scripts" / "build-plugin-runtime.sh"), str(tmp_path)],
        check=True,
        capture_output=True,
    )
    (again,) = tmp_path.glob("*.whl")
    assert again.name == carried.name
    assert hashlib.sha256(again.read_bytes()).hexdigest() == carried.sha256


def test_without_a_wheel_iki_spec_says_how_to_build_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(plugin_runtime, "DIRECTORY", tmp_path)
    with pytest.raises(plugin_runtime.UnavailableError, match="no iki-plugin-runtime wheel"):
        plugin_runtime.wheel()
    for version in ("0.1.0", "0.2.0"):
        (tmp_path / f"iki_plugin_runtime-{version}-py3-none-any.whl").write_bytes(b"PK")
    with pytest.raises(plugin_runtime.UnavailableError, match="2 iki-plugin-runtime wheels"):
        plugin_runtime.wheel()
