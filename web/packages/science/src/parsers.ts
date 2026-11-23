/** Browser readers backed by the FASTA and HELM parsers in iki-wasm. */

/** A row of an alignment: a FASTA record, gaps and all. */
export interface AlignmentRow {
  /** Its FASTA header’s identifier, with a validated coordinate suffix removed. */
  id: string;
  description: string;
  /** Its residues, `-` for a gap and `.` for a place it has no observation of. */
  aligned: string;
  /** How many residues it has, gaps aside. */
  residues: number;
  /** Its first residue's number in its own sequence: 1, or the start of a `name/4-7` header. */
  start: number;
  /** Whether its header names the part of its sequence it is, as `name/1-4` does: then its
   * residues may be only part of it, even from 1. */
  spanned: boolean;
  /** Whether it's its sequence's other strand, headed `name/end-start` (`DNA-000001/2686-1`)
   * as Rfam writes one: its residues are numbered down from `start`. */
  reverse: boolean;
}

export type Alphabet = "nucleotide" | "protein";

export type Reading =
  | {
      kind: "alignment";
      rows: AlignmentRow[];
      length: number;
      alphabet: Alphabet;
      /** Whether the alphabet was guessed from its letters, the file not saying. */
      alphabet_guessed: boolean;
      /** Clustal's marks, a column each: `*` identical, `:` strongly and `.` weakly alike. */
      conservation: string;
      /** Each column's commonest residue, `-` where every row has a gap. */
      consensus: string;
      /** Each column's share of rows that have its consensus residue, as a percentage. */
      agreement: number[];
      identical: number;
      gapped: number;
    }
  | {
      kind: "sequences";
      alphabet: Alphabet;
      records: { id: string; description: string; length: number }[];
    }
  | { kind: "error"; message: string; line: number | null };

/** The most a file may be to be read in the browser, as iki-wasm also checks. */
export const MAX_READ_BYTES = 64 * 1024 * 1024;

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

interface Exports {
  memory: WebAssembly.Memory;
  iki_input: (length: number) => number;
  iki_read: (alphabet: number) => void;
  iki_helm: () => void;
  iki_render: () => void;
  iki_evaluate_rules: () => void;
  iki_output: () => number;
  iki_output_len: () => number;
}

type JsonOperation = "render" | "rules" | "helm";

export interface Parsers {
  /** Read UTF-8 FASTA bytes or text. Supply the alphabet if known from the data source. */
  readFasta: (input: Uint8Array | string, alphabet?: Alphabet | null) => Reading;
  /** Read a batch in order. Each invalid notation gets its own character offset and error. */
  readHelm: (texts: readonly string[]) => HelmReading[];
  /** The module's JSON wire format, for workers that also run CEL templates and rules. */
  requestJson: (operation: JsonOperation, request: string) => string;
}

/**
 * Instantiate the shipped reader. Pass a different asset URL or an already compiled
 * WebAssembly.Module to control loading. Keep the returned functions for repeated
 * reads, or scope them to a worker that can be terminated to release its memory.
 */
export async function loadParsers(
  source: URL | WebAssembly.Module = new URL("../wasm/iki_wasm.wasm", import.meta.url),
): Promise<Parsers> {
  let instance: WebAssembly.Instance;
  if (source instanceof WebAssembly.Module) {
    instance = await WebAssembly.instantiate(source, {});
  } else {
    const response = await fetch(source);
    if (!response.ok) throw new Error(`The parsers didn’t load (${String(response.status)}).`);
    ({ instance } = await WebAssembly.instantiate(await response.arrayBuffer(), {}));
  }

  const wasm = instance.exports as unknown as Exports;
  const encoder = new TextEncoder();
  const decoder = new TextDecoder();

  function exchange(bytes: Uint8Array, run: () => void): string {
    // iki_input caps the allocation. Check before writing into linear memory.
    if (bytes.byteLength > MAX_READ_BYTES) throw new RangeError("Input exceeds 64 MiB.");
    const at = wasm.iki_input(bytes.byteLength);
    new Uint8Array(wasm.memory.buffer, at, bytes.byteLength).set(bytes);
    run();
    // The call can grow memory and replace its buffer.
    const out = new Uint8Array(wasm.memory.buffer, wasm.iki_output(), wasm.iki_output_len());
    return decoder.decode(out);
  }

  const calls = { render: wasm.iki_render, rules: wasm.iki_evaluate_rules, helm: wasm.iki_helm };
  function requestJson(operation: JsonOperation, request: string): string {
    return exchange(encoder.encode(request), calls[operation]);
  }

  return {
    readFasta(input, alphabet = null) {
      const bytes = typeof input === "string" ? encoder.encode(input) : input;
      const declared = alphabet === "nucleotide" ? 1 : alphabet === "protein" ? 2 : 0;
      return JSON.parse(
        exchange(bytes, () => {
          wasm.iki_read(declared);
        }),
      ) as Reading;
    },
    readHelm(texts) {
      if (!texts.length) return [];
      const response = JSON.parse(requestJson("helm", JSON.stringify({ helm: texts }))) as {
        readings?: HelmReading[];
        error?: string;
      };
      if (!response.readings)
        throw new Error(response.error ?? "The HELM parser returned no readings.");
      return response.readings;
    },
    requestJson,
  };
}
