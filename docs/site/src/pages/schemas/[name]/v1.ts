// The iki/v1 JSON Schemas at the URLs they name themselves by (`$id`), which editors fetch
// to check a plugin's YAML: https://ikisci.org/schemas/manifest/v1.
import type { APIRoute } from "astro";

import { loadSchemas } from "../../../lib/spec.ts";

export function getStaticPaths() {
  return [...loadSchemas().keys()].map((name) => ({ params: { name } }));
}

export const GET: APIRoute = ({ params }) => {
  const schema = loadSchemas().get(String(params["name"]));
  return new Response(`${JSON.stringify(schema, null, 2)}\n`, {
    headers: { "Content-Type": "application/schema+json" },
  });
};
