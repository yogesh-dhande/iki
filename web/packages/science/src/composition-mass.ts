import { AVERAGE_H, MASS_1H } from "./molecular-data.js";

/** Neutral Hill formula and masses in u. Supply null for unknown values. ε280 in L/(mol·cm). */
export interface CompositionMass {
  formula: string;
  average: number | null;
  monoisotopic: number | null;
  extinctionCoefficient: number | null;
}

// Pace et al. (1995), folded proteins in water: https://doi.org/10.1002/pro.5560041120
const CYSTINE_EXTINCTION = 125;

/** Oxidizing two thiols to a disulfide removes two H per bond. Keeps isotope labels intact. */
export function withDisulfides(reduced: CompositionMass, count: number): CompositionMass {
  if (!Number.isSafeInteger(count) || count < 0) {
    throw new RangeError("Disulfide count must be a nonnegative integer.");
  }
  const tokens = formulaAtoms(reduced.formula);
  if (!count) return { ...reduced };
  const hydrogen = tokens.find((atom) => atom.symbol === "H");
  if (!hydrogen || hydrogen.count < 2 * count) {
    throw new RangeError("The formula has too few hydrogens for these disulfides.");
  }
  hydrogen.count -= 2 * count;
  return {
    formula: tokens
      .filter((atom) => atom.count > 0)
      .map(({ symbol, count: atoms }) => symbol + (atoms === 1 ? "" : String(atoms)))
      .join(""),
    average: reduced.average === null ? null : reduced.average - 2 * AVERAGE_H * count,
    monoisotopic: reduced.monoisotopic === null ? null : reduced.monoisotopic - 2 * MASS_1H * count,
    extinctionCoefficient:
      reduced.extinctionCoefficient === null
        ? null
        : reduced.extinctionCoefficient + CYSTINE_EXTINCTION * count,
  };
}

/** Beer–Lambert law at 1 g/L with a 1 cm path, A280 = ε280/M (M in g/mol).
 * IUPAC Gold Book: https://doi.org/10.1351/goldbook.B00626 */
export function absorbanceAtOneGramPerLitre(mass: CompositionMass): number | null {
  return mass.extinctionCoefficient === null || mass.average === null || mass.average <= 0
    ? null
    : mass.extinctionCoefficient / mass.average;
}

/** Symbols and positive integer counts, in the order supplied, including D, T and [13C]. */
export function formulaAtoms(formula: string): { symbol: string; count: number }[] {
  const pattern = /(\[\d+[A-Z][a-z]?\]|[A-Z][a-z]?)(\d*)/gy;
  const atoms: { symbol: string; count: number }[] = [];
  const symbols = new Set<string>();
  let read = 0;
  for (const match of formula.matchAll(pattern)) {
    const symbol = match[1] ?? "";
    const count = match[2] ? Number(match[2]) : 1;
    if (count <= 0 || !Number.isSafeInteger(count)) throw new RangeError("Invalid atom count.");
    if (symbols.has(symbol)) throw new RangeError("Each isotope or element must appear once.");
    symbols.add(symbol);
    atoms.push({ symbol, count });
    read += match[0].length;
  }
  if (!read || read !== formula.length)
    throw new RangeError("Expected a neutral molecular formula.");
  return atoms;
}
