// The REST API reference's data, for its pages: the server's OpenAPI document, read once
// per build, and what links each operation to the rest of the site.
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { endpoints, schemaUses, tags, type Document, type Endpoint } from "./openapi.ts";
import { ROOT } from "./repo.ts";

/** Where the server's document is kept (`make client` writes it). */
export const OPENAPI = "web/packages/sdk/openapi.json";

let loaded: { doc: Document; all: Endpoint[]; uses: Map<string, Endpoint[]> } | undefined;

export function api(): { doc: Document; all: Endpoint[]; uses: Map<string, Endpoint[]> } {
  if (!loaded) {
    const doc = JSON.parse(readFileSync(join(ROOT, OPENAPI), "utf-8")) as Document;
    const all = endpoints(doc);
    loaded = { doc, all, uses: schemaUses(doc, all) };
  }
  return loaded;
}

export function apiTags() {
  return tags(api().doc);
}

export function endpointPath(slug: string): string {
  return `/reference/api/${slug}/`;
}

export function schemaPath(name: string): string {
  return `/reference/api/schemas/${encodeURIComponent(name)}/`;
}

/** The path as the reference shows it: without the `/api/v1` every path starts with. */
export function shortPath(path: string): string {
  return path.replace(/^\/api\/v\d+(?=\/)/, "");
}
