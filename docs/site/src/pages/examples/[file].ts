// The files the use cases' worked examples read, served beside them so a reader can
// download what a figure shows.
import { readFileSync } from "node:fs";
import { join } from "node:path";

import type { APIRoute } from "astro";

import { exampleFiles } from "../../lib/examples.ts";
import { ROOT } from "../../lib/repo.ts";

export function getStaticPaths() {
  return [...exampleFiles.keys()].map((file) => ({ params: { file } }));
}

export const GET: APIRoute = ({ params }) => {
  const path = exampleFiles.get(String(params["file"]));
  if (path === undefined) {
    return new Response(null, { status: 404 });
  }
  return new Response(readFileSync(join(ROOT, path)), {
    headers: { "Content-Type": "text/csv; charset=utf-8" },
  });
};
