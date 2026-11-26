"""The oligo plugin's actions, by their first argument:

- ``register``: an oligonucleotide from its HELM.
- ``import``: oligonucleotides from a CSV or SDF file of HELM.
- ``compute``: an oligonucleotide's weight, formula, extinction and bases, from the
  monomers it lists (for one registered without this plugin's actions).
- ``library``: the HELM monomer library the plugin seeds the registry from.
- ``monomers``: a monomer's structure checked, and what's computed from it.
- ``duplex``: a duplex of two strands registered, with its duplex region and overhangs.
- ``analyse``: a duplex's region and overhangs, from its strands.

Each monomer an oligonucleotide's HELM names is the registry's: one given to the run as
an input (a lab's own), or else one of the library's, which the run registers if the
registry hasn't got it. Registering a library monomer the registry has with another
structure is refused (its structure is a fixed fact, ADR 0143), and the run with it.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import chemistry
import helm

MONOMER = "iki/oligo.Monomer"
OLIGO = "iki/oligo.Oligonucleotide"
DUPLEX = "iki/oligo.Duplex"
MAX_MESSAGES = 100
MAX_OUTPUT_BYTES = 2_500_000
#: The most an import registers at once.
MAX_IMPORT = 2_000
SDF_END = re.compile(r"^\$\$\$\$[ \t]*\r?$", re.MULTILINE)
#: A line of a CSV file, with its line end: CRLF, LF or CR, as RFC 4180 and Python's csv
#: module end one.
CSV_LINE = re.compile(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+\Z")


class Refused(Exception):  # noqa: N818 - reads as "the run is refused"
    """What keeps a run from doing what it was asked."""


def run_input() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(Path(os.environ["IKI_INPUT"]).read_text("utf-8"))
    return loaded


def write_output(output: Mapping[str, Any]) -> None:
    text = json.dumps({"apiVersion": "iki/v1", "kind": "RunOutput", **output})
    if len(text.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise Refused("What it registers is over the 2.5 MB a run's output holds: give it fewer.")
    Path(os.environ["IKI_OUTPUT"]).write_text(text, encoding="utf-8")


def _and(items: Iterable[str]) -> str:
    listed = list(items)
    return listed[0] if len(listed) == 1 else f"{', '.join(listed[:-1])} and {listed[-1]}"


def message(level: str, text: str, entity: str | None = None) -> dict[str, str]:
    out = {"level": level, "text": text[:2000]}
    if entity is not None:
        out["entity"] = entity
    return out


# --- Monomers -------------------------------------------------------------------------------


def monomer_values(m: chemistry.Monomer, source: str | None = None) -> dict[str, Any]:
    """A monomer's registry values: what it is, and what's computed from its structure."""
    s = chemistry.structure(m.smiles)
    free = chemistry.free(m.smiles)
    values: dict[str, Any] = {
        "polymer_type": m.polymer_type,
        "symbol": m.symbol,
        "name": m.name,
        "role": m.role,
        "natural_analogue": m.natural_analogue,
        "smiles": m.smiles,
        "structure": chemistry.structure_key(m),
        "attachments": [f"R{n} ({s.cap_groups[n]})" for n in sorted(s.caps)],
        "molecular_formula": chemistry.formula(free),
        "molecular_weight": round(chemistry.average_weight(free), 3),
    }
    if m.extinction_260 is not None:
        values["extinction_260"] = m.extinction_260
    if source is not None:
        values["source"] = source
    return {k: v for k, v in values.items() if v is not None}


_LIBRARY_SOURCES: dict[tuple[str, str], dict[str, Any]] = {}


def _library_entry(key: tuple[str, str]) -> dict[str, Any]:
    if not _LIBRARY_SOURCES:
        found = json.loads(chemistry.LIBRARY.read_text(encoding="utf-8"))
        for m in found["monomers"]:
            _LIBRARY_SOURCES[(m["polymer_type"], m["symbol"])] = m
    return _LIBRARY_SOURCES[key]


class Resolver:
    """Each monomer by (polymer type, symbol): the run's given ones, then the library's.
    It remembers which library monomers it used, to register them."""

    def __init__(self, given: Iterable[Mapping[str, Any]]) -> None:
        self.given = {m.key: m for m in (chemistry.from_entity(e) for e in given)}
        self.library = chemistry.library()
        self.used: dict[tuple[str, str], chemistry.Monomer] = {}

    def __call__(self, keys: Iterable[tuple[str, str]]) -> dict[tuple[str, str], chemistry.Monomer]:
        out: dict[tuple[str, str], chemistry.Monomer] = {}
        missing = []
        for key in keys:
            if key in self.given:
                out[key] = self.given[key]
            elif key in self.library:
                out[key] = self.used[key] = self.library[key]
            else:
                missing.append(key)
        if missing:
            raise chemistry.MissingError(sorted(missing))
        return out

    def explain(self, error: chemistry.MissingError) -> str:
        known = list(self.given) + list(self.library)
        hints = []
        for polymer_type, symbol in error.missing:
            same = [s for t, s in known if t == polymer_type and s.lower() == symbol.lower()]
            if same:
                hints.append(f"[{symbol}] isn't [{same[0]}]: symbols are compared exactly.")
        return " ".join(
            [
                str(error),
                "Register each as a monomer, with its structure, then give it to this "
                "action as one of its monomers.",
                *hints,
            ]
        )

    def creates(self) -> list[dict[str, Any]]:
        """The library monomers it used, registered: found where the registry has them."""
        out = []
        for key in sorted(self.used):
            entry = _library_entry(key)
            values = monomer_values(self.used[key], entry["source"])
            if entry.get("extinction_source"):
                values["extinction_source"] = entry["extinction_source"]
            out.append(
                {
                    "type": MONOMER,
                    "key": _monomer_key(key),
                    "values": values,
                    # Only what a registrar may correct fills, of what it gives; its
                    # structure must match.
                    "fills": [n for n in ("name", "source", "extinction_source") if n in values],
                }
            )
        return out

    def reference(self, m: chemistry.Monomer) -> Any:  # noqa: ANN401 - an ID or a create's key
        return m.id if m.id is not None else {"$create": _monomer_key(m.key)}


def _monomer_key(key: tuple[str, str]) -> str:
    return f"monomer:{key[0]}:{key[1]}"


# --- Oligonucleotides -----------------------------------------------------------------------


def computed(helm_text: str, resolve: Resolver) -> tuple[dict[str, Any], list[chemistry.Monomer]]:
    """What's computed from an oligonucleotide's HELM: its values, and its monomers."""
    read = helm.read(helm_text)
    chemistry.check_joined(read)
    keys = chemistry.unique_keys(read)
    monomers = resolve(keys)
    built = chemistry.build(read, monomers)
    units = [n for n in chemistry.nucleotides(built) if n.nucleotide]
    e = chemistry.extinction(built)
    values: dict[str, Any] = {
        "bases": chemistry.bases(built),
        "length": len(units),
        "molecular_formula": chemistry.formula(built.mol),
        "molecular_weight": round(chemistry.average_weight(built.mol), 2),
        "monoisotopic_mass": round(chemistry.monoisotopic_mass(built.mol), 4),
        "extinction_note": e.note,
        "modifications": chemistry.modifications(built),
        "positions": chemistry.positions(built),
    }
    if e.value is not None:
        values["extinction_260"] = e.value
    sodium = chemistry.sodium_salt_weight(built.mol)
    if sodium is not None:
        values["molecular_weight_sodium"] = round(sodium, 2)
    return values, [monomers[k] for k in keys]


def oligo_create(
    helm_text: str, resolve: Resolver, extra: Mapping[str, Any], key: str = "oligo"
) -> dict[str, Any]:
    values, used = computed(helm_text, resolve)
    return {
        "type": OLIGO,
        "key": key,
        "values": {
            "helm": helm_text,
            **{k: v for k, v in extra.items() if v not in (None, "")},
            **values,
            "monomers": [resolve.reference(m) for m in used],
        },
    }


def register(run: Mapping[str, Any]) -> dict[str, Any]:
    params = run["params"]
    resolve = Resolver(run["inputs"].get("monomers", []))
    extra = {k: params.get(k) for k in ("name", "description", "purpose")}
    try:
        oligo = oligo_create(params["helm"].strip(), resolve, extra)
    except chemistry.MissingError as missing:
        raise Refused(resolve.explain(missing)) from None
    except helm.HelmError as why:
        raise Refused(str(why)) from None
    v = oligo["values"]
    print(f"{v['bases']}: {v['molecular_formula']}, {v['molecular_weight']} g/mol")
    return {"creates": [*resolve.creates(), oligo]}


# --- Importing ------------------------------------------------------------------------------


def _rows_csv(text: str) -> list[dict[str, str]]:
    """Each row's values by its column's name, in lower case, and the record as written
    (under ""): every line it spans, a quoted value's line ends among them, with their
    own line ends, as the file has them (ADR 0052)."""
    # The reader is given the lines as CSV ends them, and only so (not at U+2028, form
    # feeds or the other line ends str.splitlines knows), so the lines it counts are these.
    lines = CSV_LINE.findall(text)
    reader = csv.DictReader(lines)
    fields = {f.lower().strip(): f for f in reader.fieldnames or []}
    if "helm" not in fields:
        raise Refused("Its first line names its columns, and one of them is helm.")
    out = []
    start = reader.line_num
    for row in reader:
        end = reader.line_num
        # Blank lines before it, which the reader skips, are no record's.
        while start < end and not lines[start].strip("\r\n"):
            start += 1
        values = {k.lower().strip(): (v or "").strip() for k, v in row.items() if k}
        values[""] = "".join(lines[start:end])
        out.append(values)
        start = end
    return out


#: The data items an import reads, by their names in lower case.
SDF_KEPT = ("helm", "name", "description")


def _rows_sdf(text: str, warnings: list[str]) -> list[dict[str, str]]:
    """Each SDF record's data items: its HELM, and its name and description if it has them;
    what's wrong with them in `warnings`.
    A record ends at a line that's only `$$$$`: HELM has `$$$$` in it too, so a HELM value
    wrapped over lines (200 characters a line, as CTfile Formats allows) may start one with
    it. Its HELM's lines are one HELM string; its name's, one line."""
    out: list[dict[str, str]] = []
    for record in SDF_END.split(text):
        if not record.strip():
            continue
        lines = record.splitlines()
        title = next((line.strip() for line in lines if line.strip()), "")
        end = next((i for i, line in enumerate(lines) if line.startswith("M  END")), -1)
        said: list[str] = []
        given = data_items(lines[end + 1 :], said)
        warnings += [f"Record {len(out) + 1}: {w}" for w in said]
        items = {name.lower(): value for name, value in given.items()}
        if "helm" in items:
            items["helm"] = "".join(line.strip() for line in items["helm"].splitlines())
        if "name" in items:
            items["name"] = " ".join(items["name"].split())
        items.setdefault("name", title)
        items[""] = record
        out.append(items)
    if not any("helm" in r for r in out):
        raise Refused("Its records have no HELM data item (> <HELM>).")
    return out


# The same reading of SD data items as the small_molecule plugin's (its scripts/sdf.py),
# which a plugin can't import from another: keep the two alike.
def data_items(lines: list[str], warnings: list[str]) -> dict[str, str]:
    """A record's SD fields, from the lines after its molblock, as CTfile Formats (SDfiles)
    has them: each data item is a header line starting with `>`, which names its field in
    angle brackets, then its value's lines, up to a blank line. Every line of a value is
    its value's, one starting with `>` too: only a line after a blank one can start the
    next item. As RDKit's SDMolSupplier reads them, the name is what's between the
    header's first `<` and its last `>`, and an item without one (a MACCS-II `DTn` field
    number alone) is passed over, with its value. A line of only spaces is a blank line,
    as the format's are. Lines between items that aren't headers aren't anyone's. A value's
    line that reads as the header of a field the import reads (`> <HELM>`), as a file
    missing a blank line has, is kept in the value, and `warnings` says so."""
    fields: dict[str, str] = {}
    name: str | None = None
    value: list[str] | None = None  # the lines of the item being read, if it's one
    for raw in lines:
        line = raw.rstrip("\r\n")
        if value is not None:
            if line.strip():
                value.append(line)
                other = _label(line.strip())
                if name is not None and other is not None and other.lower() in SDF_KEPT:
                    warnings.append(
                        f"Its {name} field has a line, {line.strip()!r}, that reads as the "
                        f"start of its {other} field, but an SD field ends only at a blank "
                        f"line, so the line is part of {name}'s value and {other} wasn't "
                        "read. Put a blank line before it to make it a field of its own."
                    )
                continue
            if name is not None:
                fields[name] = "\n".join(value).strip()
            name, value = None, None
            continue
        if line.strip().startswith(">"):
            name, value = _label(line.strip()), []
    if name is not None and value is not None:
        fields[name] = "\n".join(value).strip()
    return fields


def _label(header: str) -> str | None:
    """The field a data header names: what's between its first `<` and its last `>`, as
    RDKit reads it. None for a line that isn't a header, or one without a name."""
    if not header.startswith(">"):
        return None
    opens, closes = header.find("<", 1), header.rfind(">")
    return (header[opens + 1 : closes].strip() or None) if 0 < opens < closes else None


def import_file(run: Mapping[str, Any]) -> dict[str, Any]:
    params = run["params"]
    file_id = params["file"]
    path = Path(run["files"][file_id]["path"])
    # As written: its line ends untranslated, so each record's digest is of its bytes.
    with path.open(encoding="utf-8-sig", newline="") as written:
        text = written.read()
    sdf = path.suffix.lower() in (".sdf", ".sd") or SDF_END.search(text) is not None
    said: list[str] = []
    rows = _rows_sdf(text, said) if sdf else _rows_csv(text)
    if len(rows) > MAX_IMPORT:
        raise Refused(
            f"It has {len(rows):,} oligonucleotides: import up to {MAX_IMPORT:,} at once."
        )
    resolve = Resolver(run["inputs"].get("monomers", []))
    creates: list[dict[str, Any]] = []
    problems: list[str] = []
    seen: dict[str, int] = {}
    for number, row in enumerate(rows, start=1):
        text_helm = row.get("helm", "")
        if not text_helm:
            problems.append(f"Row {number} has no HELM.")
            continue
        before = dict(resolve.used)
        try:
            create = oligo_create(
                text_helm,
                resolve,
                {"name": row.get("name"), "description": row.get("description")},
                key=f"oligo:{number}",
            )
        except chemistry.MissingError as missing:
            # A row left out registers none of its monomers.
            resolve.used = before
            problems.append(f"Row {number}: {resolve.explain(missing)}")
            continue
        except helm.HelmError as why:
            resolve.used = before
            problems.append(f"Row {number}: {why}")
            continue
        if text_helm in seen:
            problems.append(f"Row {number} is row {seen[text_helm]}'s HELM again.")
            continue
        seen[text_helm] = number
        create["origin"] = {
            "source": "SDF file" if sdf else "CSV file",
            "id": (row.get("name") or f"{'Record' if sdf else 'Row'} {number}")[:255],
            "file": file_id,
            "sha256": hashlib.sha256(row[""].encode("utf-8")).hexdigest(),
        }
        creates.append(create)
    skip = params.get("on_error") == "skip"
    if problems and not skip:
        more = f" And {len(problems) - 10:,} more." if len(problems) > 10 else ""
        raise Refused(
            f"{len(problems):,} of {len(rows):,} rows can't be imported, so none was. "
            + " ".join(problems[:10])
            + more
        )
    if not creates:
        raise Refused("None of its rows could be imported. " + " ".join(problems[:10]))
    return {
        "creates": [*resolve.creates(), *creates],
        "metrics": {"rows": len(rows), "skipped": len(problems)},
        "messages": [message("warning", p) for p in [*said, *problems][:MAX_MESSAGES]],
    }


# --- Computing for one registered without the plugin's actions -----------------------------


def compute(run: Mapping[str, Any]) -> dict[str, Any]:
    updates = []
    messages = []
    for entity in run["inputs"]["oligonucleotide"]:
        listed = (entity.get("included") or {}).get("monomers") or []
        resolve = Resolver(listed)
        resolve.library = {}  # only what it lists: its values must be its monomers'
        try:
            values, used = computed(entity["values"]["helm"], resolve)
        except chemistry.MissingError as missing:
            messages.append(
                message(
                    "error",
                    f"{entity['hrid']}'s monomers don't include "
                    + ", ".join(f"[{s}]" for _, s in missing.missing)
                    + ", which its HELM names. Register it with Register an oligonucleotide "
                    "from HELM, which finds its monomers.",
                    entity["id"],
                )
            )
            continue
        except helm.HelmError as why:
            messages.append(message("error", f"{entity['hrid']}: {why}", entity["id"]))
            continue
        extra = sorted({m.label for m in resolve.given.values()} - {m.label for m in used})
        if extra:
            messages.append(
                message(
                    "warning",
                    f"{entity['hrid']} lists monomers its HELM doesn't name: {', '.join(extra)}.",
                    entity["id"],
                )
            )
        updates.append({"entity": entity["id"], "values": values})
    return {"updates": {"oligonucleotide": updates}, "messages": messages}


# --- Monomers -------------------------------------------------------------------------------


def library_run(run: Mapping[str, Any]) -> dict[str, Any]:
    resolve = Resolver([])
    resolve(list(resolve.library))
    creates = resolve.creates()
    print(f"{len(creates)} monomers, from {chemistry.LIBRARY.name}")
    return {"creates": creates}


def monomers(run: Mapping[str, Any]) -> dict[str, Any]:
    updates = []
    messages = []
    for entity in run["inputs"]["monomer"]:
        m = chemistry.from_entity(entity)
        try:
            values = monomer_values(m)
        except ValueError as why:
            messages.append(
                message(
                    "error", f"{entity['hrid']}'s structure can't be used: {why}.", entity["id"]
                )
            )
            continue
        caps = chemistry.structure(m.smiles).caps
        needs = {"sugar": {1, 2, 3}, "base": {1}, "linker": {1, 2}}.get(m.role, set())
        if m.polymer_type == "RNA" and not needs <= set(caps):
            messages.append(
                message(
                    "error",
                    f"{entity['hrid']} is a {m.role}, which joins at "
                    f"{_and(f'R{n}' for n in sorted(needs))}; its structure has only "
                    f"{_and(f'R{n}' for n in sorted(caps))}.",
                    entity["id"],
                )
            )
            continue
        updates.append(
            {
                "entity": entity["id"],
                "values": {
                    k: values[k]
                    for k in ("structure", "attachments", "molecular_formula", "molecular_weight")
                },
            }
        )
    return {"updates": {"monomer": updates}, "messages": messages}


# --- Duplexes -------------------------------------------------------------------------------

PAIRS = {("A", "T"), ("T", "A"), ("A", "U"), ("U", "A"), ("G", "C"), ("C", "G")}


def pairing(sense: str, antisense: str) -> dict[str, Any]:
    """The two strands' duplex region: where, antiparallel, they pair longest.

    Sense position i faces antisense position j (both 5′→3′, from 0) when i + j is the
    offset tried; every offset is tried. A region starts and ends with Watson–Crick
    pairs and has no two non-pairs in a row; the one with the most pairs is the
    duplex, then the one with fewest non-pairs, then the first.
    """
    best: tuple[int, int, int, int, int] | None = None  # pairs, -non-pairs, -offset, start, end
    for offset in range(len(sense) + len(antisense) - 1):
        facing = [(i, offset - i) for i in range(len(sense)) if 0 <= offset - i < len(antisense)]
        paired = [(sense[i], antisense[j]) in PAIRS for i, j in facing]
        k = 0
        while k < len(paired):
            if not paired[k]:
                k += 1
                continue
            start = last = k
            m = k + 1
            while m < len(paired):
                if paired[m]:
                    last = m
                elif not (m + 1 < len(paired) and paired[m + 1]):
                    break
                m += 1
            pairs = sum(paired[start : last + 1])
            candidate = (pairs, pairs - (last - start + 1), -offset, start, last)
            if best is None or candidate[:3] > best[:3]:
                best = candidate
            k = last + 1
    if best is None:
        return {
            "length": 0,
            "mismatches": 0,
            "sense": {"bases": sense},
            "antisense": {"bases": antisense},
            "pairs": [],
        }
    _, minus, minus_offset, start, last = best
    offset = -minus_offset
    first = max(0, offset - len(antisense) + 1)
    s_from, s_to = first + start, first + last
    a_from, a_to = offset - s_to, offset - s_from
    return {
        "length": s_to - s_from + 1,
        "mismatches": -minus,
        "sense": {"bases": sense, "from": s_from + 1, "to": s_to + 1},
        "antisense": {"bases": antisense, "from": a_from + 1, "to": a_to + 1},
        "sense_overhang_5": s_from,
        "sense_overhang_3": len(sense) - 1 - s_to,
        "antisense_overhang_5": a_from,
        "antisense_overhang_3": len(antisense) - 1 - a_to,
        "pairs": [
            {
                "sense": i + 1,
                "antisense": offset - i + 1,
                "paired": (sense[i], antisense[offset - i]) in PAIRS,
            }
            for i in range(s_from, s_to + 1)
        ],
    }


def duplex_values(sense: Mapping[str, Any], antisense: Mapping[str, Any]) -> dict[str, Any]:
    s, a = sense["values"], antisense["values"]
    for strand in (sense, antisense):
        if not strand["values"].get("bases"):
            raise Refused(
                f"{strand['hrid']} has no bases computed yet: compute its properties first."
            )
    found = pairing(s["bases"], a["bases"])
    values: dict[str, Any] = {
        "duplex_length": found["length"],
        "mismatches": found.get("mismatches", 0),
        "pairing": found,
    }
    for k in (
        "sense_overhang_5",
        "sense_overhang_3",
        "antisense_overhang_5",
        "antisense_overhang_3",
    ):
        if k in found:
            values[k] = found[k]
    if s.get("molecular_weight") is not None and a.get("molecular_weight") is not None:
        values["molecular_weight"] = round(
            _number(s["molecular_weight"]) + _number(a["molecular_weight"]), 2
        )
    if (
        s.get("molecular_weight_sodium") is not None
        and a.get("molecular_weight_sodium") is not None
    ):
        values["molecular_weight_sodium"] = round(
            _number(s["molecular_weight_sodium"]) + _number(a["molecular_weight_sodium"]), 2
        )
    if s.get("molecular_formula") and a.get("molecular_formula"):
        values["molecular_formula"] = chemistry.add_formulas(
            s["molecular_formula"], a["molecular_formula"]
        )
    return values


def _number(value: Any) -> float:  # noqa: ANN401 - a quantity's JSON
    return float(value["value"] if isinstance(value, dict) else value)


def duplex(run: Mapping[str, Any]) -> dict[str, Any]:
    params = run["params"]
    (sense,) = run["inputs"]["sense"]
    (antisense,) = run["inputs"]["antisense"]
    # A self-complementary strand, such as the Dickerson–Drew dodecamer, is both of its
    # duplex's strands: two copies of one molecule, each in its role.
    values = duplex_values(sense, antisense)
    print(f"{values['duplex_length']} bp, {values.get('mismatches', 0)} mismatches")
    return {
        "creates": [
            {
                "type": DUPLEX,
                "values": {
                    "strands": [
                        {"role": "sense", "strand": sense["id"]},
                        {"role": "antisense", "strand": antisense["id"]},
                    ],
                    **{k: params.get(k) for k in ("name", "description") if params.get(k)},
                    **values,
                },
                "derived_from": list(dict.fromkeys([sense["id"], antisense["id"]])),
            }
        ]
    }


def analyse(run: Mapping[str, Any]) -> dict[str, Any]:
    (entity,) = run["inputs"]["duplex"]
    strands = {s["id"]: s for s in run["inputs"].get("strands", [])}
    roles = {row["role"]: row["strand"] for row in entity["values"]["strands"]}
    missing = [r for r in ("sense", "antisense") if roles.get(r) not in strands]
    if missing:
        return {
            "messages": [
                message(
                    "error",
                    f"{entity['hrid']}'s {' and '.join(missing)} strand isn't in its project, so "
                    "it can't be read.",
                    entity["id"],
                )
            ]
        }
    values = duplex_values(strands[roles["sense"]], strands[roles["antisense"]])
    return {"updates": {"duplex": [{"entity": entity["id"], "values": values}]}}


COMMANDS = {
    "register": register,
    "import": import_file,
    "compute": compute,
    "library": library_run,
    "monomers": monomers,
    "duplex": duplex,
    "analyse": analyse,
}


def main() -> None:
    try:
        write_output(COMMANDS[sys.argv[1]](run_input()))
    except Refused as why:
        print(f"::error {why}", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
