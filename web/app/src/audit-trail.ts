/**
 * A record's audit trail (ADR 0176): its address (Timeline or By field, narrowed to a
 * person or a field) and the queries its tab reads. Its revisions are the record's and,
 * for a record of a registry entry, its entry's, as the API tells them (ADR 0115).
 */

import { unwrap } from "@ikisci/sdk";
import { infiniteQueryOptions, keepPreviousData, queryOptions } from "@tanstack/react-query";

import { iki } from "./api";
import { ENTITIES } from "./entities";

/** The Audit trail tab's address: ?view=fields&person=<id>&field=<name>. */
export interface AuditSearch {
  view?: "fields" | undefined;
  person?: string | undefined;
  field?: string | undefined;
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
/** A field's name, as a type names it. */
const FIELD = /^[a-z][a-z0-9_]{0,62}$/;

export function validateAuditSearch(search: Record<string, unknown>): AuditSearch {
  const out: AuditSearch = {};
  if (search["view"] === "fields") out.view = "fields";
  const person = search["person"];
  if (typeof person === "string" && UUID.test(person)) out.person = person;
  const field = search["field"];
  if (typeof field === "string" && FIELD.test(field)) out.field = field;
  return out;
}

/** What the trail is narrowed to. */
export interface TrailFilter {
  person?: string | undefined;
  field?: string | undefined;
}

function filterQuery(filter: TrailFilter): { person?: string; field?: string } {
  return {
    ...(filter.person ? { person: filter.person } : {}),
    ...(filter.field ? { field: filter.field } : {}),
  };
}

/** The record's revisions, newest first, a page at a time, narrowed as the tab says. */
export function trailQuery(ref: string, filter: TrailFilter) {
  return infiniteQueryOptions({
    queryKey: [...ENTITIES, "revisions", ref.toUpperCase(), filterQuery(filter)],
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam, signal }) =>
      unwrap(
        await iki.GET("/api/v1/entities/{ref}/revisions", {
          params: {
            path: { ref },
            query: {
              limit: 25,
              ...filterQuery(filter),
              ...(pageParam ? { cursor: pageParam } : {}),
            },
          },
          signal,
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    placeholderData: keepPreviousData,
  });
}

/** Its registry entry's revisions, likewise: a person finds only those you're told they
 * made. */
export function entryTrailQuery(entry: string, filter: TrailFilter) {
  return infiniteQueryOptions({
    queryKey: [...ENTITIES, "entry-revisions", entry.toUpperCase(), filterQuery(filter)],
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam, signal }) =>
      unwrap(
        await iki.GET("/api/v1/entries/{ref}/revisions", {
          params: {
            path: { ref: entry },
            query: {
              limit: 25,
              ...filterQuery(filter),
              ...(pageParam ? { cursor: pageParam } : {}),
            },
          },
          signal,
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    placeholderData: keepPreviousData,
  });
}

/** Who made its revisions: whom the trail can be narrowed to. */
export function contributorsQuery(ref: string) {
  return queryOptions({
    queryKey: [...ENTITIES, "contributors", ref.toUpperCase()],
    queryFn: async ({ signal }) =>
      unwrap(
        await iki.GET("/api/v1/entities/{ref}/contributors", { params: { path: { ref } }, signal }),
      ),
  });
}
