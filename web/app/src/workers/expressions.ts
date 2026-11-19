/**
 * Apps' expressions' Web Worker (ADR 0090): it renders templates with iki-wasm, the CEL
 * the server checked them with, and posts back their values. Forms evaluate plugins'
 * rules with it too (ADR 0127), as the server will at the save, and read HELM as it's
 * typed (ADR 0144), as the server will read it. It's kept between
 * renderings, since apps render often; a rendering that runs too long has the page end
 * the worker and start another. Like the parsers' worker, it has a policy of its own: no
 * network beyond Iki's origin, which it loads the module from, and no `eval`.
 */

import wasmUrl from "../../wasm/iki_wasm.wasm?url";

interface Exports {
  memory: WebAssembly.Memory;
  iki_input: (len: number) => number;
  iki_render: () => void;
  iki_evaluate_rules: () => void;
  iki_helm: () => void;
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

/** What a page may ask the module for: apps' templates, rules' expressions, or HELM read
 * (ADR 0144). */
const CALLS = { render: "iki_render", rules: "iki_evaluate_rules", helm: "iki_helm" } as const;
type Call = keyof typeof CALLS;

function render(wasm: Exports, request: string, call: Call = "render"): string {
  const bytes = new TextEncoder().encode(request);
  const at = wasm.iki_input(bytes.length);
  new Uint8Array(wasm.memory.buffer, at, bytes.length).set(bytes);
  wasm[CALLS[call]]();
  // Read the memory after the call: it may have grown, which replaces its buffer.
  const out = new Uint8Array(wasm.memory.buffer, wasm.iki_output(), wasm.iki_output_len());
  return new TextDecoder().decode(out);
}

// What the module renders once before it says it's ready, so that browsers which run a
// module's first calls slowly, before compiling them fully, do so here and not in the
// time a person's rendering is given.
const WARM_UP = JSON.stringify({
  templates: ["{{ size(p.rows.filter(r, r.title.startsWith('a'))) }} of {{ p.rows }}"],
  variables: { p: { rows: [{ title: "a" }, { title: "b" }] } },
});

// The module loads and warms up first, and the page hears when it's ready, so that the
// time a rendering may take is the rendering's alone.
instantiate().then(
  (wasm) => {
    try {
      render(wasm, WARM_UP);
    } catch (error) {
      scope.postMessage({ failed: error instanceof Error ? error.message : String(error) });
      return;
    }
    scope.addEventListener("message", (event) => {
      const { request, call } = event.data as { request: string; call?: Call };
      try {
        scope.postMessage({ json: render(wasm, request, call && call in CALLS ? call : "render") });
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
