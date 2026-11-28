/**
 * Reading files in the browser with iki-wasm (ADR 0088): the page hands a file's bytes to
 * the parsers' Web Worker and gets back what they read. The worker is created through a
 * Trusted Types policy that only allows the app's own worker scripts.
 */

import type { Alphabet, Reading } from "@ikisci/science/parsers";

import readerUrl from "./workers/reader.ts?worker&url";

export { MAX_READ_BYTES } from "@ikisci/science/parsers";
export type { AlignmentRow, Alphabet, Reading } from "@ikisci/science/parsers";

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

/** One of the app's own workers, `script` (as Vite's `?worker&url` gives it). */
export function newWorker(script: string = readerUrl): Worker {
  const url = new URL(script, location.href).href;
  const tt = (window as TrustedTypesWindow).trustedTypes;
  policy ??= tt ? tt.createPolicy("iki-workers", { createScriptURL: workerScriptUrl }) : null;
  const trusted = policy ? policy.createScriptURL(url) : workerScriptUrl(url);
  return new Worker(trusted as string | URL, { type: "module" });
}

/** The alphabet a FASTA file's format says it holds, if it says. */
export function declaredAlphabet(formatId: string | undefined): Alphabet | null {
  if (formatId === "iki/core.protein_fasta") return "protein";
  if (formatId === "iki/core.nucleotide_fasta") return "nucleotide";
  return null;
}

/** What iki-wasm reads in `bytes`, which are handed to the worker (and so emptied here).
 * `alphabet` is what the file says it holds, if it says; otherwise its letters are read for it.
 * Each read has a worker of its own, ended when it's done: WebAssembly's memory never
 * shrinks, so a worker kept for later would hold on to what the largest file needed. */
export function readInWorker(
  bytes: ArrayBuffer,
  alphabet: Alphabet | null = null,
): Promise<Reading> {
  const worker = newWorker();
  return new Promise<Reading>((resolve, reject) => {
    worker.addEventListener(
      "message",
      (event: MessageEvent<{ reading?: Reading; error?: string }>) => {
        worker.terminate();
        const { reading, error } = event.data;
        if (reading !== undefined) resolve(reading);
        else reject(new Error(error ?? "The parsers stopped."));
      },
      { once: true },
    );
    worker.addEventListener("error", () => {
      worker.terminate();
      reject(new Error("The parsers stopped."));
    });
    worker.postMessage({ bytes, alphabet }, [bytes]);
  });
}
