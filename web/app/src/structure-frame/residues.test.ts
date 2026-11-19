/**
 * The structure viewer's residues and superposition, against Mol* itself: structures
 * parsed by Mol*'s mmCIF reader, selections and fits made with its own queries.
 */
import { OrderedSet } from "molstar/lib/mol-data/int.js";
import { CIF } from "molstar/lib/mol-io/reader/cif.js";
import { trajectoryFromMmCIF } from "molstar/lib/mol-model-formats/structure/mmcif.js";
import {
  QueryContext,
  Structure,
  StructureElement,
  StructureProperties,
  StructureSelection,
} from "molstar/lib/mol-model/structure.js";
import { alignAndSuperpose } from "molstar/lib/mol-model/structure/structure/util/superposition.js";
import { StructureSelectionQueries } from "molstar/lib/mol-plugin-state/helpers/structure-selection-query.js";
import { MolScriptBuilder as MS } from "molstar/lib/mol-script/language/builder.js";
import type { Expression } from "molstar/lib/mol-script/language/expression.js";
import { Script } from "molstar/lib/mol-script/script.js";
import { describe, expect, it } from "vitest";
import { fit, spansOf, spanTest, type Span } from "./residues";

const THREE: Record<string, string> = {
  A: "ALA",
  G: "GLY",
  S: "SER",
  M: "MET",
  W: "TRP",
  K: "LYS",
  V: "VAL",
  F: "PHE",
  L: "LEU",
};

/** One chain of Cα atoms, each [one-letter code, authors' number, insertion code]. */
function mmcif(id: string, residues: [string, number, string?][]): string {
  const sequence = residues.map(([r], i) => `1 ${String(i + 1)} ${THREE[r] ?? "ALA"}`);
  const atoms = residues.map(([r, n, code], i) => {
    const xyz = [3.8 * i, Math.sin(i) * 2, i % 3].map((v) => v.toFixed(3)).join(" ");
    return `ATOM ${String(i + 1)} C CA ${THREE[r] ?? "ALA"} A 1 ${String(i + 1)} ${xyz} A ${String(n)} ${code ?? "?"} 1`;
  });
  return [
    `data_${id}`,
    `_entry.id ${id}`,
    "_entity.id 1",
    "_entity.type polymer",
    "_entity_poly.entity_id 1",
    "_entity_poly.type 'polypeptide(L)'",
    "loop_",
    "_entity_poly_seq.entity_id",
    "_entity_poly_seq.num",
    "_entity_poly_seq.mon_id",
    ...sequence,
    "loop_",
    ...[
      "group_PDB",
      "id",
      "type_symbol",
      "label_atom_id",
      "label_comp_id",
      "label_asym_id",
      "label_entity_id",
      "label_seq_id",
      "Cartn_x",
      "Cartn_y",
      "Cartn_z",
      "auth_asym_id",
      "auth_seq_id",
      "pdbx_PDB_ins_code",
      "pdbx_PDB_model_num",
    ].map((tag) => `_atom_site.${tag}`),
    ...atoms,
    "",
  ].join("\n");
}

async function structure(text: string): Promise<Structure> {
  const parsed = await CIF.parse(text).run();
  if (parsed.isError) throw new Error(parsed.message);
  const [block] = parsed.result.blocks;
  if (!block) throw new Error("no block");
  const trajectory = await trajectoryFromMmCIF(block).run();
  const frame = trajectory.getFrameAtIndex(0);
  return Structure.ofModel("run" in frame ? await frame.run() : frame);
}

function loci(data: Structure, query: Expression): StructureElement.Loci {
  return StructureSelection.toLociWithSourceUnits(Script.getStructureSelection(query, data));
}

/** The residues of a loci, as 35A writes them. */
function named(found: StructureElement.Loci): string[] {
  const out: string[] = [];
  StructureElement.Loci.forEachLocation(found, (l) => {
    const name = `${String(StructureProperties.residue.auth_seq_id(l))}${StructureProperties.residue.pdbx_PDB_ins_code(l)}`;
    if (out[out.length - 1] !== name) out.push(name);
  });
  return out;
}

function restored(data: Structure, spans: Span[]): string[] {
  return named(loci(data, MS.struct.combinator.merge(spans.map(spanTest))));
}

function residue(number: number, code = ""): Expression {
  return MS.struct.generator.atomGroups({
    "residue-test": MS.core.logic.and([
      MS.core.rel.eq([MS.struct.atomProperty.macromolecular.auth_seq_id(), number]),
      MS.core.rel.eq([MS.struct.atomProperty.macromolecular.pdbx_PDB_ins_code(), code]),
    ]),
  });
}

/** A long CDR-H1 in Kabat's numbering: 35, 35A and 35B before 36. */
const KABAT: [string, number, string?][] = [
  ["A", 33],
  ["G", 34],
  ["S", 35],
  ["M", 35, "A"],
  ["W", 35, "B"],
  ["K", 36],
];

describe("a selection", () => {
  it("keeps a residue with an insertion code as itself", async () => {
    const data = await structure(mmcif("INS", KABAT));
    const spans = spansOf(loci(data, residue(35, "A")));
    expect(spans).toEqual([
      { chain: "A", start: 35, start_insertion: "A", end: 35, end_insertion: "A" },
    ]);
    expect(restored(data, spans)).toEqual(["35A"]);
  });

  it("runs through inserted residues in their order, and stops at a gap", async () => {
    const data = await structure(mmcif("INS", KABAT));
    const picked = MS.struct.combinator.merge([
      residue(34),
      residue(35),
      residue(35, "A"),
      residue(36),
    ]);
    const spans = spansOf(loci(data, picked));
    expect(spans).toEqual([
      { chain: "A", start: 34, end: 35, end_insertion: "A" },
      { chain: "A", start: 36, end: 36 },
    ]);
    expect(restored(data, spans)).toEqual(["34", "35", "35A", "36"]);
  });

  it("restores a span saved without insertion codes as its numbers' residues alone", async () => {
    const data = await structure(mmcif("INS", KABAT));
    expect(restored(data, [{ chain: "A", start: 35, end: 35 }])).toEqual(["35"]);
    expect(restored(data, [{ chain: "A", start: 34, end: 36 }])).toEqual([
      "34",
      "35",
      "35A",
      "35B",
      "36",
    ]);
  });
});

function chainOf(data: Structure): StructureElement.Loci {
  const found = StructureSelection.toLociWithSourceUnits(
    StructureSelectionQueries.trace.query(new QueryContext(data)),
  );
  return StructureElement.Loci(found.structure, found.elements.slice(0, 1));
}

describe("a superposition", () => {
  it("is over the residues it paired, not the moving chain's length", async () => {
    const reference = chainOf(
      await structure(
        mmcif("REF", [
          ["M", 1],
          ["W", 2],
          ["K", 3],
          ["V", 4],
        ]),
      ),
    );
    const moving = chainOf(
      await structure(
        mmcif("MOV", [
          ["M", 1],
          ["W", 2],
          ["K", 3],
          ["V", 4],
          ["F", 5],
          ["L", 6],
        ]),
      ),
    );
    const found = fit(reference, moving);
    expect(found?.residues).toBe(4);
    expect(OrderedSet.size(moving.elements[0]?.indices ?? OrderedSet.Empty)).toBe(6);
    // The same fit as Mol*'s own superposition.
    const [theirs] = alignAndSuperpose([reference, moving]);
    expect(found?.rmsd).toBeCloseTo(theirs?.rmsd ?? NaN, 9);
    // The same coordinates: 0 Å, to Mol*'s single-precision fit.
    expect(found?.rmsd).toBeCloseTo(0, 2);
    expect(Array.from(found?.bTransform ?? [])).toEqual(Array.from(theirs?.bTransform ?? []));
  });
});
