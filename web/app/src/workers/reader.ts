/**
 * The parsers' Web Worker (ADR 0088): it runs iki-wasm, Iki's parsers compiled from Rust
 * to WebAssembly, on a file's bytes and posts back what it read. The server gives this
 * script a policy of its own: no network beyond Iki's origin, which it loads the module
 * from, and WebAssembly but no `eval`.
 */

import wasmUrl from "../../wasm/iki_wasm.wasm?url";

interface Exports {
  memory: WebAssembly.Memory;
  iki_input: (len: number) => number;
  iki_read: () => void;
  iki_output: () => number;
  iki_output_len: () => number;
}

/** The worker's own global scope, which the app's DOM types don't describe. */
const scope = self as unknown as {
  postMessage: (message: unknown) => void;
  addEventListener: (type: "message", listener: (event: MessageEvent) => void) => void;
};

let module: Promise<Exports> | null = null;

async function instantiate(): Promise<Exports> {
  const response = await fetch(wasmUrl);
  if (!response.ok) throw new Error(`The parsers didn’t load (${String(response.status)}).`);
  const { instance } = await WebAssembly.instantiate(await response.arrayBuffer(), {});
  return instance.exports as unknown as Exports;
}

/** What the module reads in `bytes`, as JSON text. */
async function read(bytes: Uint8Array): Promise<string> {
  module ??= instantiate();
  const wasm = await module;
  const at = wasm.iki_input(bytes.length);
  new Uint8Array(wasm.memory.buffer, at, bytes.length).set(bytes);
  wasm.iki_read();
  // Read the memory after the call: it may have grown, which replaces its buffer.
  const out = new Uint8Array(wasm.memory.buffer, wasm.iki_output(), wasm.iki_output_len());
  return new TextDecoder().decode(out);
}

scope.addEventListener("message", (event) => {
  const { bytes } = event.data as { bytes: ArrayBuffer };
  read(new Uint8Array(bytes)).then(
    (json) => {
      scope.postMessage({ json });
    },
    (error: unknown) => {
      scope.postMessage({ error: error instanceof Error ? error.message : String(error) });
    },
  );
});
