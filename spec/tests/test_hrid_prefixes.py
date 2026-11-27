"""A deployment's HRID prefixes (ADR 0033): its lockfile can give a type another, the
validator and the build use it in place of the plugin's, and Iki publishes the prefixes it
keeps, warning a lab's plugin that takes one."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from iki_spec import hrid_prefixes, naming
from iki_spec.builtin import core_plugin
from iki_spec.lockfile import load_lock, lock_to_dict
from iki_spec.model import BUILTIN_TYPES
from iki_spec.problems import Severity
from iki_spec.validate import Result, load_plugin

ROOT = Path(__file__).parents[2]
SHA = "4f1c2b9e0d6a7c3b8e5f1a2d9c0b7e6f5a4d3c2b"


def plugin(root: Path, name: str, types: dict[str, str]) -> Path:
    """A plugin of ``name`` with a type per entry, ``{"Buffer": "BUF"}``."""
    directory = root / naming.short_name(name)
    (directory / "types").mkdir(parents=True)
    (directory / "plugin.yaml").write_text(
        f"apiVersion: iki/v1\nkind: Plugin\nname: {name}\nversion: 1.0.0\n"
    )
    for type_name, prefix in types.items():
        (directory / "types" / f"{type_name.lower()}.yaml").write_text(
            "apiVersion: iki/v1\nkind: EntityType\n"
            f"name: {type_name}\nschema_version: 1\nhrid: {{ prefix: {prefix} }}\n"
        )
    return directory


def problems(result: Result, code: str) -> list[str]:
    return [p.message for p in result.problems if p.code == code]


def lockfile(tmp_path: Path, hrids: str, plugins: str = "") -> Path:
    file = tmp_path / "plugins.lock.yaml"
    file.write_text(
        "apiVersion: iki/v1\nkind: PluginLock\nplugins:\n"
        + textwrap.indent(
            textwrap.dedent(
                plugins
                or f"""\
                - name: acme/assays
                  git: https://github.com/acme/iki-assays.git
                  sha: {SHA}
                  state: active
                - name: acme/old
                  state: purged
                """
            ),
            "  ",
        )
        + textwrap.dedent(hrids)
    )
    return file


@pytest.mark.skipif(not (ROOT / ".git").exists(), reason="needs Iki's git repository")
def test_the_published_list_is_current() -> None:
    script = ROOT / "scripts" / "reserved_hrid_prefixes.py"
    result = subprocess.run(  # noqa: S603 - this interpreter, Iki's own script
        [sys.executable, str(script), "--check"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.reserved_hrid_prefixes
def test_the_list_has_cores_prefixes_and_ikis_plugins_and_reserves_planned_ones() -> None:
    kept = hrid_prefixes.reserved()
    for t in core_plugin().types.values():
        if t.hrid_prefix and not t.abstract:
            assert kept.core[t.hrid_prefix] == t.qualified_name
    # The records the server numbers itself.
    assert {"RUN", "BAT", "WF", "SETUP"} <= kept.core.keys()
    assert kept.plugins["PRT"] == "iki/protein.Protein"
    assert all(t.partition(".")[0] in naming.FIRST_PARTY for t in kept.plugins.values())
    # Specific complex types beyond the antibody, which the roadmap names.
    assert kept.planned["FCF"] == "iki/antibody.FcFusion"
    every = [*kept.core, *kept.plugins, *kept.planned]
    assert len(every) == len(set(every))
    assert all(hrid_prefixes.PATTERN.match(p) for p in every)
    shipped = json.loads((ROOT / "spec/src/iki_spec" / hrid_prefixes.FILENAME).read_text())
    assert list(shipped) == ["core", "plugins", "planned"]


@pytest.mark.reserved_hrid_prefixes
def test_a_lab_plugin_that_takes_one_of_ikis_plugins_prefixes_is_warned(tmp_path: Path) -> None:
    result = load_plugin(plugin(tmp_path, "acme/proteins", {"Protein": "PRT", "Kinase": "KIN"}))
    assert result.errors == []
    [warning] = [p for p in result.problems if p.code == "reserved-hrid-prefix"]
    assert warning.severity is Severity.WARNING
    assert warning.message == (
        "Iki's plugins use or reserve the HRID prefix `PRT`, for iki/protein.Protein: a "
        "deployment that installs both will have to give one of them another."
    )
    assert warning.hint is not None and "such as `APRT`" in warning.hint
    assert warning.path == ("hrid", "prefix")
    # And one the roadmap reserves.
    planned = load_plugin(plugin(tmp_path / "b", "acme/fusions", {"Fusion": "FCF"}))
    assert problems(planned, "reserved-hrid-prefix") == [
        "Iki's plugins use or reserve the HRID prefix `FCF`, for iki/antibody.FcFusion, which "
        "is planned: a deployment that installs both will have to give one of them another."
    ]
    # A deployment that gives the type its own prefix settles it.
    hrids = {"acme/proteins.Protein": "APRT"}
    given = load_plugin(plugin(tmp_path / "c", "acme/proteins", {"Protein": "PRT"}), hrids=hrids)
    assert given.problems == []


@pytest.mark.reserved_hrid_prefixes
def test_ikis_own_plugins_arent_warned() -> None:
    directory = ROOT / "plugins" / "protein"
    dependencies = [ROOT / "plugins" / name for name in ("dna", "lots")]
    result = load_plugin(directory, dependencies=[d for d in dependencies if d.is_dir()])
    assert problems(result, "reserved-hrid-prefix") == []


def test_no_plugin_takes_a_prefix_of_cores(tmp_path: Path) -> None:
    result = load_plugin(plugin(tmp_path, "acme/lab", {"Batch": "BAT", "Album": "APP"}))
    assert sorted(problems(result, "duplicate-hrid-prefix")) == [
        "`acme/lab.Album` uses `APP`, the HRID prefix of iki/core.App.",
        "`acme/lab.Batch` uses `BAT`, the HRID prefix of Iki's batches of runs.",
    ]
    hint = next(p.hint for p in result.problems if "Batch" in p.message)
    assert hint is not None and "hrids: { acme/lab.Batch: ABAT }" in hint


def test_two_plugins_with_one_prefix_are_told_the_override_that_settles_it(
    tmp_path: Path,
) -> None:
    assays = plugin(tmp_path, "acme/assays", {"Buffer": "BUF"})
    reagents = plugin(tmp_path, "globex/reagents", {"Buffer": "BUF"})
    result = load_plugin(reagents, dependencies=[assays])
    [clash] = [p for p in result.problems if p.code == "duplicate-hrid-prefix"]
    assert clash.message == (
        "`acme/assays.Buffer` and `globex/reagents.Buffer` both use the HRID prefix `BUF`."
    )
    assert clash.hint is not None
    assert "`hrids: { globex/reagents.Buffer: GBUF }`" in clash.hint
    # The deployment's prefix, wherever the build checks it.
    hrids = {"globex/reagents.Buffer": "GBUF"}
    assert load_plugin(reagents, dependencies=[assays], hrids=hrids).problems == []
    assert load_plugin(assays, dependencies=[reagents], hrids=hrids).problems == []
    # An override can clash too: it's the prefix the type gets.
    hrids = {"globex/reagents.Buffer": "GBUF", "acme/assays.Buffer": "GBUF"}
    again = load_plugin(reagents, dependencies=[assays], hrids=hrids)
    assert problems(again, "duplicate-hrid-prefix") == [
        "`acme/assays.Buffer` and `globex/reagents.Buffer` both use the HRID prefix `GBUF` "
        "(the prefix plugins.lock.yaml gives it)."
    ]


def test_the_override_moves_a_labs_type_before_one_of_ikis_and_never_cores() -> None:
    taken = {"BUF"}
    lab, ikis = "acme/assays.Buffer", "iki/reagents.Buffer"
    assert f"{{ {lab}: ABUF }}" in hrid_prefixes.settle(ikis, lab, "BUF", taken)
    assert f"{{ {lab}: ABUF }}" in hrid_prefixes.settle(lab, ikis, "BUF", taken)
    assert "{ acme/lab.Run: ARUN }" in hrid_prefixes.settle(
        "Iki's action runs", "acme/lab.Run", "RUN", taken
    )


def test_a_suggested_prefix_is_free_and_well_formed() -> None:
    kept = hrid_prefixes.reserved()
    taken = {"ABUF", "AABUF"}
    suggested = hrid_prefixes.suggest("acme/assays.Buffer", "BUF", taken)
    assert suggested == "ACBUF"
    assert hrid_prefixes.PATTERN.match(suggested) and suggested not in kept.all()
    long = hrid_prefixes.suggest("acme/assays.Longest", "LONGESTX", set())
    assert hrid_prefixes.PATTERN.match(long) and len(long) <= 8


def test_with_prefixes_gives_the_deployments_prefix_and_leaves_the_rest() -> None:
    core = core_plugin()
    moved = hrid_prefixes.with_prefixes(core, {"iki/core.App": "PAGE"})
    assert moved.types["iki/core.App"].hrid_prefix == "PAGE"
    assert moved.types["iki/core.View"] is core.types["iki/core.View"]
    assert hrid_prefixes.with_prefixes(core, {}) is core
    assert hrid_prefixes.effective(BUILTIN_TYPES["iki/core.App"], {"iki/core.App": "X1"}) == "X1"


def test_a_lockfile_gives_types_prefixes(tmp_path: Path) -> None:
    file = lockfile(tmp_path, "hrids:\n  acme/assays.Buffer: ABUF\n")
    result = load_lock(file)
    assert result.problems == []
    assert result.lock is not None and result.lock.hrids == {"acme/assays.Buffer": "ABUF"}
    assert lock_to_dict(result.lock)["hrids"] == {"acme/assays.Buffer": "ABUF"}
    # Without any, the record of the lock has none.
    bare = load_lock(lockfile(tmp_path, ""))
    assert bare.lock is not None and "hrids" not in lock_to_dict(bare.lock)


@pytest.mark.parametrize(
    ("hrids", "code", "message"),
    [
        (
            "iki/core.App: PAGE",
            "hrid-override-core",
            "`iki/core.App` is a core type: core's types keep Iki's prefixes.",
        ),
        (
            "globex/reagents.Buffer: GBUF",
            "hrid-override-unlisted",
            "`globex/reagents.Buffer` is a type of `globex/reagents`, which this lockfile "
            "doesn't list.",
        ),
        (
            "acme/old.Thing: OLD",
            "hrid-override-unlisted",
            "`acme/old.Thing` is a type of `acme/old`, which this lockfile purges.",
        ),
    ],
)
def test_a_lockfile_refuses_an_override_it_cant_apply(
    tmp_path: Path, hrids: str, code: str, message: str
) -> None:
    result = load_lock(lockfile(tmp_path, f"hrids:\n  {hrids}\n"))
    assert [(p.code, p.message) for p in result.problems] == [(code, message)]
    assert result.problems[0].location is not None


@pytest.mark.parametrize(
    "hrids",
    ["acme/assays.Buffer: buf", "acme/assays.Buffer: B", "Buffer: ABUF", "acme/assays.buffer: X1"],
)
def test_the_lockfile_schema_checks_types_and_prefixes(tmp_path: Path, hrids: str) -> None:
    result = load_lock(lockfile(tmp_path, f"hrids:\n  {hrids}\n"))
    assert [p.code for p in result.problems] == ["schema"]
