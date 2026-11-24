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
import type { Span } from "../structure-viewer.js";
import { fit, spansOf, spanTest } from "./residues.js";

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

/** A Cα atom as an entry has it: its residue, its place in its entity's sequence
 * (label_seq_id), its authors' number and insertion code, its alternate location, x, y,
 * z, and occupancy. */
type Atom = [string, number, number, string, string, number, number, number, number];

/** An entry's chain of Cα atoms, as mmCIF, in the chain its authors named. */
function entry(id: string, chain: string, atoms: Atom[]): string {
  const residues = new Map(atoms.map(([name, seq]) => [seq, name]));
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
    ...[...residues].map(([seq, name]) => `1 ${String(seq)} ${name}`),
    "loop_",
    ...[
      "group_PDB",
      "id",
      "type_symbol",
      "label_atom_id",
      "label_alt_id",
      "label_comp_id",
      "label_asym_id",
      "label_entity_id",
      "label_seq_id",
      "pdbx_PDB_ins_code",
      "Cartn_x",
      "Cartn_y",
      "Cartn_z",
      "occupancy",
      "auth_seq_id",
      "auth_asym_id",
      "pdbx_PDB_model_num",
    ].map((tag) => `_atom_site.${tag}`),
    ...atoms.map(
      ([name, seq, number, code, alt, x, y, z, occupancy], i) =>
        `ATOM ${String(i + 1)} C CA ${alt || "."} ${name} A 1 ${String(seq)} ${code || "?"} ` +
        `${String(x)} ${String(y)} ${String(z)} ${String(occupancy)} ${String(number)} ${chain} 1`,
    ),
    "",
  ].join("\n");
}

/** 1PPB's thrombin light chain (chain L), as the entry has it, cut to its first twelve
 * Cα atoms: chymotrypsin numbering, so it runs 1H, 1G, … 1A, then 1, 2, 3, 4 (RCSB PDB
 * 1PPB, Bode et al., EMBO J 8:3467, 1989). */
const THROMBIN_L: Atom[] = [
  ["THR", 1, 1, "H", "", -8.111, 49.104, 20.376, 1],
  ["PHE", 2, 1, "G", "", -7.837, 46.07, 22.662, 1],
  ["GLY", 3, 1, "F", "", -10.229, 43.057, 22.414, 1],
  ["SER", 4, 1, "E", "", -9.828, 42.509, 26.233, 1],
  ["GLY", 5, 1, "D", "", -11.349, 42.841, 29.879, 1],
  ["GLU", 6, 1, "C", "", -7.919, 41.511, 31.123, 1],
  ["ALA", 7, 1, "B", "", -5.395, 44.338, 32.319, 1],
  ["ASP", 8, 1, "A", "", -5.064, 42.967, 35.816, 1],
  ["CYS", 9, 1, "", "", -3.425, 40.108, 33.934, 1],
  ["GLY", 10, 2, "", "", -1.072, 37.691, 35.474, 1],
  ["LEU", 11, 3, "", "", -2.391, 38.328, 39.008, 1],
  ["ARG", 12, 4, "", "", -4.137, 35.302, 40.197, 1],
];

describe("a selection where the numbering runs backwards", () => {
  it("restores what was selected, not the residues numbered between", async () => {
    const data = await structure(entry("1PPB", "L", THROMBIN_L));
    const picked = (...names: [number, string?][]) =>
      MS.struct.combinator.merge(
        names.map(([n, code]) =>
          MS.struct.generator.atomGroups({
            "chain-test": MS.core.rel.eq([
              MS.struct.atomProperty.macromolecular.auth_asym_id(),
              "L",
            ]),
            "residue-test": MS.core.logic.and([
              MS.core.rel.eq([MS.struct.atomProperty.macromolecular.auth_seq_id(), n]),
              MS.core.rel.eq([
                MS.struct.atomProperty.macromolecular.pdbx_PDB_ins_code(),
                code ?? "",
              ]),
            ]),
          }),
        ),
      );
    // 1 and 2: a range 1–2 would take in 1A to 1H, which come before them in the chain.
    const spans = spansOf(loci(data, picked([1], [2])));
    expect(spans).toEqual([
      { chain: "L", start: 1, end: 1 },
      { chain: "L", start: 2, end: 2 },
    ]);
    expect(restored(data, spans)).toEqual(["1", "2"]);
    // Past them, the numbering rises, and a range is a range again.
    expect(spansOf(loci(data, picked([2], [3], [4])))).toEqual([{ chain: "L", start: 2, end: 4 }]);
    for (const selection of [
      picked([1, "B"], [1, "A"], [1], [2]),
      picked([1, "H"], [1, "G"]),
      picked([1, "A"], [1], [2], [3], [4]),
    ]) {
      const chosen = loci(data, selection);
      expect(restored(data, spansOf(chosen))).toEqual(named(chosen));
    }
  });
});

function chainOf(data: Structure): StructureElement.Loci {
  const found = StructureSelection.toLociWithSourceUnits(
    StructureSelectionQueries.trace.query(new QueryContext(data)),
  );
  return StructureElement.Loci(found.structure, found.elements.slice(0, 1));
}

/** 4AR3's rubredoxin (Pyrococcus furiosus, neutron diffraction at 1.05 Å), cut to its first
 * 21 Cα atoms as the entry has them, Ile11 and Pro19 each in two alternate locations,
 * A and B (RCSB PDB 4AR3). */
const RUBREDOXIN: Atom[] = [
  ["MET", 1, 0, "", "", 23.129, 30.956, 4.875, 1],
  ["ALA", 2, 1, "", "", 21.47, 33.37, 2.449, 1],
  ["LYS", 3, 2, "", "", 20.392, 36.935, 2.699, 1],
  ["TRP", 4, 3, "", "", 17.299, 38.421, 1.021, 1],
  ["VAL", 5, 4, "", "", 16.5, 42.115, 0.634, 1],
  ["CYS", 6, 5, "", "", 13.079, 43.717, 0.89, 1],
  ["LYS", 7, 6, "", "", 12.89, 45.7, -2.356, 1],
  ["ILE", 8, 7, "", "", 10.531, 48.246, -0.75, 1],
  ["CYS", 9, 8, "", "", 12.415, 49.314, 2.402, 1],
  ["GLY", 10, 9, "", "", 15.912, 47.768, 2.355, 1],
  ["TYR", 11, 10, "", "", 15.477, 45.399, 5.346, 1],
  ["ILE", 12, 11, "", "A", 17.703, 42.316, 4.985, 0.59],
  ["ILE", 12, 11, "", "B", 17.561, 42.261, 4.864, 0.41],
  ["TYR", 13, 12, "", "", 16.227, 38.959, 5.98, 1],
  ["ASP", 14, 13, "", "", 19.287, 37.044, 7.109, 1],
  ["GLU", 15, 14, "", "", 18.633, 33.302, 7.306, 1],
  ["ASP", 16, 15, "", "", 21.332, 33.017, 9.961, 1],
  ["ALA", 17, 16, "", "", 19.432, 35.497, 12.233, 1],
  ["GLY", 18, 17, "", "", 15.813, 34.477, 11.443, 1],
  ["ASP", 19, 18, "", "", 13.163, 36.857, 12.854, 1],
  ["PRO", 20, 19, "", "A", 12.601, 35.53, 16.358, 0.61],
  ["PRO", 20, 19, "", "B", 12.658, 35.598, 16.422, 0.39],
  ["ASP", 21, 20, "", "", 10.373, 38.419, 17.514, 1],
];

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

  it("counts a residue once, however many alternate locations its Cα has", async () => {
    const reference = chainOf(await structure(entry("4AR3", "A", RUBREDOXIN)));
    const moving = chainOf(await structure(entry("4AR3", "A", RUBREDOXIN)));
    // 23 Cα positions, of 21 residues.
    expect(OrderedSet.size(reference.elements[0]?.indices ?? OrderedSet.Empty)).toBe(23);
    const found = fit(reference, moving);
    expect(found?.residues).toBe(21);
    // The fit itself is Mol*'s, over every paired position.
    const [theirs] = alignAndSuperpose([reference, moving]);
    expect(found?.rmsd).toBeCloseTo(theirs?.rmsd ?? NaN, 9);
  });
});
