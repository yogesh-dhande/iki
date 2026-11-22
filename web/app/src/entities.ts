/**
 * Entities and their types, as the pages read them: queries, and how each
 * kind of value reads to people (units in their display unit, references by
 * HRID, files by name).
 */

import { ApiError, unwrap, type Schemas } from "@ikisci/sdk";
import { infiniteQueryOptions, keepPreviousData, queryOptions } from "@tanstack/react-query";

import { iki, projectQuery, queryClient } from "./api";
import { searchWords, validateFilters, type Filter } from "./filters";
import { objectList, objectListText } from "./object-lists";

export type EntityType = Schemas["EntityTypeOut"];
export type FieldDef = Schemas["FieldOut"];
export type Entity = Schemas["EntityOut"];
export type Revision = Schemas["RevisionOut"];
export type RevisionDetail = Schemas["RevisionDetailOut"];
export type EntityProject = Schemas["EntityProjectOut"];
export type Origin = Schemas["OriginOut"];
export type Making = Schemas["MakingOut"];
export type EntryRef = Schemas["EntryRefOut"];
export type Ref = Schemas["RefOut"];

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
  /** Text in their HRID or title. */
  q?: string | undefined;
  /** Conditions on fields, all of which must hold. */
  filters?: Filter[] | undefined;
  sort?: string | undefined;
  descending?: boolean | undefined;
  /** Only a collection's members (its HRID), from every project the reader can read. */
  collection?: string | undefined;
}

/** The search the grid runs, without its paging: a component that shows the grid's
 * results is given the same (ADR 0117), and a saved search keeps it (ADR 0037).
 *
 * Words such as `assigned:me` are conditions (ADR 0122); the rest is text. They're a group
 * of their own (`all`) after the filters', so the search reads back as the same filters
 * and words: that's how a saved search opens in the grid as it was saved. */
export function gridSearch(filter: EntityFilter) {
  const words = searchWords(filter.q ?? "");
  const conditions = [
    ...(filter.filters ?? []),
    ...(words.conditions.length ? [{ all: words.conditions }] : []),
  ];
  return {
    exact_type: false,
    ...(filter.collection
      ? { collection: filter.collection }
      : filter.project
        ? { project: filter.project }
        : {}),
    ...(filter.type ? { type: filter.type } : {}),
    ...(filter.archived === undefined ? {} : { archived: filter.archived }),
    ...(words.text.trim() ? { q: words.text.trim() } : {}),
    ...(conditions.length ? { where: { all: conditions as Schemas["Condition"][] } } : {}),
    ...(filter.sort
      ? { sort: { field: filter.sort, descending: filter.descending ?? false } }
      : {}),
  };
}

/** The address of everything the grid's search finds, as CSV (ADR 0175): the same search,
 * as the download's parameters, with the columns the grid shows if it's given them. */
export function searchCsvUrl(filter: EntityFilter, columns: readonly string[] = []): string {
  const search = gridSearch(filter);
  const query = new URLSearchParams();
  // The grid's columns, as it shows them; without them, the type's.
  for (const column of columns) query.append("columns", column);
  if (search.type) query.set("type", search.type);
  if ("project" in search) query.set("project", search.project);
  if ("collection" in search) query.set("collection", search.collection);
  if (search.archived !== undefined) query.set("archived", String(search.archived));
  if (search.q) query.set("q", search.q);
  if (search.where) query.set("where", JSON.stringify(search.where));
  if (search.sort) {
    query.set("sort", search.sort.field);
    if (search.sort.descending) query.set("descending", "true");
  }
  return `/api/v1/entities/search.csv?${query.toString()}`;
}

/** A page at a time, in the order asked for; the grid asks for more as it scrolls. */
export function entitiesQuery(filter: EntityFilter) {
  return infiniteQueryOptions({
    queryKey: [...ENTITIES, "list", filter],
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam }) =>
      unwrap(
        await iki.POST("/api/v1/entities/search", {
          body: { limit: 100, ...gridSearch(filter), ...(pageParam ? { cursor: pageParam } : {}) },
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    // A search that went wrong (too slow, say) won't go right by asking again.
    retry: false,
    // Changing a filter or the sort keeps the grid in place until the new results come.
    // Another type has other columns, so its rows aren't kept.
    placeholderData: (previous, query) =>
      (query?.queryKey[2] as EntityFilter | undefined)?.type === filter.type
        ? keepPreviousData(previous)
        : undefined,
  });
}

export type SavedSearch = Schemas["SavedSearchOut"];
export const SAVED_SEARCHES = ["saved-searches"] as const;

/** A project's saved searches: the caller's own, and those shared with the project. */
export function savedSearchesQuery(projectId: string) {
  return queryOptions({
    queryKey: [...SAVED_SEARCHES, projectId],
    queryFn: async () =>
      unwrap(
        await iki.GET("/api/v1/saved-searches", { params: { query: { project: projectId } } }),
      ),
  });
}

/** Entities a reference may name, for a picker: by HRID or the text in their title, of any
 * one of its types (ADR 0136), or any entity for none or `iki/core.Entity`. No placeholder:
 * until the answer for `q` comes, a picker shows none, never another text's answer, which
 * would change under someone choosing from it. */
export function pickerQuery(types: readonly string[], q: string) {
  const named = types.includes("iki/core.Entity") ? [] : [...types];
  return queryOptions({
    queryKey: [...ENTITIES, "picker", named.join(" "), q.trim()],
    queryFn: async () =>
      unwrap(
        await iki.GET("/api/v1/entities", {
          params: {
            query: {
              limit: 20,
              archived: false,
              ...(named.length === 1 && named[0]
                ? { type: named[0] }
                : named.length
                  ? { types: named }
                  : {}),
              ...(q.trim() ? { q: q.trim() } : {}),
            },
          },
        }),
      ).items,
    staleTime: 30_000,
  });
}

/**
 * By ID or HRID, as in the page's address. A registry entry's records share its HRID
 * (ADR 0115), so `project` (a project's slug) says whose record; without it, an HRID that
 * names records in several of the reader's projects answers 409 (`choose_project`).
 */
export function entityQuery(ref: string, project?: string) {
  return queryOptions({
    queryKey: [...ENTITIES, "one", ref.toUpperCase(), project ?? null],
    queryFn: async () => {
      const id = project ? (await queryClient.query(projectQuery(project))).id : undefined;
      return unwrap(
        await iki.GET("/api/v1/entities/{ref}", {
          params: { path: { ref }, query: id ? { project: id } : {} },
        }),
      );
    },
  });
}

/** An entity by its ID, or a project's record of a registry entry by the entry's ID, in the
 * project with ID `project`: a view's targets are entries, shown as its project's records. */
export function recordQuery(ref: string, project?: string) {
  return queryOptions({
    queryKey: [...ENTITIES, "record", ref.toUpperCase(), project ?? null],
    queryFn: async () =>
      unwrap(
        await iki.GET("/api/v1/entities/{ref}", {
          params: { path: { ref }, query: project ? { project } : {} },
        }),
      ),
  });
}

/** How the API names an entity unambiguously: a record of a registry entry by its ID, since
 * other projects' records have its HRID too; anything else by its HRID. */
export function apiRef(entity: Pick<Entity, "id" | "hrid" | "entry">): string {
  return entity.entry ? entity.id : entity.hrid;
}

/** An entity page's `?project=`: a slug, for a record of a registry entry (ADR 0115). */
export interface EntityPageSearch {
  project?: string | undefined;
  /** The signing the identity provider sent the person back from (ADR 0126). */
  signing?: string | undefined;
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function validateEntityPageSearch(search: Record<string, unknown>): EntityPageSearch {
  const out: EntityPageSearch = {};
  if (typeof search["project"] === "string" && search["project"]) {
    out.project = search["project"].slice(0, 100);
  }
  if (typeof search["signing"] === "string" && UUID.test(search["signing"])) {
    out.signing = search["signing"];
  }
  return out;
}

/** The `?project=` a link to an entity carries: a record's project, so the link opens that
 * project's record of the entry rather than asking which. Other entities need none. */
export function recordSearch(
  entity: { entry?: EntryRef | null | undefined; owner_project?: { slug: string } | null },
  fallback?: string,
): EntityPageSearch {
  if (!entity.entry) return {};
  const slug = entity.owner_project?.slug ?? fallback;
  return slug ? { project: slug } : {};
}

/** Whether a type's entities are records of registry entries (ADR 0115). */
export function hasIdentity(types: EntityType[] | undefined, name: string): boolean {
  return Boolean(types?.find((t) => t.name === name)?.identity.length);
}

export function provenanceQuery(ref: string) {
  return queryOptions({
    queryKey: [...ENTITIES, "provenance", ref.toUpperCase()],
    queryFn: async () =>
      unwrap(await iki.GET("/api/v1/entities/{ref}/provenance", { params: { path: { ref } } })),
  });
}

/** Where runs imported it from, newest first. */
export function originsQuery(ref: string) {
  return queryOptions({
    queryKey: [...ENTITIES, "origins", ref.toUpperCase()],
    queryFn: async () =>
      unwrap(await iki.GET("/api/v1/entities/{ref}/origins", { params: { path: { ref } } })),
  });
}

/** How it was made, one tool at a time (ADR 0106): Iki's records first, then sources'. */
export function makingQuery(ref: string) {
  return queryOptions({
    queryKey: [...ENTITIES, "making", ref.toUpperCase()],
    queryFn: async () =>
      unwrap(await iki.GET("/api/v1/entities/{ref}/making", { params: { path: { ref } } })),
  });
}

/** The bundles from other deployments that made it here, or added to its history. */
export function bundleOriginsQuery(ref: string) {
  return queryOptions({
    queryKey: [...ENTITIES, "bundles", ref.toUpperCase()],
    queryFn: async () =>
      unwrap(await iki.GET("/api/v1/entities/{ref}/bundles", { params: { path: { ref } } })),
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

/** An entity at a revision. `project` (an ID) says whose record of a registry entry: a
 * view's targets are entries, pinned in the view's own project (ADR 0115). */
export function revisionQuery(ref: string, number: number, project?: string) {
  return queryOptions({
    queryKey: [...ENTITIES, "revision", ref.toUpperCase(), number, project ?? null],
    queryFn: async () =>
      unwrap(
        await iki.GET("/api/v1/entities/{ref}/revisions/{number}", {
          params: { path: { ref, number }, query: project ? { project } : {} },
        }),
      ),
    // A revision never changes.
    staleTime: Infinity,
  });
}

/** Which entities a list shows: ?show=archived|all. */
export type Showing = "current" | "archived" | "all";

/** The lists' order when their address names none: last changed, newest first (ADR 0176),
 * so recent work is at the top. An index on when records changed serves it across types. */
export const DEFAULT_SORT = { field: "updated_at", descending: true } as const;

export interface EntitySearch {
  type?: string | undefined;
  show?: Showing | undefined;
  q?: string | undefined;
  filters?: Filter[] | undefined;
  sort?: string | undefined;
  desc?: boolean | undefined;
  /** A collection's members, by its HRID. */
  collection?: string | undefined;
  /** A plugin's component to show them in, by its name, in place of the grid. */
  view?: string | undefined;
}

/** A plugin's component's name: `acme/lab.box_map`. */
export const COMPONENT_NAME = /^[a-z][a-z0-9_]*\/[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$/;

export function validateEntitySearch(search: Record<string, unknown>): EntitySearch {
  const out: EntitySearch = {};
  if (typeof search["type"] === "string" && search["type"]) out.type = search["type"];
  if (search["show"] === "archived" || search["show"] === "all") out.show = search["show"];
  if (typeof search["q"] === "string" && search["q"]) out.q = search["q"].slice(0, 200);
  const filters = validateFilters(search["filters"]);
  if (filters) out.filters = filters;
  if (typeof search["sort"] === "string" && search["sort"]) out.sort = search["sort"];
  if (search["desc"] === true) out.desc = true;
  if (typeof search["collection"] === "string" && search["collection"]) {
    out.collection = search["collection"].slice(0, 100);
  }
  const view = search["view"];
  if (typeof view === "string" && view.length <= 200 && COMPONENT_NAME.test(view)) out.view = view;
  return out;
}

/** A new entity's form: ?type=iki/dna.DNASequence. */
export interface NewEntitySearch {
  type?: string | undefined;
}

export function validateNewEntitySearch(search: Record<string, unknown>): NewEntitySearch {
  return typeof search["type"] === "string" && search["type"] ? { type: search["type"] } : {};
}

// --- Reading values ---------------------------------------------------------------

/** Drops floating-point noise from a conversion (52.341000000000001 → 52.341). */
function clean(value: number): number {
  return Number(value.toPrecision(12));
}

const number = new Intl.NumberFormat("en-GB", { maximumFractionDigits: 20 });
const SUPERSCRIPT: Record<string, string> = {
  "-": "⁻",
  "0": "⁰",
  "1": "¹",
  "2": "²",
  "3": "³",
  "4": "⁴",
  "5": "⁵",
  "6": "⁶",
  "7": "⁷",
  "8": "⁸",
  "9": "⁹",
};

/**
 * A number as people read it. Below a millionth, it's in scientific notation with
 * every digit it has (1.5 × 10⁻²¹), since decimals would show it as 0 or drop digits.
 */
export function formatNumber(value: number): string {
  if (value !== 0 && Math.abs(value) < 1e-6) {
    // toExponential gives the shortest digits that read back as the same number.
    const [mantissa = "", exponent = ""] = value.toExponential().split("e");
    const power = exponent.replace(/[-0-9]/g, (c) => SUPERSCRIPT[c] ?? c);
    return `${number.format(Number(mantissa))} × 10${power}`;
  }
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
  entity?: Pick<Entity, "refs" | "files" | "people"> & Partial<Pick<Entity, "request">>,
): string {
  if (value === null || value === undefined) return "";
  if (Array.isArray(value) && field.items === "row") {
    // A list of rows (ADR 0137): each row's values, rows apart by semicolons.
    const cells = field.row?.fields ?? [];
    return value
      .map((row) =>
        cells
          .map((c) => valueText(c, (row as Record<string, unknown>)[c.name], entity))
          .filter(Boolean)
          .join(" · "),
      )
      .join("; ");
  }
  // A list of objects, such as a plasmid's features: how many, and their names, never JSON.
  const objects = objectList(value);
  if (objects) return objectListText(objects);
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
    case "person": {
      const found = entity?.people?.[value as string];
      if (!found) return "Someone you can’t see";
      return found.active ? found.name : `${found.name} (deactivated)`;
    }
    case "json":
      return JSON.stringify(value);
    case "state":
      return unsentState(entity, field) ? NOT_SENT : stateLabel(field, value as string);
    default:
      return typeof value === "string" ? value : JSON.stringify(value);
  }
}

/**
 * A request's service state (ADR 0132) before the request is sent: nobody has set it. It
 * holds its type's initial state, whose name, such as Submitted, would say it had been
 * sent, so pages say it's not sent instead.
 */
export function unsentState(
  entity:
    | {
        request?: Pick<Schemas["RequestSidesOut"], "service_fields" | "submitted"> | null;
      }
    | undefined,
  field: Pick<FieldDef, "name" | "type">,
): boolean {
  const sides = entity?.request;
  return Boolean(
    sides &&
    !sides.submitted &&
    field.type === "state" &&
    sides.service_fields.includes(field.name),
  );
}

/** How an unsent request's service state reads, as a badge or a value. */
export const NOT_SENT = "Not sent";

/** A state field's state (ADR 0125), as people read it: its label, or its name. */
export function stateLabel(field: Pick<FieldDef, "states">, state: string): string {
  return field.states.find((s) => s.name === state)?.label ?? state;
}

/** A type's state fields (ADR 0125), in its order. */
export function stateFields(type: EntityType | undefined): FieldDef[] {
  return (type?.fields ?? []).filter((f) => f.type === "state");
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

/** "iki/dna.DNASequence" → the type's label, or the name if it isn't installed. */
export function typeLabel(types: EntityType[] | undefined, name: string): string {
  return types?.find((t) => t.name === name)?.label ?? name;
}

/** A type's name as lists show it: its label, and its plugin's name when another installed
 * type has the same label, so two "Sample"s can be told apart. */
export interface TypeDisplayName {
  label: string;
  /** Its plugin, when its label alone is ambiguous. */
  plugin?: string;
}

export function typeDisplayName(
  type: Pick<EntityType, "name" | "label" | "plugin">,
  allTypes: readonly Pick<EntityType, "name" | "label">[],
): TypeDisplayName {
  const label = type.label.trim().toLowerCase();
  const shared = allTypes.some(
    (other) => other.name !== type.name && other.label.trim().toLowerCase() === label,
  );
  return shared ? { label: type.label, plugin: type.plugin } : { label: type.label };
}

/** The same as one string, where muted text can't show: an option, an accessible name. */
export function typeDisplayText(
  type: Pick<EntityType, "name" | "label" | "plugin">,
  allTypes: readonly Pick<EntityType, "name" | "label">[],
): string {
  const shown = typeDisplayName(type, allTypes);
  return shown.plugin ? `${shown.label} (${shown.plugin})` : shown.label;
}
