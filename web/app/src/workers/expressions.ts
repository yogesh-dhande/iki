/**
 * Apps' expressions' Web Worker (ADR 0090): it renders templates with iki-wasm, the CEL
 * the server checked them with, and posts back their values. Forms evaluate plugins'
 * rules with it too (ADR 0127), as the server will at the save, and read HELM as it's
 * typed (ADR 0144), as the server will read it. It's kept between
 * renderings, since apps render often; a rendering that runs too long has the page end
 * the worker and start another. Like the parsers' worker, it has a policy of its own: no
 * network beyond Iki's origin, which it loads the module from, and no `eval`.
 */

import { loadParsers } from "@ikisci/science/parsers";

const scope = self as unknown as {
  postMessage: (message: unknown) => void;
  addEventListener: (type: "message", listener: (event: MessageEvent) => void) => void;
};

// What the module renders once before it says it's ready, so that browsers which run a
// module's first calls slowly, before compiling them fully, do so here and not in the
// time a person's rendering is given.
const WARM_UP = JSON.stringify({
  templates: ["{{ size(p.rows.filter(r, r.title.startsWith('a'))) }} of {{ p.rows }}"],
  variables: { p: { rows: [{ title: "a" }, { title: "b" }] } },
});

// The module loads and warms up first, and the page hears when it's ready, so that the
// time a rendering may take is the rendering's alone.
loadParsers().then(
  (wasm) => {
    try {
      wasm.requestJson("render", WARM_UP);
    } catch (error) {
      scope.postMessage({ failed: error instanceof Error ? error.message : String(error) });
      return;
    }
    scope.addEventListener("message", (event) => {
      const { request, call } = event.data as {
        request: string;
        call?: "render" | "rules" | "helm";
      };
      try {
        scope.postMessage({
          json: wasm.requestJson(call === "rules" || call === "helm" ? call : "render", request),
        });
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
