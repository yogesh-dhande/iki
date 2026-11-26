/**
 * The file previews' Web Worker (ADR 0226): it runs iki-previews, Iki's readers for
 * chromatograms, spectra, TIFF tiles and tables, compiled from Rust to WebAssembly, on
 * the bytes the page hands it, and posts back what they read. Like the parsers' worker it
 * has a policy of its own: no network beyond Iki's origin, which it loads the module from,
 * and WebAssembly but no `eval`. The page ends it when a read takes too long.
 */

import { call, type Exports, type Operation } from "../previews/wasm";

const scope = self as unknown as {
  postMessage: (message: unknown, transfer?: Transferable[]) => void;
  addEventListener: (type: "message", listener: (event: MessageEvent) => void) => void;
};

let module: Promise<Exports> | null = null;

async function load(): Promise<Exports> {
  const response = await fetch(new URL("../../wasm/iki_previews.wasm", import.meta.url));
  if (!response.ok) throw new Error(`The readers didn’t load (${String(response.status)}).`);
  const { instance } = await WebAssembly.instantiate(await response.arrayBuffer(), {});
  return instance.exports as unknown as Exports;
}

scope.addEventListener("message", (event) => {
  const { id, operation, args, input } = event.data as {
    id: number;
    operation: Operation;
    args: number[];
    input: ArrayBuffer;
  };
  module ??= load();
  module
    .then((wasm) => call(wasm, operation, args, new Uint8Array(input)))
    .then(
      ({ json, pixels }) => {
        scope.postMessage({ id, json, pixels }, pixels ? [pixels] : []);
      },
      (error: unknown) => {
        scope.postMessage({ id, error: error instanceof Error ? error.message : String(error) });
      },
    );
});
