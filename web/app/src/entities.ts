/**
 * Entities and their types, as the pages read them: queries, and how each
 * kind of value reads to people (units in their display unit, references by
 * HRID, files by name).
 */

import { ApiError, unwrap, type Schemas } from "@ikisci/sdk";
import { infiniteQueryOptions, queryOptions } from "@tanstack/react-query";

import { iki } from "./api";

export type EntityType = Schemas["EntityTypeOut"];
export type FieldDef = Schemas["FieldOut"];
export type Entity = Schemas["EntityOut"];
export type Revision = Schemas["RevisionOut"];
export type RevisionDetail = Schemas["RevisionDetailOut"];
export type EntityProject = Schemas["EntityProjectOut"];

export const ENTITIES = ["entities"] as const;

/** The installed types: they change only when the deployment does. */
export const typesQuery = queryOptions({
  queryKey: ["entity-types"],
  queryFn: async () => unwrap(await iki.GET("/api/v1/entity-types")),
  staleTime: 10 * 60_000,
});

export function typeQuery(name: string) {
  return queryOptions({
    ...typesQuery,
    select: (types: EntityType[]) => {
      const found = types.find((t) => t.name === name);
      if (!found) {
        throw new ApiError({
          type: "about:blank",
          title: "Not Found",
          status: 404,
          detail: `There’s no entity type ${name}.`,
          code: "not_found",
        });
      }
      return found;
    },
  });
}

export interface EntityFilter {
  project?: string | undefined;
  type?: string | undefined;
  archived?: boolean | undefined;
}

/** A page at a time, oldest first; the grid asks for more as it scrolls. */
export function entitiesQuery(filter: EntityFilter) {
  return infiniteQueryOptions({
    queryKey: [...ENTITIES, "list", filter],
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam }) =>
      unwrap(
        await iki.GET("/api/v1/entities", {
          params: {
            query: {
              limit: 100,
              ...(filter.project ? { project: filter.project } : {}),
              ...(filter.type ? { type: filter.type } : {}),
              ...(filter.archived === undefined ? {} : { archived: filter.archived }),
              ...(pageParam ? { cursor: pageParam } : {}),
            },
          },
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
}

/** By ID or HRID, as in the page's address. */
export function entityQuery(ref: string) {
  return queryOptions({
    queryKey: [...ENTITIES, "one", ref.toUpperCase()],
    queryFn: async () =>
      unwrap(await iki.GET("/api/v1/entities/{ref}", { params: { path: { ref } } })),
  });
}

export function provenanceQuery(ref: string) {
  return queryOptions({
    queryKey: [...ENTITIES, "provenance", ref.toUpperCase()],
    queryFn: async () =>
      unwrap(await iki.GET("/api/v1/entities/{ref}/provenance", { params: { path: { ref } } })),
  });
}

export function entityProjectsQuery(ref: string) {
  return queryOptions({
    queryKey: [...ENTITIES, "projects", ref.toUpperCase()],
    queryFn: async () =>
      unwrap(await iki.GET("/api/v1/entities/{ref}/projects", { params: { path: { ref } } })),
  });
}

/** Newest first, a page at a time ("Show older"). */
export function revisionsQuery(ref: string) {
  return infiniteQueryOptions({
    queryKey: [...ENTITIES, "revisions", ref.toUpperCase()],
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam }) =>
      unwrap(
        await iki.GET("/api/v1/entities/{ref}/revisions", {
          params: {
            path: { ref },
            query: { limit: 25, ...(pageParam ? { cursor: pageParam } : {}) },
          },
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
}

export function revisionQuery(ref: string, number: number) {
  return queryOptions({
    queryKey: [...ENTITIES, "revision", ref.toUpperCase(), number],
    queryFn: async () =>
      unwrap(
        await iki.GET("/api/v1/entities/{ref}/revisions/{number}", {
          params: { path: { ref, number } },
        }),
      ),
    // A revision never changes.
    staleTime: Infinity,
  });
}

/** Which entities a list shows: ?show=archived|all. */
export type Showing = "current" | "archived" | "all";

export interface EntitySearch {
  type?: string | undefined;
  show?: Showing | undefined;
}

export function validateEntitySearch(search: Record<string, unknown>): EntitySearch {
  const out: EntitySearch = {};
  if (typeof search["type"] === "string" && search["type"]) out.type = search["type"];
  if (search["show"] === "archived" || search["show"] === "all") out.show = search["show"];
  return out;
}

// --- Reading values ---------------------------------------------------------------

/** Fields people see: computed ones too, since actions set them. */
export function shownFields(type: EntityType): FieldDef[] {
  return type.fields;
}

/** Drops floating-point noise from a conversion (52.341000000000001 → 52.341). */
function clean(value: number): number {
  return Number(value.toPrecision(12));
}

const number = new Intl.NumberFormat("en-GB", { maximumFractionDigits: 20 });

export function formatNumber(value: number): string {
  return number.format(value);
}

/** A quantity in the unit people read it in, and in the unit it's stored in. */
export function quantity(field: FieldDef, value: number): { shown: string; stored: string } {
  const stored = `${formatNumber(value)} ${field.unit_symbol ?? field.unit ?? ""}`.trim();
  if (field.display_scale != null && field.display_unit) {
    const converted = clean(value * field.display_scale + (field.display_offset ?? 0));
    const unit = field.display_unit_symbol ?? field.display_unit;
    return { shown: `${formatNumber(converted)} ${unit}`, stored };
  }
  return { shown: stored, stored };
}

export function formatDate(value: string): string {
  return new Date(`${value}T00:00:00Z`).toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  });
}

export function formatDateTime(value: string): string {
  return new Date(value).toLocaleString("en-GB", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZoneName: "short",
  });
}

const SIZE_UNITS = ["KiB", "MiB", "GiB", "TiB"] as const;

export function formatSize(bytes: number): string {
  if (bytes < 1024) return bytes === 1 ? "1 byte" : `${String(bytes)} bytes`;
  let size = bytes;
  let unit = "bytes";
  for (const next of SIZE_UNITS) {
    if (size < 1024) break;
    size /= 1024;
    unit = next;
  }
  return `${size.toFixed(size < 10 ? 1 : 0)} ${unit}`;
}

/** A value as plain text, for the grid and for comparing revisions. */
export function valueText(
  field: FieldDef,
  value: unknown,
  entity?: Pick<Entity, "refs" | "files">,
): string {
  if (value === null || value === undefined) return "";
  if (Array.isArray(value)) {
    const item: FieldDef = { ...field, type: field.items ?? "string" };
    return value.map((v) => valueText(item, v, entity)).join(", ");
  }
  switch (field.type) {
    case "bool":
      return value === true ? "Yes" : "No";
    case "int":
    case "float":
      return formatNumber(value as number);
    case "quantity":
      return quantity(field, value as number).shown;
    case "date":
      return formatDate(value as string);
    case "datetime":
      return formatDateTime(value as string);
    case "ref":
      return entity?.refs[value as string]?.hrid ?? "An entity you can’t read";
    case "file":
      return entity?.files[value as string]?.name ?? "A file you can’t read";
    case "json":
      return JSON.stringify(value);
    default:
      return typeof value === "string" ? value : JSON.stringify(value);
  }
}

/** Who did something: their name, their email address if they gave no name. */
export function personName(user: { name: string; email: string } | null | undefined): string {
  if (!user) return "Someone no longer here";
  return user.name === "" ? user.email : user.name;
}

/** A label inside a sentence: "Protein structures" → "protein structures"; "DNA" stays. */
export function inSentence(label: string): string {
  const second = label.charAt(1);
  if (second !== second.toLowerCase()) return label;
  return label.charAt(0).toLowerCase() + label.slice(1);
}

/** "dna.DNASequence" → the type's label, or the name if it isn't installed. */
export function typeLabel(types: EntityType[] | undefined, name: string): string {
  return types?.find((t) => t.name === name)?.label ?? name;
}
