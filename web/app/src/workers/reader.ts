/**
 * The parsers' Web Worker (ADR 0088): it runs iki-wasm, Iki's parsers compiled from Rust
 * to WebAssembly, on a file's bytes and posts back what it read. The server gives this
 * script a policy of its own: no network beyond Iki's origin, which it loads the module
 * from, and WebAssembly but no `eval`.
 */

import { loadParsers, type Alphabet, type Parsers } from "@ikisci/science/parsers";

/** The worker's own global scope, which the app's DOM types don't describe. */
const scope = self as unknown as {
  postMessage: (message: unknown) => void;
  addEventListener: (type: "message", listener: (event: MessageEvent) => void) => void;
};

let module: Promise<Parsers> | null = null;

scope.addEventListener("message", (event) => {
  const { bytes, alphabet } = event.data as { bytes: ArrayBuffer; alphabet?: Alphabet | null };
  module ??= loadParsers();
  module
    .then((parsers) => parsers.readFasta(new Uint8Array(bytes), alphabet ?? null))
    .then(
      (reading) => {
        scope.postMessage({ reading });
      },
      (error: unknown) => {
        scope.postMessage({ error: error instanceof Error ? error.message : String(error) });
      },
    );
});
