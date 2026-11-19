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
  units: number;
  monomers: HelmMonomer[];
  ambiguous: boolean;
}

export interface HelmRead {
  canonical: string;
  polymers: HelmPolymer[];
  connections: string[];
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

/** What a reading holds, in a sentence: "Two RNA strands of 21 and 23 nucleotides." */
export function describeHelm(reading: HelmReading): string {
  if (isProblem(reading)) return `At character ${String(reading.at + 1)}: ${reading.error}`;
  const parts = reading.polymers.map((p) => {
    const [one, many] = KINDS[p.type];
    if (p.type === "CHEM") return p.monomers[0]?.id ?? "a chemical group";
    if (p.type === "BLOB") return "a part HELM doesn't describe";
    return `${p.type === "RNA" ? "a strand" : "a chain"} of ${String(p.units)} ${p.units === 1 ? one : many}`;
  });
  const connections = reading.connections.length;
  const joined = new Intl.ListFormat("en", { type: "conjunction" }).format(parts);
  const tail =
    connections === 0
      ? ""
      : `, with ${String(connections)} ${connections === 1 ? "connection" : "connections"}`;
  return `HELM: ${joined}${tail}.`;
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
