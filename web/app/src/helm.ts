/**
 * HELM (ADR 0144), read as it's typed by iki-wasm in the expressions worker: the module
 * the server reads it with, so what a form says of a HELM string is what a save will.
 */

import { useEffect, useState } from "react";

import { expressionRenderer } from "./apps";
import { SETTLE_MS } from "./rules";

/** A monomer as HELM has it: its ID, and where it is. */
export interface HelmMonomer {
  /** Its position in its polymer, from 1, as connections count. */
  position: number;
  id: string;
  /** A branch monomer: a nucleotide's base. */
  branch: boolean;
  smiles: boolean;
  /** One of several monomers, or a mixture. */
  list: boolean;
  /** The unit it's in (its nucleotide, for RNA), from 1. */
  unit: number;
}

export interface HelmPolymer {
  id: string;
  type: "PEPTIDE" | "RNA" | "CHEM" | "BLOB";
  /** Its units as written canonically: an RNA's include any without a base. */
  units: number;
  /** Its amino acids, or nucleotides (an RNA's units with a base): the fewest and most. */
  length: { fewest: number; most: number | null };
  monomers: HelmMonomer[];
  ambiguous: boolean;
}

/** A polymer group: a mixture of its members, or one of them. */
export interface HelmGroup {
  id: string;
  mixture: boolean;
  members: { id: string; ratio: string | null }[];
}

export interface HelmRead {
  canonical: string;
  polymers: HelmPolymer[];
  connections: string[];
  groups: HelmGroup[];
  version1: boolean;
}

export interface HelmProblem {
  error: string;
  /** The character it's at, from 0. */
  at: number;
}

export type HelmReading = HelmRead | HelmProblem;

export function isProblem(reading: HelmReading): reading is HelmProblem {
  return "error" in reading;
}

/** Each HELM string read, or why it isn't HELM. */
export async function readHelm(texts: string[]): Promise<HelmReading[]> {
  if (texts.length === 0) return [];
  return (await expressionRenderer().helm(JSON.stringify({ helm: texts }))) as HelmReading[];
}

const KINDS: Record<HelmPolymer["type"], [string, string]> = {
  RNA: ["nucleotide", "nucleotides"],
  PEPTIDE: ["amino acid", "amino acids"],
  CHEM: ["chemical group", "chemical groups"],
  BLOB: ["other part", "other parts"],
};

function count(n: number, [one, many]: [string, string]): string {
  return `${String(n)} ${n === 1 ? one : many}`;
}

/** How many a polymer has: "21 nucleotides", "4 to 7 amino acids", "at least 2". */
function howMany(p: HelmPolymer, kind: [string, string]): string {
  const { fewest, most } = p.length;
  if (most === null) {
    return fewest === 0 ? `an unknown number of ${kind[1]}` : `at least ${count(fewest, kind)}`;
  }
  if (most !== fewest) return `${String(fewest)} to ${count(most, kind)}`;
  return count(fewest, kind);
}

function describePolymer(p: HelmPolymer): string {
  if (p.type === "CHEM") return p.monomers[0]?.id ?? "a chemical group";
  if (p.type === "BLOB") return "a part HELM doesn't describe";
  const kind = KINDS[p.type];
  let text = `${p.type === "RNA" ? "a strand" : "a chain"} of ${howMany(p, kind)}`;
  const exact = p.length.most === p.length.fewest;
  // A unit without a base, such as a 5′ phosphate or an abasic site, isn't a nucleotide.
  const others = p.type === "RNA" && exact ? p.units - p.length.fewest : 0;
  if (others > 0) text += ` and ${count(others, ["other unit", "other units"])}`;
  if (p.ambiguous && exact) text += ", not all of them known";
  return text;
}

/** What a reading holds, in a sentence: "a strand of 21 nucleotides and 5Bio". A polymer
 * group is "either" its members (`,`: one of them is there) or "a mixture" of them. */
export function describeHelm(reading: HelmReading): string {
  if (isProblem(reading)) return `At character ${String(reading.at + 1)}: ${reading.error}`;
  const polymers = new Map(reading.polymers.map((p) => [p.id, p]));
  const groups = new Map(reading.groups.map((g) => [g.id, g]));
  const grouped = new Set(reading.groups.flatMap((g) => g.members.map((m) => m.id)));
  const and = new Intl.ListFormat("en", { type: "conjunction" });
  const or = new Intl.ListFormat("en", { type: "disjunction" });
  const describe = (id: string): string => {
    const polymer = polymers.get(id);
    if (polymer) return describePolymer(polymer);
    const group = groups.get(id);
    if (!group) return id;
    const members = group.members.map((m) => describe(m.id));
    if (!group.mixture) return `either ${or.format(members)}`;
    const ratios = group.members.map((m) => m.ratio);
    const proportions = ratios.every((r) => r !== null) ? `, ${ratios.join(":")}` : "";
    return `a mixture of ${and.format(members)}${proportions}`;
  };
  const parts = [...reading.polymers.map((p) => p.id), ...reading.groups.map((g) => g.id)]
    .filter((id) => !grouped.has(id))
    .map(describe);
  const connections = reading.connections.length;
  const tail =
    connections === 0
      ? ""
      : `, with ${String(connections)} ${connections === 1 ? "connection" : "connections"}`;
  return `HELM: ${and.format(parts)}${tail}.`;
}

/** `text` read once it has settled, as it's typed; null while it's empty or waiting. */
export function useHelmReading(text: string): HelmReading | null {
  const [reading, setReading] = useState<{ text: string; reading: HelmReading } | null>(null);
  useEffect(() => {
    if (!text.trim()) return undefined;
    let current = true;
    const timer = setTimeout(() => {
      readHelm([text]).then(
        ([found]) => {
          if (current && found) setReading({ text, reading: found });
        },
        () => {
          // The worker didn't load: the server reads it at the save.
        },
      );
    }, SETTLE_MS);
    return () => {
      current = false;
      clearTimeout(timer);
    };
  }, [text]);
  return text.trim() && reading?.text === text ? reading.reading : null;
}
