/**
 * Residues of a structure in Mol*, as the structure viewer saves and restores them, and how
 * closely two chains superpose: kept apart from the frame's page so they can be tested
 * against Mol* itself.
 *
 * A residue is its chain, its authors' number and its insertion code, as wwPDB defines it
 * (`_atom_site.pdbx_PDB_ins_code`): 35, 35A and 35B are three residues, as in Kabat's
 * numbering of a long CDR. A selection is saved as ranges of residues in each chain's
 * order, and restored by comparing (number, insertion code), so saving 35A restores 35A
 * alone, not 35 to 35B.
 */

import { OrderedSet } from "molstar/lib/mol-data/int.js";
import type { Mat4 } from "molstar/lib/mol-math/linear-algebra.js";
import { MinimizeRmsd } from "molstar/lib/mol-math/linear-algebra/3d/minimize-rmsd.js";
import { AlignSequences } from "molstar/lib/mol-model/sequence/alignment/sequence.js";
import {
  StructureElement,
  StructureProperties,
  type Structure,
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

/** The residues a selection covers, as ranges: a range runs on while the next residue
 * selected is the next of its chain and comes after it in the numbering. */
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
  const spans: (Span & { last: Residue })[] = [];
  for (const residue of residues) {
    const span = spans[spans.length - 1];
    if (
      span?.chain === residue.chain &&
      span.last.index === residue.index - 1 &&
      before(span.last, residue)
    ) {
      span.last = residue;
    } else {
      spans.push({
        chain: residue.chain,
        start: residue.number,
        end: residue.number,
        last: residue,
      });
      const made = spans[spans.length - 1];
      if (made && residue.insertion) made.start_insertion = residue.insertion;
    }
  }
  return spans.slice(0, MAX_SPANS).map(({ last, ...span }) => ({
    ...span,
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
 * pairs. `residues` is how many pairs the fit used, not either chain's length. */
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
  return { rmsd: done.rmsd, bTransform: done.bTransform, residues: pairs };
}

/** The structure a selection's loci are of, for tests and callers that need it. */
export type { Structure };
