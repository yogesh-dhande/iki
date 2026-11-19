/**
 * Residues of a structure in Mol*, as the structure viewer saves and restores them, and how
 * closely two chains superpose: kept apart from the frame's page so they can be tested
 * against Mol* itself.
 *
 * A residue is its chain, its authors' number and its insertion code, as wwPDB defines it
 * (`_atom_site.pdbx_PDB_ins_code`): 35, 35A and 35B are three residues, as in Kabat's
 * numbering of a long CDR. A selection is saved as ranges of residues in each chain's
 * order, and restored by comparing (number, insertion code), so saving 35A restores 35A
 * alone, not 35 to 35B. Where a chain's numbering doesn't rise in its order, as in
 * chymotrypsin numbering (thrombin's light chain in 1PPB runs 1H, 1G, … 1A, 1, 2), a range
 * stops before it would take in a residue that isn't selected: 1 and 2 are saved as 1 and
 * 2, which restores them alone, not 1A to 1H with them.
 */

import { OrderedSet } from "molstar/lib/mol-data/int.js";
import type { Mat4 } from "molstar/lib/mol-math/linear-algebra.js";
import { MinimizeRmsd } from "molstar/lib/mol-math/linear-algebra/3d/minimize-rmsd.js";
import { AlignSequences } from "molstar/lib/mol-model/sequence/alignment/sequence.js";
import {
  StructureElement,
  StructureProperties,
  Structure,
} from "molstar/lib/mol-model/structure.js";
import { getPositionTable } from "molstar/lib/mol-model/structure/structure/util/superposition.js";
import { MolScriptBuilder as MS } from "molstar/lib/mol-script/language/builder.js";
import type { Expression } from "molstar/lib/mol-script/language/expression.js";

/** Residues of a chain, by its authors' numbering, from `start` to `end`, each with its
 * insertion code if it has one. */
export interface Span {
  chain: string;
  start: number;
  end: number;
  start_insertion?: string;
  end_insertion?: string;
}

/** At most this many ranges of residues are kept of a structure's selection. */
export const MAX_SPANS = 500;

interface Residue {
  chain: string;
  number: number;
  insertion: string;
  /** Its place in the model, so neighbours in a chain are next to each other. */
  index: number;
}

/** Whether residue `a` comes before `b` in a chain's numbering: by number, then code. */
function before(a: { number: number; insertion: string }, b: typeof a): boolean {
  return a.number < b.number || (a.number === b.number && a.insertion < b.insertion);
}

/** Whether residue `r` is from `start` to `end` in the numbering, as `spanTest` finds it. */
function within(r: Residue, start: Residue, end: Residue): boolean {
  return !before(r, start) && !before(end, r);
}

/** Each chain's residues, in its order, where its numbering doesn't rise along it: only
 * there can a range of numbers take in residues outside the stretch it was saved from. */
function unorderedChains(structure: Structure): Map<string, Residue[]> {
  const chains = new Map<string, Map<number, Residue>>();
  StructureElement.Loci.forEachLocation(Structure.toStructureElementLoci(structure), (location) => {
    const chain = StructureProperties.chain.auth_asym_id(location);
    const index = StructureProperties.residue.key(location);
    const residues = chains.get(chain) ?? new Map<number, Residue>();
    chains.set(chain, residues);
    if (residues.has(index)) return;
    residues.set(index, {
      chain,
      number: StructureProperties.residue.auth_seq_id(location),
      insertion: StructureProperties.residue.pdbx_PDB_ins_code(location).trim(),
      index,
    });
  });
  const out = new Map<string, Residue[]>();
  for (const [chain, residues] of chains) {
    const ordered = [...residues.values()].sort((a, b) => a.index - b.index);
    if (ordered.some((r, i) => i > 0 && !before(ordered[i - 1] ?? r, r))) {
      out.set(chain, ordered);
    }
  }
  return out;
}

/** The residues a selection covers, as ranges: a range runs on while the next residue
 * selected is the next of its chain, comes after it in the numbering, and no residue
 * outside the range's stretch of the chain falls in its numbers. */
export function spansOf(loci: StructureElement.Loci): Span[] {
  const seen = new Map<string, Residue>();
  StructureElement.Loci.forEachLocation(loci, (location) => {
    const residue = {
      chain: StructureProperties.chain.auth_asym_id(location),
      number: StructureProperties.residue.auth_seq_id(location),
      insertion: StructureProperties.residue.pdbx_PDB_ins_code(location).trim(),
      index: StructureProperties.residue.key(location),
    };
    seen.set(`${residue.chain}\u0000${String(residue.index)}`, residue);
  });
  const residues = [...seen.values()].sort((a, b) =>
    a.chain === b.chain ? a.index - b.index : a.chain < b.chain ? -1 : 1,
  );
  const unordered = unorderedChains(loci.structure);
  /** Whether a range from `first` to `last` would take in a residue outside them. */
  const strays = (first: Residue, last: Residue) =>
    (unordered.get(first.chain) ?? []).some(
      (r) => (r.index < first.index || r.index > last.index) && within(r, first, last),
    );
  const runs: { first: Residue; last: Residue }[] = [];
  for (const residue of residues) {
    const run = runs[runs.length - 1];
    if (
      run?.last.chain === residue.chain &&
      run.last.index === residue.index - 1 &&
      before(run.last, residue) &&
      !strays(run.first, residue)
    ) {
      run.last = residue;
    } else {
      runs.push({ first: residue, last: residue });
    }
  }
  return runs.slice(0, MAX_SPANS).map(({ first, last }) => ({
    chain: first.chain,
    start: first.number,
    ...(first.insertion ? { start_insertion: first.insertion } : {}),
    end: last.number,
    ...(last.insertion ? { end_insertion: last.insertion } : {}),
  }));
}

/** A MolScript test for the residues of a span: (number, code) from its start's to its
 * end's. A span with no codes is its numbers' residues without one, and those between. */
export function spanTest(span: Span): Expression {
  const number = MS.struct.atomProperty.macromolecular.auth_seq_id();
  const code = MS.struct.atomProperty.macromolecular.pdbx_PDB_ins_code();
  const fromStart = MS.core.logic.or([
    MS.core.rel.gr([number, span.start]),
    MS.core.logic.and([
      MS.core.rel.eq([number, span.start]),
      MS.core.rel.gre([code, span.start_insertion ?? ""]),
    ]),
  ]);
  const toEnd = MS.core.logic.or([
    MS.core.rel.lt([number, span.end]),
    MS.core.logic.and([
      MS.core.rel.eq([number, span.end]),
      MS.core.rel.lte([code, span.end_insertion ?? ""]),
    ]),
  ]);
  return MS.struct.generator.atomGroups({
    "chain-test": MS.core.rel.eq([
      MS.struct.atomProperty.macromolecular.auth_asym_id(),
      span.chain,
    ]),
    "residue-test": MS.core.logic.and([fromStart, toEnd]),
  });
}

/** How one chain superposes on another, as Mol*'s alignAndSuperpose does it: residues
 * paired by aligning their sequences (BLOSUM62 for proteins), then the RMSD over the
 * pairs. `residues` is how many residues the fit paired, each once, however many
 * alternate locations its atom has (Ile11 A and B in 4AR3 are one), not either chain's
 * length. */
export interface Fit {
  rmsd: number;
  bTransform: Mat4;
  residues: number;
}

const PROTEIN = /(polypeptide|cyclic-pseudo-peptide)/i;

export function fit(reference: StructureElement.Loci, moving: StructureElement.Loci): Fit | null {
  const [first] = reference.elements;
  const [second] = moving.elements;
  if (!first || !second) return null;
  const location = StructureElement.Loci.getFirstLocation(reference);
  const subtype = location ? StructureProperties.entity.subtype(location) : "";
  const { a, b } = AlignSequences.compute(
    { a: first, b: second },
    { substMatrix: PROTEIN.test(subtype) ? "blosum62" : "default" },
  );
  const pairs = OrderedSet.size(a.indices);
  if (!pairs) return null;
  const done = MinimizeRmsd.compute({
    a: getPositionTable(StructureElement.Loci(reference.structure, [a]), pairs),
    b: getPositionTable(StructureElement.Loci(moving.structure, [b]), pairs),
  });
  const residues = Math.min(residuesOf(reference.structure, a), residuesOf(moving.structure, b));
  return { rmsd: done.rmsd, bTransform: done.bTransform, residues };
}

/** How many residues an element's atoms are of: an atom's alternate locations are one. */
function residuesOf(structure: Structure, element: StructureElement.Loci["elements"][number]) {
  const seen = new Set<number>();
  StructureElement.Loci.forEachLocation(StructureElement.Loci(structure, [element]), (location) => {
    seen.add(StructureProperties.residue.key(location));
  });
  return seen.size;
}

/** The structure a selection's loci are of, for tests and callers that need it. */
export type { Structure };
