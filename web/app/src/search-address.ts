/**
 * A saved search as the grid's address, and the grid's address as a saved search keeps it.
 * Kept apart from the saved searches' page and dialogs: search everything's palette opens a
 * saved view at its address too, on the server's pages as well as the app's.
 */

import {
  DEFAULT_SORT,
  gridSearch,
  sortKeysOf,
  sortKeyText,
  validateEntitySearch,
  type EntitySearch,
} from "./entities";
import { sameJson, searchWords, wordsOf, type Filter } from "./filters";

interface Query {
  type?: string | null;
  archived?: boolean | null;
  exact_type?: boolean | null;
  q?: string | null;
  where?: { all?: unknown[] } | Filter | null;
  sort?: { field: string; descending?: boolean } | null;
  then?: { field: string; descending: boolean }[] | null;
}

/**
 * The grid's address for a saved search, or null if it holds what the grid can't show:
 * opening it would run another search. The grid's own searches come back as they were
 * made (see `gridSearch`): the filters', and a group of conditions the search box's words
 * make, as those words again.
 */
export function toAddress(query: Query): EntitySearch | null {
  const out: EntitySearch = {};
  // The grid finds a type's subtypes too.
  if (query.exact_type) return null;
  if (query.type) out.type = query.type;
  if (query.archived === true) out.show = "archived";
  else if (query.archived !== false) out.show = "all";
  // Text the search box would read as conditions isn't text to it.
  if (query.q && searchWords(query.q).conditions.length) return null;
  const words: string[] = [];
  if (query.where) {
    const where = query.where as Record<string, unknown>;
    const all = "all" in where ? (where["all"] as unknown[]) : "field" in where ? [where] : null;
    if (!all) return null;
    const filters: Filter[] = [];
    for (const c of all) {
      if (typeof c !== "object" || c === null) return null;
      if ("field" in c) {
        filters.push(c as Filter);
      } else if ("all" in c && Array.isArray(c.all) && !words.length) {
        const said = wordsOf(c.all);
        if (!said?.length) return null;
        words.push(...said);
      } else {
        return null;
      }
    }
    if (filters.length) out.filters = filters;
  }
  const q = [query.q ?? "", ...words].filter(Boolean).join(" ");
  if (q) out.q = q;
  // Without a sort of its own, it opens in the grid's order, newest changes first, as
  // every list does (ADR 0176).
  if (
    query.sort &&
    (query.sort.field !== DEFAULT_SORT.field ||
      Boolean(query.sort.descending) !== DEFAULT_SORT.descending ||
      Boolean(query.then?.length))
  ) {
    out.sort = query.sort.field;
    if (query.sort.descending) out.desc = true;
    if (query.then?.length) out.then = query.then.map(sortKeyText);
  }
  // The grid reads only so many filters and so much text from its address. A search that
  // needs more isn't a link: the grid would run it without what it cut.
  return sameJson(validateEntitySearch({ ...out }), out) ? out : null;
}

/** The grid's search, as a saved search keeps it: what the grid runs, without its project. */
export function fromAddress(search: EntitySearch) {
  const show = search.show ?? "current";
  return gridSearch({
    type: search.type,
    archived: show === "all" ? undefined : show === "archived",
    q: search.q,
    filters: search.filters,
    // Without a sort in the address, the grid's default order, which it runs.
    sort: search.sort ?? DEFAULT_SORT.field,
    descending: search.sort ? search.desc : DEFAULT_SORT.descending,
    then: search.sort ? sortKeysOf(search.then) : undefined,
  });
}
