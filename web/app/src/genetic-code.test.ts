/** Translation lanes: NCBI's codes, and coding sequences read as GenBank reads them. */

import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import {
  exceptionsOf,
  FRAME_CODES,
  GENETIC_CODES,
  proteinOf,
  residueOf,
  translateFeature,
  translateFrame,
} from "./genetic-code";
import { readFeatures, type Feature } from "./sequence";

/** A file of the repository's, from web/app (as pnpm runs the tests) or from its root. */
function repositoryFile(path: string): string {
  const here = join(process.cwd(), "..", "..", path);
  return readFileSync(existsSync(here) ? here : path, "utf8");
}

interface Part {
  start: number;
  end: number;
  strand: "forward" | "reverse";
  partial_start?: boolean;
  partial_end?: boolean;
}

/** An INSDC location's parts in the order they're read, as the DNA plugin's import writes
 * them (plugins/dna/scripts/imports.py `parts_of`): a complement's last first. */
function partsOf(location: string, strand: "forward" | "reverse" = "forward"): Part[] {
  const text = location.trim();
  for (const wrapper of ["complement", "join", "order"]) {
    if (text.startsWith(`${wrapper}(`) && text.endsWith(")")) {
      const inner = text.slice(wrapper.length + 1, -1);
      if (wrapper === "complement") {
        return partsOf(inner, strand === "forward" ? "reverse" : "forward").reverse();
      }
      const pieces: string[] = [];
      let depth = 0;
      let from = 0;
      for (const [i, c] of Array.from(inner).entries()) {
        if (c === "(") depth++;
        if (c === ")") depth--;
        if (c === "," && depth === 0) {
          pieces.push(inner.slice(from, i));
          from = i + 1;
        }
      }
      pieces.push(inner.slice(from));
      return pieces.flatMap((piece) => partsOf(piece, strand));
    }
  }
  const range = /^(<)?(\d+)\.\.(>)?(\d+)$/.exec(text);
  if (range) {
    return [
      {
        start: Number(range[2]),
        end: Number(range[4]),
        strand,
        ...(range[1] ? { partial_start: true } : {}),
        ...(range[3] ? { partial_end: true } : {}),
      },
    ];
  }
  const base = /^([<>])?(\d+)$/.exec(text);
  if (!base) throw new Error(`Can't read ${text}`);
  const at = Number(base[2]);
  return [{ start: at, end: at, strand }];
}

/** A GenBank record's bases and its coding sequences, as the viewer reads them after the
 * DNA plugin's import: each with its qualifiers. */
function record(path: string): { bases: string; cds: readonly Feature[] } {
  const text = repositoryFile(path);
  const [head = "", origin = ""] = text.split(/^ORIGIN.*$/m);
  const bases = origin.replace(/[^a-zA-Z]/g, "").toUpperCase();
  const table = head.split(/^FEATURES.*$/m)[1] ?? "";
  const raw: { key: string; location: string; qualifiers: string[] }[] = [];
  for (const line of table.split("\n")) {
    if (/^ {5}\S/.test(line)) {
      raw.push({ key: line.slice(5, 21).trim(), location: line.slice(21).trim(), qualifiers: [] });
      continue;
    }
    const body = line.slice(21);
    const current = raw.at(-1);
    if (!current || !line.startsWith(" ".repeat(21))) continue;
    if (body.startsWith("/")) current.qualifiers.push(body);
    else if (current.qualifiers.length) {
      const last = current.qualifiers.pop() ?? "";
      const joint = last.startsWith("/translation") ? "" : " ";
      current.qualifiers.push(last + joint + body.trim());
    } else current.location += body.trim();
  }
  const features = raw
    .filter((f) => f.key === "CDS")
    .map((f) => {
      const qualifiers: Record<string, string> = {};
      for (const item of f.qualifiers) {
        const [name = "", ...rest] = item.slice(1).split("=");
        let value = rest.join("=");
        if (value.startsWith('"') && value.endsWith('"')) value = value.slice(1, -1);
        qualifiers[name] = qualifiers[name] ? `${qualifiers[name]}; ${value}` : value;
      }
      const parts = partsOf(f.location);
      const ends = parts.flatMap((p) => [p.start, p.end]);
      return {
        type: "CDS",
        start: Math.min(...ends),
        end: Math.max(...ends),
        strand: parts[0]?.strand ?? "forward",
        parts,
        qualifiers,
      };
    });
  return { bases, cds: readFeatures(features, bases.length) };
}

const RECORDS = [
  // Phage lambda's 66, by table 11, some from GTG or TTG.
  "server/tests/data/genbank/J02459.1.gb",
  // The human mitochondrion's 13, by table 2, with stops completed by polyadenylation.
  "server/tests/data/genbank/NC_012920.1.gb",
  "server/tests/data/genbank/J00265.1.gb",
  "server/tests/data/genbank/D10085.1.gb",
  "server/tests/data/genbank/PZ465884.1.gb",
  "server/tests/data/genbank/PZ465879.1.gb",
  "server/tests/data/genbank/PP826089.1.gb",
  "server/tests/data/plasmids/J01749.1.gb",
  "server/tests/data/plasmids/LT009457.1.gb",
  "server/tests/data/plasmids/MN996867.1.gb",
  "server/tests/data/plasmids/OL989372.1.gb",
  "server/tests/data/plasmids/X01095.1.gb",
];

describe("a coding sequence's translation", () => {
  it.each(RECORDS)("is what %s's own /translation says", (path) => {
    const { bases, cds } = record(path);
    const stated = cds.filter((f) => f.qualifiers["translation"]);
    expect(stated.length).toBeGreaterThan(0);
    for (const feature of stated) {
      const read = translateFeature(feature, bases);
      if ("error" in read) throw new Error(read.error);
      expect([
        feature.qualifiers["gene"] ?? feature.qualifiers["product"],
        proteinOf(read.codons),
      ]).toEqual([
        feature.qualifiers["gene"] ?? feature.qualifiers["product"],
        feature.qualifiers["translation"],
      ]);
    }
  });

  it("reads a join across its intron, and says which bases each codon is", () => {
    // The human insulin gene: join(2424..2610,3397..3542); codon 63 spans the intron.
    const { bases, cds } = record("server/tests/data/genbank/J00265.1.gb");
    const [insulin] = cds;
    if (!insulin) throw new Error("No CDS");
    const read = translateFeature(insulin, bases);
    if ("error" in read) throw new Error(read.error);
    expect(read.table).toBe(1);
    expect(read.codons[0]).toMatchObject({ residue: "M", bases: [2424, 2425, 2426] });
    expect(read.codons[62]?.bases).toEqual([2610, 3397, 3398]);
    expect(read.codons.at(-1)).toMatchObject({ residue: "*", number: 111 });
  });

  it("reads a stop a /transl_except completes, from the bases there are", () => {
    // The human mitochondrion's ND1 ends 4261..4262 with TA, made TAA by polyadenylation.
    const { bases, cds } = record("server/tests/data/genbank/NC_012920.1.gb");
    const nd1 = cds.find((f) => f.qualifiers["gene"] === "ND1");
    if (!nd1) throw new Error("No ND1");
    const read = translateFeature(nd1, bases);
    if ("error" in read) throw new Error(read.error);
    expect(read.table).toBe(2);
    expect(read.codons.at(-1)).toMatchObject({ residue: "*", codon: "TA", bases: [4261, 4262] });
    expect(exceptionsOf("(pos:4261..4262,aa:TERM); (pos:complement(9..11),aa:Sec)")).toEqual([
      { from: 4261, to: 4262, residue: "*" },
      { from: 9, to: 11, residue: "U" },
    ]);
  });

  it("says when it names no genetic code NCBI has", () => {
    const [feature] = readFeatures(
      [{ type: "CDS", start: 1, end: 6, strand: "forward", qualifiers: { transl_table: "7" } }],
      6,
    );
    if (!feature) throw new Error("No feature");
    expect(translateFeature(feature, "ATGTAA")).toEqual({ error: "NCBI has no genetic code 7" });
  });
});

describe("a codon", () => {
  const standard = GENETIC_CODES[1];
  const bacterial = GENETIC_CODES[11];
  const condylostoma = GENETIC_CODES[28];
  if (!standard || !bacterial || !condylostoma) throw new Error("No tables");

  it("reads by NCBI's tables, and a start codon starting a protein as M", () => {
    expect(residueOf("ATG", standard)).toBe("M");
    expect(residueOf("TGA", standard)).toBe("*");
    expect(residueOf("TGA", GENETIC_CODES[2] ?? standard)).toBe("W");
    expect(residueOf("GTG", bacterial)).toBe("V");
    expect(residueOf("GTG", bacterial, { start: true, end: false })).toBe("M");
    expect(residueOf("aug", standard)).toBe("M");
  });

  it("reads a codon that's a stop only at the end as one only there (tables 27, 28, 31)", () => {
    expect(residueOf("TAA", condylostoma)).toBe("Q");
    expect(residueOf("TAA", condylostoma, { start: false, end: true })).toBe("*");
    expect(FRAME_CODES).not.toContain(28);
    expect(FRAME_CODES).toContain(11);
  });

  it("reads ambiguous bases as what every codon they could be reads as", () => {
    expect(residueOf("TTY", standard)).toBe("F"); // TTC or TTT
    expect(residueOf("CTN", standard)).toBe("L");
    expect(residueOf("RAY", standard)).toBe("B"); // AAC, AAT (N) or GAC, GAT (D)
    expect(residueOf("SAR", standard)).toBe("Z"); // CAA, CAG (Q) or GAA, GAG (E)
    expect(residueOf("ATN", standard)).toBe("X"); // I or M
    expect(residueOf("TAR", standard)).toBe("*");
    expect(residueOf("NNN", standard)).toBe("X");
    expect(residueOf("A-G", standard)).toBe("X");
  });
});

describe("a reading frame", () => {
  it("reads each of the six frames, the reverse ones from the end of the other strand", () => {
    // ATGAAATAG: +1 is M K *, and its reverse complement, CTATTTCAT, is L F H in −1.
    expect(proteinOf(translateFrame("ATGAAATAG", 1, 1))).toBe("MK");
    expect(translateFrame("ATGAAATAG", 2, 1).map((c) => c.residue)).toEqual(["*", "N"]);
    const reverse = translateFrame("ATGAAATAG", -1, 1);
    expect(reverse.map((c) => c.residue).join("")).toBe("LFH");
    expect(reverse[0]?.bases).toEqual([9, 8, 7]);
    expect(translateFrame("ATGAAATAG", -3, 1).map((c) => c.codon)).toEqual(["ATT", "TCA"]);
  });
});
