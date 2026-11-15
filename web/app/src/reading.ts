/**
 * Reading files in the browser with iki-wasm (ADR 0088): the page hands a file's bytes to
 * the parsers' Web Worker and gets back what they read. The worker is created through a
 * Trusted Types policy that only allows the app's own worker scripts.
 */

import readerUrl from "./workers/reader.ts?worker&url";

/** A row of an alignment: a FASTA record, gaps and all. */
export interface AlignmentRow {
  /** Its header's first word: a sequence's HRID when it's one in Iki. */
  id: string;
  description: string;
  aligned: string;
  /** How many residues it has, gaps aside. */
  residues: number;
}

export type Alphabet = "nucleotide" | "protein";

export type Reading =
  | {
      kind: "alignment";
      rows: AlignmentRow[];
      length: number;
      alphabet: Alphabet;
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

interface TrustedTypesWindow {
  trustedTypes?: {
    createPolicy: (
      name: string,
      rules: { createScriptURL: (input: string) => string },
    ) => { createScriptURL: (input: string) => unknown };
  };
}

/** Only the app's own worker scripts, from Iki's origin, may run as workers. */
export function workerScriptUrl(input: string): string {
  const url = new URL(input, location.href);
  if (url.origin !== location.origin || !/\/assets\/worker-[\w-]+\.js$/.test(url.pathname)) {
    // Vite's dev server serves workers' sources as they are.
    if (!(import.meta.env.DEV && url.origin === location.origin)) {
      throw new TypeError(`${url.href} isn't one of Iki's workers.`);
    }
  }
  return url.href;
}

let policy: { createScriptURL: (input: string) => unknown } | null = null;

function newWorker(): Worker {
  const url = new URL(readerUrl, location.href).href;
  const tt = (window as TrustedTypesWindow).trustedTypes;
  policy ??= tt ? tt.createPolicy("iki-workers", { createScriptURL: workerScriptUrl }) : null;
  const script = policy ? policy.createScriptURL(url) : workerScriptUrl(url);
  return new Worker(script as string | URL, { type: "module" });
}

/** What iki-wasm reads in `bytes`, which are handed to the worker (and so emptied here).
 * Each read has a worker of its own, ended when it's done: WebAssembly's memory never
 * shrinks, so a worker kept for later would hold on to what the largest file needed. */
export function readInWorker(bytes: ArrayBuffer): Promise<Reading> {
  const worker = newWorker();
  return new Promise<Reading>((resolve, reject) => {
    worker.addEventListener(
      "message",
      (event: MessageEvent<{ json?: string; error?: string }>) => {
        worker.terminate();
        const { json, error } = event.data;
        if (json !== undefined) resolve(JSON.parse(json) as Reading);
        else reject(new Error(error ?? "The parsers stopped."));
      },
      { once: true },
    );
    worker.addEventListener("error", () => {
      worker.terminate();
      reject(new Error("The parsers stopped."));
    });
    worker.postMessage({ bytes }, [bytes]);
  });
}
