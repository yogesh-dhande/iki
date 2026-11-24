/**
 * NCBI's genetic codes, and the amino acids a coding sequence or a reading frame reads as,
 * for the sequence viewer's translation lanes. Pure, so it's tested without a page.
 *
 * The tables are NCBI's (https://www.ncbi.nlm.nih.gov/Taxonomy/Utils/wprintgc.cgi, the
 * `ncbieaa` and `sncbieaa` lines of its gc.prt), as Biopython's Bio.Data.CodonTable holds
 * them and the plugins translate by: each table's amino acid for the 64 codons in NCBI's
 * order (first base T, C, A, G, then the second, then the third), and `M` where the codon
 * may start a protein. In tables 27, 28 and 31 some codons are a stop at a coding
 * sequence's end and an amino acid elsewhere (`dual`); `residues` gives the amino acid.
 */

import type { Feature } from "./sequence.js";

export interface GeneticCode {
  name: string;
  residues: string;
  starts: string;
  dual: string[];
}

export const GENETIC_CODES: Record<number, GeneticCode> = {
  1: {
    name: "Standard",
    residues: "FFLLSSSSYY**CC*WLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG",
    starts: "---M---------------M---------------M----------------------------",
    dual: [],
  },
  2: {
    name: "Vertebrate Mitochondrial",
    residues: "FFLLSSSSYY**CCWWLLLLPPPPHHQQRRRRIIMMTTTTNNKKSS**VVVVAAAADDEEGGGG",
    starts: "--------------------------------MMMM---------------M------------",
    dual: [],
  },
  3: {
    name: "Yeast Mitochondrial",
    residues: "FFLLSSSSYY**CCWWTTTTPPPPHHQQRRRRIIMMTTTTNNKKSSRRVVVVAAAADDEEGGGG",
    starts: "----------------------------------MM---------------M------------",
    dual: [],
  },
  4: {
    name: "Mold Mitochondrial, Protozoan Mitochondrial, Coelenterate Mitochondrial, Mycoplasma, Spiroplasma",
    residues: "FFLLSSSSYY**CCWWLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG",
    starts: "--MM---------------M------------MMMM---------------M------------",
    dual: [],
  },
  5: {
    name: "Invertebrate Mitochondrial",
    residues: "FFLLSSSSYY**CCWWLLLLPPPPHHQQRRRRIIMMTTTTNNKKSSSSVVVVAAAADDEEGGGG",
    starts: "---M----------------------------MMMM---------------M------------",
    dual: [],
  },
  6: {
    name: "Ciliate Nuclear, Dasycladacean Nuclear, Hexamita Nuclear",
    residues: "FFLLSSSSYYQQCC*WLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG",
    starts: "-----------------------------------M----------------------------",
    dual: [],
  },
  9: {
    name: "Echinoderm Mitochondrial, Flatworm Mitochondrial",
    residues: "FFLLSSSSYY**CCWWLLLLPPPPHHQQRRRRIIIMTTTTNNNKSSSSVVVVAAAADDEEGGGG",
    starts: "-----------------------------------M---------------M------------",
    dual: [],
  },
  10: {
    name: "Euplotid Nuclear",
    residues: "FFLLSSSSYY**CCCWLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG",
    starts: "-----------------------------------M----------------------------",
    dual: [],
  },
  11: {
    name: "Bacterial, Archaeal, Plant Plastid",
    residues: "FFLLSSSSYY**CC*WLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG",
    starts: "---M---------------M------------MMMM---------------M------------",
    dual: [],
  },
  12: {
    name: "Alternative Yeast Nuclear",
    residues: "FFLLSSSSYY**CC*WLLLSPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG",
    starts: "-------------------M---------------M----------------------------",
    dual: [],
  },
  13: {
    name: "Ascidian Mitochondrial",
    residues: "FFLLSSSSYY**CCWWLLLLPPPPHHQQRRRRIIMMTTTTNNKKSSGGVVVVAAAADDEEGGGG",
    starts: "---M------------------------------MM---------------M------------",
    dual: [],
  },
  14: {
    name: "Alternative Flatworm Mitochondrial",
    residues: "FFLLSSSSYYY*CCWWLLLLPPPPHHQQRRRRIIIMTTTTNNNKSSSSVVVVAAAADDEEGGGG",
    starts: "-----------------------------------M----------------------------",
    dual: [],
  },
  15: {
    name: "Blepharisma Macronuclear",
    residues: "FFLLSSSSYY*QCC*WLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG",
    starts: "-----------------------------------M----------------------------",
    dual: [],
  },
  16: {
    name: "Chlorophycean Mitochondrial",
    residues: "FFLLSSSSYY*LCC*WLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG",
    starts: "-----------------------------------M----------------------------",
    dual: [],
  },
  21: {
    name: "Trematode Mitochondrial",
    residues: "FFLLSSSSYY**CCWWLLLLPPPPHHQQRRRRIIMMTTTTNNNKSSSSVVVVAAAADDEEGGGG",
    starts: "-----------------------------------M---------------M------------",
    dual: [],
  },
  22: {
    name: "Scenedesmus obliquus Mitochondrial",
    residues: "FFLLSS*SYY*LCC*WLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG",
    starts: "-----------------------------------M----------------------------",
    dual: [],
  },
  23: {
    name: "Thraustochytrium Mitochondrial",
    residues: "FF*LSSSSYY**CC*WLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG",
    starts: "--------------------------------M--M---------------M------------",
    dual: [],
  },
  24: {
    name: "Pterobranchia Mitochondrial",
    residues: "FFLLSSSSYY**CCWWLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSSKVVVVAAAADDEEGGGG",
    starts: "---M---------------M---------------M---------------M------------",
    dual: [],
  },
  25: {
    name: "Candidate Division SR1, Gracilibacteria",
    residues: "FFLLSSSSYY**CCGWLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG",
    starts: "---M-------------------------------M---------------M------------",
    dual: [],
  },
  26: {
    name: "Pachysolen tannophilus Nuclear",
    residues: "FFLLSSSSYY**CC*WLLLAPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG",
    starts: "-------------------M---------------M----------------------------",
    dual: [],
  },
  27: {
    name: "Karyorelict Nuclear",
    residues: "FFLLSSSSYYQQCCWWLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG",
    starts: "-----------------------------------M----------------------------",
    dual: ["TGA"],
  },
  28: {
    name: "Condylostoma Nuclear",
    residues: "FFLLSSSSYYQQCCWWLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG",
    starts: "-----------------------------------M----------------------------",
    dual: ["TAA", "TAG", "TGA"],
  },
  29: {
    name: "Mesodinium Nuclear",
    residues: "FFLLSSSSYYYYCC*WLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG",
    starts: "-----------------------------------M----------------------------",
    dual: [],
  },
  30: {
    name: "Peritrich Nuclear",
    residues: "FFLLSSSSYYEECC*WLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG",
    starts: "-----------------------------------M----------------------------",
    dual: [],
  },
  31: {
    name: "Blastocrithidia Nuclear",
    residues: "FFLLSSSSYYEECCWWLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG",
    starts: "-----------------------------------M----------------------------",
    dual: ["TAA", "TAG"],
  },
  32: {
    name: "Balanophoraceae Plastid",
    residues: "FFLLSSSSYY*WCC*WLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG",
    starts: "---M---------------M------------MMMM---------------M------------",
    dual: [],
  },
  33: {
    name: "Cephalodiscidae Mitochondrial",
    residues: "FFLLSSSSYYY*CCWWLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSSKVVVVAAAADDEEGGGG",
    starts: "---M---------------M---------------M---------------M------------",
    dual: [],
  },
};

/** The codes a reading frame can be read by: not 27, 28 and 31, whose stops the bases
 * alone don't settle (as the plasmid plugin's ORF finder refuses them). */
export const FRAME_CODES = Object.keys(GENETIC_CODES)
  .map(Number)
  .filter((n) => !GENETIC_CODES[n]?.dual.length);

const ORDER = "TCAG";

/** The bases each IUPAC nucleotide code stands for (IUBMB, 1985), U read as T. */
const READINGS: Record<string, string> = {
  A: "A",
  C: "C",
  G: "G",
  T: "T",
  U: "T",
  R: "AG",
  Y: "CT",
  S: "CG",
  W: "AT",
  K: "GT",
  M: "AC",
  B: "CGT",
  D: "AGT",
  H: "ACT",
  V: "ACG",
  N: "ACGT",
};

/** The complement of a base, U and the ambiguity codes too. */
const PAIRS: Record<string, string> = {
  A: "T",
  C: "G",
  G: "C",
  T: "A",
  U: "A",
  R: "Y",
  Y: "R",
  K: "M",
  M: "K",
  B: "V",
  V: "B",
  D: "H",
  H: "D",
  S: "S",
  W: "W",
  N: "N",
};

/** The amino acids one ambiguity code stands for (IUPAC-IUB), as the alignment viewer
 * compares residues: B for D or N, Z for E or Q, J for I or L. */
const AMBIGUOUS: [string, string][] = [
  ["B", "DN"],
  ["Z", "EQ"],
  ["J", "IL"],
];

/** What a codon reads as in `code`: its amino acid, `*` for a stop, `M` for a start codon
 * at a protein's start (an initiator tRNA reads any start codon as methionine), and a stop
 * for a dual codon at a coding sequence's end. A codon with ambiguous bases reads as what
 * every codon it could be reads as, or as B, Z or J where those are all it could be, or X. */
export function residueOf(
  codon: string,
  code: GeneticCode,
  at: { start: boolean; end: boolean } = { start: false, end: false },
): string {
  const found = new Set<string>();
  const [a, b, c] = Array.from(codon.toUpperCase(), (base) => READINGS[base] ?? "");
  if (codon.length !== 3 || !a || !b || !c) return "X";
  for (const x of a) {
    for (const y of b) {
      for (const z of c) {
        const index = ORDER.indexOf(x) * 16 + ORDER.indexOf(y) * 4 + ORDER.indexOf(z);
        const reading = x + y + z;
        if (at.start && code.starts.charAt(index) === "M") found.add("M");
        else if (at.end && code.dual.includes(reading)) found.add("*");
        else found.add(code.residues.charAt(index));
      }
    }
  }
  if (found.size === 1) return [...found][0] ?? "X";
  for (const [letter, pair] of AMBIGUOUS) {
    if ([...found].every((r) => pair.includes(r))) return letter;
  }
  return "X";
}

/** An amino acid as INSDC's /transl_except names it (three letters, TERM, OTHER). */
const THREE_LETTERS: Record<string, string> = {
  Ala: "A",
  Arg: "R",
  Asn: "N",
  Asp: "D",
  Cys: "C",
  Gln: "Q",
  Glu: "E",
  Gly: "G",
  His: "H",
  Ile: "I",
  Leu: "L",
  Lys: "K",
  Met: "M",
  Phe: "F",
  Pro: "P",
  Ser: "S",
  Thr: "T",
  Trp: "W",
  Tyr: "Y",
  Val: "V",
  Sec: "U",
  Pyl: "O",
  Asx: "B",
  Glx: "Z",
  Xle: "J",
  Xaa: "X",
  OTHER: "X",
  TERM: "*",
};

/** A coding sequence's /transl_except qualifiers: the bases (from 1) each names, and what
 * they read as, such as a stop codon completed by polyadenylation (`(pos:4261..4262,aa:TERM)`)
 * or a selenocysteine (`aa:Sec`). Several, as the import joins them, are each read. */
export function exceptionsOf(
  value: string | undefined,
): { from: number; to: number; residue: string }[] {
  if (!value) return [];
  const out = [];
  const pattern = /\(pos:(?:complement\()?(\d+)(?:\.\.(\d+))?\)?,aa:([A-Za-z]+)\)/g;
  for (const [, from, to, aa] of value.matchAll(pattern)) {
    const residue = THREE_LETTERS[aa ?? ""];
    if (!residue || !from) continue;
    const a = Number(from);
    const b = to ? Number(to) : a;
    out.push({ from: Math.min(a, b), to: Math.max(a, b), residue });
  }
  return out;
}

/** One codon as a lane draws it: what it reads as, its bases on the sequence (from 1, in the
 * order they're read), and its place in the protein, from 1. */
export interface Codon {
  residue: string;
  /** Its bases as read, 5′ to 3′ on its strand: three, or fewer for a stop completed off the
   * sequence (a /transl_except). */
  codon: string;
  bases: number[];
  number: number;
}

/** A coding sequence's translation: its codons, and the table it was read by. */
export interface Translation {
  table: number;
  codons: Codon[];
}

/** The table a coding sequence is read by: its /transl_table, or 1, NCBI's standard code. */
export function tableOf(feature: Feature): number {
  const given = feature.qualifiers["transl_table"];
  return given === undefined ? 1 : Number(given.trim());
}

/** A coding sequence read as INSDC says to: its parts in the order kept (a reverse-strand
 * one's bases complemented and read from its end), from the base its /codon_start names,
 * by its /transl_table, with its /transl_except. A 5′ partial one (`<`) reads from its first
 * whole codon and takes no start codon as methionine; a 3′ partial one ends no protein, so a
 * dual codon at its end isn't a stop. A last codon of fewer than three bases is left out,
 * unless a /transl_except says what it is, or it's at a 3′ partial end and its bases settle
 * it whatever the rest would be. Or why it can't be read. */
export function translateFeature(
  feature: Feature,
  sequence: string,
): Translation | { error: string } {
  const table = tableOf(feature);
  const code = GENETIC_CODES[table];
  if (!code)
    return { error: `NCBI has no genetic code ${feature.qualifiers["transl_table"] ?? ""}` };
  const length = sequence.length;
  const bases: number[] = [];
  let read = "";
  const parts = feature.parts.filter((p) => !p.between);
  for (const part of parts) {
    const stretch: number[] = [];
    const ends =
      part.end >= part.start
        ? [[part.start, part.end]]
        : [
            [part.start, length],
            [1, part.end],
          ];
    for (const [a = 0, b = 0] of ends) for (let at = a; at <= b; at++) stretch.push(at);
    const reverse = part.strand === "reverse";
    if (reverse) stretch.reverse();
    for (const at of stretch) {
      const base = sequence.charAt(at - 1).toUpperCase();
      bases.push(at);
      read += reverse ? (PAIRS[base] ?? base) : base;
    }
  }
  const startGiven = Number(feature.qualifiers["codon_start"] ?? "1");
  const offset = [1, 2, 3].includes(startGiven) ? startGiven - 1 : 0;
  const first = parts[0];
  const last = parts.at(-1);
  const complete5 = !first?.partialStart && offset === 0;
  const complete3 = !last?.partialEnd;
  const exceptions = exceptionsOf(feature.qualifiers["transl_except"]);
  const excepted = (at: number[]) =>
    exceptions.find((e) => at.some((b) => b >= e.from && b <= e.to))?.residue;
  const codons: Codon[] = [];
  const whole = Math.floor((read.length - offset) / 3);
  for (let k = 0; k < whole; k++) {
    const i = offset + 3 * k;
    const codon = read.slice(i, i + 3);
    const at = bases.slice(i, i + 3);
    const residue =
      excepted(at) ??
      residueOf(codon, code, { start: k === 0 && complete5, end: k === whole - 1 && complete3 });
    codons.push({ residue, codon, bases: at, number: k + 1 });
  }
  const tail = offset + 3 * whole;
  if (tail < read.length) {
    const at = bases.slice(tail);
    // At a 3′ partial end, the bases there may settle it whatever follows (CT is L in the
    // standard code), and NCBI's /translation then gives it.
    const settled = complete3 ? "X" : residueOf(read.slice(tail).padEnd(3, "N"), code);
    const residue = excepted(at) ?? (settled === "X" ? undefined : settled);
    if (residue) codons.push({ residue, codon: read.slice(tail), bases: at, number: whole + 1 });
  }
  return { table, codons };
}

/** The reading frames a lane can show: +1, +2 and +3 read the forward strand from its
 * first, second and third base; −1, −2 and −3 the reverse strand from its first, second and
 * third, which are the forward strand's last, last but one and last but two. */
export type Frame = 1 | 2 | 3 | -1 | -2 | -3;
export const FRAMES: Frame[] = [1, 2, 3, -1, -2, -3];

/** A reading frame's codons, by `table`, each as it reads there: no start codon is taken as
 * methionine, as nothing says a protein starts there. Along the sequence as it's numbered,
 * not round a circle's origin. */
export function translateFrame(sequence: string, frame: Frame, table: number): Codon[] {
  const code = GENETIC_CODES[table];
  if (!code) return [];
  const length = sequence.length;
  const out: Codon[] = [];
  const reverse = frame < 0;
  const skip = Math.abs(frame) - 1;
  for (let k = 0; skip + 3 * k + 3 <= length; k++) {
    const at = [0, 1, 2].map((j) => (reverse ? length - (skip + 3 * k + j) : skip + 3 * k + j + 1));
    let codon = "";
    for (const b of at) {
      const base = sequence.charAt(b - 1).toUpperCase();
      codon += reverse ? (PAIRS[base] ?? base) : base;
    }
    out.push({ residue: residueOf(codon, code), codon, bases: at, number: k + 1 });
  }
  return out;
}

/** A translation as one-letter codes, a stop ending it left off: as GenBank's /translation
 * writes a coding sequence's. */
export function proteinOf(codons: Codon[]): string {
  return codons
    .map((c) => c.residue)
    .join("")
    .replace(/\*$/, "");
}
