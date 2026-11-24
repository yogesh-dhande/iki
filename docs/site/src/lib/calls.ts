// The same REST call elsewhere: the Python SDK method and the MCP tool that make it, as
// `python -m sitegen generate` read them from their code (docs/sitegen/calls.py).
const generated = import.meta.glob<{ default: Record<string, { sdk: string[]; mcp: string[] }> }>(
  "../generated/calls.json",
  { eager: true },
);

/** `GET /api/v1/entities/{ref}/signatures` → `GET /entities/{}/signatures`, as calls.py keys it. */
export function callKey(method: string, path: string): string {
  return `${method.toUpperCase()} ${path.replace(/^\/api\/v\d+(?=\/)/, "").replace(/\{[^}]*\}/g, "{}")}`;
}

export function sameCall(method: string, path: string): { sdk: string[]; mcp: string[] } {
  const calls = generated["../generated/calls.json"]?.default;
  if (!calls) {
    throw new Error("No calls.json: run `make docs` (python -m sitegen generate) first");
  }
  return calls[callKey(method, path)] ?? { sdk: [], mcp: [] };
}
