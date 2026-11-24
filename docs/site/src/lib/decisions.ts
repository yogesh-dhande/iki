// The decision records as the other pages use them: to link "ADR 0126" in generated text,
// and to find the decisions that explain an API call.
import { getCollection } from "astro:content";

import type { Decisions } from "./markdown.ts";

export async function decisionList() {
  return (await getCollection("decisions")).flatMap((entry) =>
    entry.data.kind === "decision" ? [{ ...entry.data, body: entry.body ?? "" }] : [],
  );
}

let map: Promise<Decisions> | undefined;

/** Each decision's number, and its page's slug. */
export function decisionMap(): Promise<Decisions> {
  map ??= decisionList().then((list) => new Map(list.map((d) => [d.number, d.slug])));
  return map;
}
