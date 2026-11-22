/**
 * The lists across projects (ADR 0176): every record, request and run their reader can
 * read, of every project they're in. A project's tabs are the same lists, held to it.
 *
 * Their addresses name a project by its slug (`?project=atlas`), as people read it; the
 * API takes its ID.
 */

import { unwrap, type Schemas } from "@ikisci/sdk";
import { queryOptions, useQuery } from "@tanstack/react-query";

import { iki, projectsQuery, type Project } from "./api";
import { ENTITIES, SAVED_SEARCHES, validateEntitySearch, type EntitySearch } from "./entities";

export type Counts = Schemas["CountsOut"];
export type TypeCount = Schemas["TypeCountOut"];

/** How many current records of each type the reader can read, in a project if one is
 * given; a count that would be slow comes back null (ADR 0176). */
export function countsQuery(projectId?: string) {
  return queryOptions({
    queryKey: [...ENTITIES, "counts", projectId ?? null],
    queryFn: async () =>
      unwrap(
        await iki.GET("/api/v1/entities/counts", {
          params: { query: projectId ? { project: projectId } : {} },
        }),
      ),
    staleTime: 30_000,
  });
}

/** Every saved search the reader sees: their own, of a project or of every project, and
 * those shared with projects they read. */
export const everySavedSearchQuery = queryOptions({
  queryKey: [...SAVED_SEARCHES, "everywhere"],
  queryFn: async () => unwrap(await iki.GET("/api/v1/saved-searches")),
});

/** The projects whose records the reader can read, by name: what a list's Project filter
 * offers. Projects they only see (an Org Admin sees them all) hold nothing for them.
 * `ready` once they're listed. */
export function useReadableProjects(): { projects: Project[]; ready: boolean } {
  const found = useQuery(projectsQuery);
  const projects = (found.data ?? [])
    .filter((p) => p.can.read)
    .sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base" }));
  return { projects, ready: found.isFetched };
}

/** A list's address across projects: its search, and the project it's held to by slug. */
export interface ListSearch extends EntitySearch {
  project?: string | undefined;
}

const SLUG = /^[a-z0-9_][a-z0-9_-]{0,99}$/;

export function validateListSearch(search: Record<string, unknown>): ListSearch {
  const out: ListSearch = validateEntitySearch(search);
  if (typeof search["project"] === "string" && SLUG.test(search["project"])) {
    out.project = search["project"];
  }
  return out;
}

/** Of a list's columns, those the reader hid, by the list they hid them in: kept in this
 * browser alone, as a convenience. Null if they haven't chosen. */
export function hiddenColumns(list: string): Set<string> | null {
  try {
    const kept = window.localStorage.getItem(`iki.columns.${list}`);
    if (kept === null) return null;
    const parsed: unknown = JSON.parse(kept);
    return Array.isArray(parsed)
      ? new Set(parsed.filter((k): k is string => typeof k === "string"))
      : null;
  } catch {
    return null;
  }
}

export function keepHiddenColumns(list: string, hidden: ReadonlySet<string>): void {
  try {
    window.localStorage.setItem(`iki.columns.${list}`, JSON.stringify([...hidden].sort()));
  } catch {
    // Storage can be off, full or blocked: the columns go back to all shown next time.
  }
}
