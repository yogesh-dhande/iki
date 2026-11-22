/**
 * Registries and their entries (ADR 0115), as the pages read them. A registry
 * keeps each identity once, as an entry with one ID; each project that has it
 * keeps its own record of it.
 */

import { unwrap, type Schemas } from "@ikisci/sdk";
import { infiniteQueryOptions, queryOptions } from "@tanstack/react-query";

import { iki } from "./api";
import { ENTITIES } from "./entities";

export type Registry = Schemas["RegistryOut"];
export type RegistryEntry = Schemas["RegistryEntryOut"];
export type Entry = Schemas["EntryOut"];
export type EntryRecord = Schemas["EntryRecordOut"];
export type Moved = Schemas["MovedOut"];

export const REGISTRIES = ["registries"] as const;

/** Where Org Admins add registries, choose their teams and appoint their registrars. */
export const REGISTRY_SETTINGS = "/admin/registries/";

/** The registries the reader may see; with `team`, those its new projects may be in. */
export function registriesQuery(team?: string) {
  return queryOptions({
    queryKey: [...REGISTRIES, "list", team ?? null],
    queryFn: async () =>
      unwrap(
        await iki.GET("/api/v1/registries", {
          params: { query: { limit: 100, ...(team ? { team } : {}) } },
        }),
      ).items,
  });
}

export function registryQuery(id: string) {
  return queryOptions({
    queryKey: [...REGISTRIES, "one", id],
    queryFn: async () =>
      unwrap(
        await iki.GET("/api/v1/registries/{registry_id}", {
          params: { path: { registry_id: id } },
        }),
      ),
  });
}

export interface EntryFilter {
  /** An ID, or the start of one. */
  q?: string | undefined;
  type?: string | undefined;
  superseded?: boolean | undefined;
}

/** Every entry in a registry, for its registrars, a page at a time. */
export function registryEntriesQuery(id: string, filter: EntryFilter) {
  return infiniteQueryOptions({
    queryKey: [...REGISTRIES, "entries", id, filter],
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam }) =>
      unwrap(
        await iki.GET("/api/v1/registries/{registry_id}/entries", {
          params: {
            path: { registry_id: id },
            query: {
              limit: 50,
              ...(filter.q?.trim() ? { q: filter.q.trim() } : {}),
              ...(filter.type ? { type: filter.type } : {}),
              ...(filter.superseded === undefined ? {} : { superseded: filter.superseded }),
              ...(pageParam ? { cursor: pageParam } : {}),
            },
          },
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    placeholderData: (previous) => previous,
  });
}

/** An entry, by its ID or HRID. Its changes are entities' changes too, so it's under
 * ENTITIES: changing a record or an entry refreshes both. */
export function entryQuery(ref: string) {
  return queryOptions({
    queryKey: [...ENTITIES, "entry", ref.toUpperCase()],
    queryFn: async () =>
      unwrap(await iki.GET("/api/v1/entries/{ref}", { params: { path: { ref } } })),
  });
}

/** An entry's revisions, newest first. Those made through projects the reader can't read
 * say only what they set: not who, how or when. */
export function entryRevisionsQuery(ref: string) {
  return infiniteQueryOptions({
    queryKey: [...ENTITIES, "entry-revisions", ref.toUpperCase()],
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam }) =>
      unwrap(
        await iki.GET("/api/v1/entries/{ref}/revisions", {
          params: {
            path: { ref },
            query: { limit: 25, ...(pageParam ? { cursor: pageParam } : {}) },
          },
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
}

/** An entry as it was at a revision, and what the revision changed: a curated fact's before
 * and after. Disclosed as the list is. */
export function entryRevisionQuery(ref: string, number: number) {
  return queryOptions({
    queryKey: [...ENTITIES, "entry-revision", ref.toUpperCase(), number],
    queryFn: async () =>
      unwrap(
        await iki.GET("/api/v1/entries/{ref}/revisions/{number}", {
          params: { path: { ref, number } },
        }),
      ),
    // A revision never changes.
    staleTime: Infinity,
  });
}

/** What creating a record did, in a sentence: registered a new entry, gave the project a
 * record of an entry that was registered already, or opened the record the project had.
 * Who else has an entry is never said: the API doesn't say. */
export function createdMessage(made: Schemas["CreatedOut"], project: { name: string }): string {
  if (!made.entry) return `Created ${made.hrid}.`;
  if (made.reused) {
    return made.archived
      ? `${project.name} has ${made.hrid} already, so it’s opened as it is. It’s archived: restore it to change it.`
      : `${project.name} has ${made.hrid} already, so it’s opened as it is. Nothing was changed.`;
  }
  if (made.registered) {
    return `Registered ${made.hrid}, a new entry in the ${made.entry.registry.name} registry. This is ${project.name}’s record of it.`;
  }
  return `${made.hrid} was registered already, so ${project.name} now has its own record of it.`;
}

/** How a field is kept (ADR 0115): part of the entry's identity, derived from it, a fact its
 * registrars curate, or each project's own. */
export const REGISTRY_MARK: Record<"identity" | "derived" | "fact", string> = {
  identity: "Identity",
  derived: "Derived from its identity",
  fact: "Registry fact",
};

/** A fact that's fixed (ADR 0143): given when its entry was registered, never changed. */
export const FIXED_FACT = "Fixed registry fact";
