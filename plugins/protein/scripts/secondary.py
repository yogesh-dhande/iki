"""The protein plugin's secondary structure: DSSP (mkdssp 4.4) on each structure, in the
structure-tools image.

Each structure's file is read as the import read it (mmCIF as it is, PDB through gemmi),
so residues are numbered as its proteins' sequences were. mkdssp annotates the first
model with the Chemical Component Dictionary the image carries, and writes mmCIF. From
that, for each chain in chain_ids, a letter per residue of its sequence, and over all of
them: the shares in helices (H, G, I) and strands (E, B), and the summed accessibility.
The annotated file is kept too.

A structure mkdssp can't annotate fails its own run, saying why: most often a residue
newer than the dictionary, which the run names.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import structures

MKDSSP = "mkdssp"
HELIX = frozenset("HGI")
STRAND = frozenset("EB")
#: DSSP's codes in its mmCIF; `.` (no assignment) is written `-`.
CODES = frozenset("HGIPEBTS")
DICTIONARY = "the Chemical Component Dictionary of 2026-01-01"
MISSING = re.compile(r"Missing compound information for (\S+)")
TIMEOUT = 10 * 60


class AnnotationError(Exception):
    """A structure mkdssp couldn't annotate, and why."""


def _models(block: structures.gemmi.cif.Block) -> int:
    return len(set(block.find_values("_atom_site.pdbx_PDB_model_num"))) or 1


def mkdssp(given: Path, out: Path) -> None:
    """Annotates `given` (mmCIF) into `out`."""
    try:
        done = subprocess.run(  # noqa: S603 - the image's mkdssp, on a file written here
            [MKDSSP, "--output-format", "mmcif", "--calculate-accessibility", str(given), str(out)],
            capture_output=True,
            text=True,
            check=False,
            timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise AnnotationError(f"DSSP took longer than {TIMEOUT // 60} minutes.") from None
    if done.returncode == 0 and out.exists():
        return
    unknown = sorted(set(MISSING.findall(done.stderr)))
    if unknown:
        raise AnnotationError(
            f"DSSP doesn't know residue {', '.join(unknown[:10])}: it isn't in {DICTIONARY}."
        )
    lines = [line.strip() for line in done.stderr.splitlines() if line.strip()]
    said = lines[-1][:500] if lines else f"it exited with {done.returncode}"
    raise AnnotationError(f"DSSP couldn't annotate it: {said}")


def summarize(annotated: Path, chain_ids: list[str]) -> tuple[dict[str, Any], list[str]]:
    """The fields, and notes, from mkdssp's mmCIF for the chains in `chain_ids`."""
    block = structures.gemmi.cif.read(str(annotated)).sole_block()
    rows = structures.rows
    # Each chain's residues, in order: the whole sequence, with or without coordinates.
    numbered: dict[str, list[str]] = {}
    asym_of: dict[str, str] = {}
    for row in rows(block, "_pdbx_poly_seq_scheme.", ["asym_id", "seq_id", "pdb_strand_id"]):
        asym_of.setdefault(row["pdb_strand_id"], row["asym_id"])
        seq = numbered.setdefault(row["asym_id"], [])
        if not seq or seq[-1] != row["seq_id"]:
            seq.append(row["seq_id"])
    assigned: dict[tuple[str, str], tuple[str, str]] = {}
    for row in rows(
        block,
        "_dssp_struct_summary.",
        ["label_asym_id", "label_seq_id", "?secondary_structure", "?accessibility"],
    ):
        code = row["secondary_structure"]
        key = (row["label_asym_id"], row["label_seq_id"])
        assigned.setdefault(key, (code if code in CODES else "-", row["accessibility"]))
    notes: list[str] = []
    strings: list[str] = []
    counted = helix = strand = 0
    area = 0.0
    for chain in chain_ids:
        asym = asym_of.get(chain)
        if asym is None:
            notes.append(f"Chain {chain} isn't in DSSP's output: it has no secondary structure.")
            strings.append("")
            continue
        letters = []
        for seq_id in numbered[asym]:
            code, accessibility = assigned.get((asym, seq_id), ("?", ""))
            letters.append(code)
            if code == "?":
                continue
            counted += 1
            helix += code in HELIX
            strand += code in STRAND
            if accessibility:
                area += float(accessibility)
        strings.append("".join(letters))
        if letters and all(code == "?" for code in letters):
            notes.append(
                f"Chain {chain} has no residues DSSP could assign: are its backbones whole?"
            )
    values: dict[str, Any] = {"secondary_structure": strings}
    if counted:
        values["helix_fraction"] = round(100 * helix / counted, 1)
        values["strand_fraction"] = round(100 * strand / counted, 1)
        values["accessible_surface_area"] = round(area, 1)
    return values, notes


def annotate(
    entity: dict[str, Any], given: dict[str, Any], out_dir: Path
) -> tuple[dict[str, Any], list[str]]:
    """One structure's fields, with its annotated file in `out_dir`, and notes."""
    try:
        block = structures.block_of(Path(given["path"]))
    except structures.UnreadableError as why:
        raise AnnotationError(str(why)) from None
    notes = []
    if _models(block) > 1:
        notes.append("It has several models: DSSP annotated the first.")
    name = f"{entity['hrid']}-dssp.cif"
    with tempfile.TemporaryDirectory() as tmp:
        written = Path(tmp, "structure.cif")
        document = structures.gemmi.cif.Document()
        document.add_copied_block(block)
        document.write_file(str(written))
        mkdssp(written, out_dir / name)
    values, more = summarize(out_dir / name, list(entity["values"].get("chain_ids") or []))
    values["dssp"] = {"$file": name, "media_type": "chemical/x-mmcif"}
    return values, notes + more


def assign(run: dict[str, Any]) -> dict[str, Any]:
    entities = run["inputs"]["structure"]
    out_dir = Path(os.environ["IKI_OUT_DIR"])
    updates = []
    messages: list[dict[str, Any]] = []
    for done, entity in enumerate(entities, start=1):
        file_id = entity["values"]["structure"]
        try:
            values, notes = annotate(entity, run["files"][file_id], out_dir)
        except AnnotationError as why:
            print(f"{entity['hrid']}: {why}", flush=True)
            messages.append({"level": "error", "text": str(why)[:2000], "entity": entity["id"]})
            continue
        updates.append({"entity": entity["id"], "values": values})
        messages.extend(
            {"level": "info", "text": note[:2000], "entity": entity["id"]} for note in notes[:20]
        )
        shares = (
            f"{values['helix_fraction']}% helix, {values['strand_fraction']}% strand"
            if "helix_fraction" in values
            else "no residues assigned"
        )
        print(f"{entity['hrid']}: {shares}", flush=True)
        print(f"::progress {done / len(entities):.2f} {done} of {len(entities)}", flush=True)
    return {"updates": {"structure": updates}, "messages": messages}


def main() -> None:
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    output = {"apiVersion": "iki/v1", "kind": "RunOutput", **assign(run)}
    Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps(output), encoding="utf-8")


if __name__ == "__main__":
    main()
