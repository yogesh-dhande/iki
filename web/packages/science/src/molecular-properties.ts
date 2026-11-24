import { formulaAtoms, withDisulfides, type CompositionMass } from "./composition-mass.js";
import {
  AVERAGE,
  DEOXYNUCLEOTIDES,
  DOUBLETS,
  ISOTOPES,
  MONOISOTOPIC,
  RESIDUES,
  SINGLES,
} from "./molecular-data.js";

/** Unrounded masses of a neutral molecule, in unified atomic mass units (u). */
export interface MolecularMass {
  average: number;
  monoisotopic: number;
}

function isotopeMass(symbol: string): number | undefined {
  if (symbol === "D") return ISOTOPES["[2H]"];
  if (symbol === "T") return ISOTOPES["[3H]"];
  if (ISOTOPES[symbol] !== undefined) return ISOTOPES[symbol];
  const match = /^\[([1-9][0-9]*)([A-Z][a-z]?)\]$/.exec(symbol);
  if (!match?.[2]) return undefined;
  const abundant = MONOISOTOPIC[match[2]];
  return abundant !== undefined && Math.round(abundant) === Number(match[1]) ? abundant : undefined;
}

/** Both masses are sums of neutral atom masses. A fixed isotope contributes the same mass
 * to both, while an unlabelled atom contributes its standard weight or most abundant isotope. */
export function molecularMass(formula: string): MolecularMass {
  let average = 0;
  let monoisotopic = 0;
  for (const { symbol, count } of formulaAtoms(formula)) {
    const labelled = isotopeMass(symbol);
    const natural = labelled ?? AVERAGE[symbol];
    const mono = labelled ?? MONOISOTOPIC[symbol];
    if (natural === undefined || mono === undefined)
      throw new RangeError("Unknown atom or isotope: " + symbol);
    average += natural * count;
    monoisotopic += mono * count;
  }
  return { average, monoisotopic };
}

// Accumulate repeated residues first so parsing an atom formula costs once per kind.
function polymerFormula(
  units: ReadonlyMap<string, number>,
  formulas: Readonly<Record<string, string>>,
  water: number,
): string {
  const atoms = new Map<string, number>([
    ["H", 2 * water],
    ["O", water],
  ]);
  for (const [unit, count] of units) {
    const formula = formulas[unit];
    if (!formula) throw new RangeError("No unique residue formula for " + unit);
    for (const atom of formulaAtoms(formula))
      atoms.set(atom.symbol, (atoms.get(atom.symbol) ?? 0) + count * atom.count);
  }
  // C, H, then the remaining symbols in alphabetical order for these carbon compounds.
  return [...atoms]
    .sort(([a], [b]) => a.localeCompare(b, "en"))
    .filter(([, n]) => n > 0)
    .map(([symbol, n]) => symbol + (n === 1 ? "" : String(n)))
    .join("");
}

function counts(sequence: string): Map<string, number> {
  const out = new Map<string, number>();
  for (const residue of sequence) out.set(residue, (out.get(residue) ?? 0) + 1);
  return out;
}

/** Free termini, unmodified residues and zero disulfides by default. Supply the observed
 * disulfide count to adjust both mass and ε280. Pace et al. (1995), folded proteins in water:
 * https://doi.org/10.1002/pro.5560041120 . Sec and Pyl have known masses but no ε in this model. */
export function proteinProperties(sequence: string, disulfides = 0): CompositionMass {
  const residues = sequence.toUpperCase().replace(/\*+$/, "");
  if (!residues.length) throw new RangeError("Expected a peptide with at least one residue.");
  const units = counts(residues);
  if (2 * disulfides > (units.get("C") ?? 0))
    throw new RangeError("Not enough cysteines for the specified bonds.");
  const formula = polymerFormula(units, RESIDUES, 1);
  const extinctionCoefficient =
    units.has("U") || units.has("O")
      ? null
      : (units.get("W") ?? 0) * 5500 + (units.get("Y") ?? 0) * 1490;
  return withDisulfides({ formula, ...molecularMass(formula), extinctionCoefficient }, disulfides);
}

/** Each linear DNA strand has a 5′ phosphate and a 3′ hydroxyl. Sum its nucleotide
 * monophosphates and subtract one H₂O per phosphodiester bond. A closed strand has
 * one more bond. Both strands of a duplex are counted, with neutral free-acid chemistry. */
export function dnaProperties(
  sequence: string,
  options: { circular?: boolean; doubleStranded?: boolean } = {},
): MolecularMass & { formula: string } {
  const bases = sequence.toUpperCase();
  if (!/^[ACGT]+$/.test(bases)) throw new RangeError("Expected unambiguous DNA bases.");
  const units = counts(bases);
  if (options.doubleStranded) {
    const complements: Readonly<Record<string, string>> = { A: "T", T: "A", G: "C", C: "G" };
    for (const [base, count] of [...units]) {
      const paired = complements[base] ?? "";
      units.set(paired, (units.get(paired) ?? 0) + count);
    }
  }
  const bonds = (bases.length - (options.circular ? 0 : 1)) * (options.doubleStranded ? 2 : 1);
  const formula = polymerFormula(units, DEOXYNUCLEOTIDES, -bonds);
  return { formula, ...molecularMass(formula) };
}

/** ε260 in L/(mol·cm) for a natural, linear, single strand read 5′→3′. Sum the
 * doublets and deduct the internal nucleotides counted twice. DNA: Cantor et al. (1970).
 * RNA: Puglisi & Tinoco (1989). The primary data and units are cited in molecular-data. */
export function strandExtinction260(sequence: string, kind: "DNA" | "RNA"): number {
  const bases = sequence.toUpperCase();
  if (!(kind === "DNA" ? /^[ACGT]*$/ : /^[ACGU]*$/).test(bases) || !["DNA", "RNA"].includes(kind)) {
    throw new RangeError("Expected an unmodified DNA or RNA strand with unambiguous bases.");
  }
  const singles = SINGLES[kind];
  if (bases.length < 2) return singles[bases] ?? 0;
  const pairs = DOUBLETS[kind];
  let coefficient = 0;
  for (let i = 1; i < bases.length; i++) {
    coefficient += pairs[bases.slice(i - 1, i + 1)] ?? 0;
    if (i + 1 < bases.length) coefficient -= singles[bases.charAt(i)] ?? 0;
  }
  return coefficient;
}
