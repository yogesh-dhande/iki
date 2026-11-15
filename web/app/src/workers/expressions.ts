/**
 * Apps' expressions' Web Worker (ADR 0090): it renders templates with iki-wasm, the CEL
 * the server checked them with, and posts back their values. It's kept between
 * renderings, since apps render often; a rendering that runs too long has the page end
 * the worker and start another. Like the parsers' worker, it has a policy of its own: no
 * network beyond Iki's origin, which it loads the module from, and no `eval`.
 */

import wasmUrl from "../../wasm/iki_wasm.wasm?url";

interface Exports {
  memory: WebAssembly.Memory;
  iki_input: (len: number) => number;
  iki_render: () => void;
  iki_output: () => number;
  iki_output_len: () => number;
}

const scope = self as unknown as {
  postMessage: (message: unknown) => void;
  addEventListener: (type: "message", listener: (event: MessageEvent) => void) => void;
};

async function instantiate(): Promise<Exports> {
  const response = await fetch(wasmUrl);
  if (!response.ok) throw new Error(`Expressions didn’t load (${String(response.status)}).`);
  const { instance } = await WebAssembly.instantiate(await response.arrayBuffer(), {});
  return instance.exports as unknown as Exports;
}

function render(wasm: Exports, request: string): string {
  const bytes = new TextEncoder().encode(request);
  const at = wasm.iki_input(bytes.length);
  new Uint8Array(wasm.memory.buffer, at, bytes.length).set(bytes);
  wasm.iki_render();
  // Read the memory after the call: it may have grown, which replaces its buffer.
  const out = new Uint8Array(wasm.memory.buffer, wasm.iki_output(), wasm.iki_output_len());
  return new TextDecoder().decode(out);
}

// The module loads first, and the page hears when it's ready, so that the time a
// rendering may take is the rendering's alone.
instantiate().then(
  (wasm) => {
    scope.addEventListener("message", (event) => {
      const { request } = event.data as { request: string };
      try {
        scope.postMessage({ json: render(wasm, request) });
      } catch (error) {
        scope.postMessage({ error: error instanceof Error ? error.message : String(error) });
      }
    });
    scope.postMessage({ ready: true });
  },
  (error: unknown) => {
    scope.postMessage({ failed: error instanceof Error ? error.message : String(error) });
  },
);
