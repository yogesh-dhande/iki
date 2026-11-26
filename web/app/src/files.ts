/**
 * Files (ADR 0177): a project's, or every one its reader can read, found by part of their
 * name, format, when their data was acquired, sample and run; and what uses each.
 */

import { unwrap, type Schemas } from "@ikisci/sdk";
import { infiniteQueryOptions, keepPreviousData, queryOptions } from "@tanstack/react-query";

import { iki } from "./api";

export type FileRow = Schemas["FileRowOut"];
export type FileDetail = Schemas["FileDetailOut"];
export type HoldingRecord = Schemas["HoldingRecordOut"];
export type UsingRun = Schemas["UsingRunOut"];
export type FileSort = NonNullable<Schemas["FileFilters"]["sort"]>;
export type FileFacts = Schemas["FileFactsOut"];
export type ReadGroup = Schemas["ReadGroupOut"];

export const FILES = ["files"] as const;

/** How a list of files is narrowed: what its toolbar and its address hold. */
export interface FileFilter {
  q: string;
  /** Format IDs, media types or families such as `image/*`. */
  format: string[];
  acquiredFrom?: string | undefined;
  acquiredTo?: string | undefined;
  /** A sample's ID or HRID. */
  sample?: string | undefined;
  /** A run's ID or HRID. */
  run?: string | undefined;
  sort: FileSort;
}

/** What a field or param takes, for a picker: only those files are listed. */
export interface Takes {
  accepts: readonly string[];
  maxSize?: number | undefined;
}

export const NO_FILTER: FileFilter = { q: "", format: [], sort: "newest" };

export const SORTS: { value: FileSort; label: string }[] = [
  { value: "newest", label: "Newest added" },
  { value: "oldest", label: "Oldest added" },
  { value: "name", label: "Name, A to Z" },
  { value: "name_desc", label: "Name, Z to A" },
  { value: "acquired", label: "Latest acquired" },
  { value: "acquired_oldest", label: "Earliest acquired" },
];

const PAGE = 50;

function query(projectId: string | undefined, filter: FileFilter, takes?: Takes) {
  return {
    ...(projectId ? { project: projectId } : {}),
    ...(filter.q.trim() ? { q: filter.q.trim() } : {}),
    ...(filter.format.length ? { format: filter.format } : {}),
    ...(filter.acquiredFrom ? { acquired_from: filter.acquiredFrom } : {}),
    ...(filter.acquiredTo ? { acquired_to: filter.acquiredTo } : {}),
    ...(filter.sample ? { sample: filter.sample } : {}),
    ...(filter.run ? { run: filter.run } : {}),
    ...(takes?.accepts.length ? { accepts: [...takes.accepts] } : {}),
    ...(takes?.maxSize != null ? { max_size: takes.maxSize } : {}),
  };
}

function key(projectId: string | undefined, filter: FileFilter, takes?: Takes) {
  return JSON.stringify({ projectId, ...filter, takes });
}

/** A list of files, a page at a time. */
export function filesQuery(projectId: string | undefined, filter: FileFilter, takes?: Takes) {
  return infiniteQueryOptions({
    queryKey: [...FILES, "list", key(projectId, filter, takes)],
    queryFn: async ({ pageParam, signal }) =>
      unwrap(
        await iki.GET("/api/v1/files", {
          params: {
            query: {
              ...query(projectId, filter, takes),
              sort: filter.sort,
              limit: PAGE,
              ...(pageParam ? { cursor: pageParam } : {}),
            },
          },
          signal,
        }),
      ),
    initialPageParam: "",
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    // A search shows the last answer until the next comes, so rows don't flash away.
    placeholderData: keepPreviousData,
  });
}

/** How many files match, up to 1,000. */
export function fileCountQuery(projectId: string | undefined, filter: FileFilter, takes?: Takes) {
  return queryOptions({
    queryKey: [...FILES, "count", key(projectId, { ...filter, sort: "newest" }, takes)],
    queryFn: async ({ signal }) =>
      unwrap(
        await iki.GET("/api/v1/files/count", {
          params: { query: query(projectId, filter, takes) },
          signal,
        }),
      ),
    placeholderData: keepPreviousData,
  });
}

/** The formats of the files that match, whatever format is chosen, with how many each. */
export function fileFormatsQuery(projectId: string | undefined, filter: FileFilter, takes?: Takes) {
  const others = { ...filter, format: [], sort: "newest" as const };
  return queryOptions({
    queryKey: [...FILES, "formats", key(projectId, others, takes)],
    queryFn: async ({ signal }) =>
      unwrap(
        await iki.GET("/api/v1/files/formats", {
          params: { query: query(projectId, others, takes) },
          signal,
        }),
      ),
    placeholderData: keepPreviousData,
  });
}

export function fileQuery(id: string) {
  return queryOptions({
    queryKey: [...FILES, "one", id],
    queryFn: async ({ signal }) =>
      unwrap(
        await iki.GET("/api/v1/files/{file_id}", { params: { path: { file_id: id } }, signal }),
      ),
    refetchInterval: (query) => (query.state.data?.scan.state === "pending" ? 2000 : false),
  });
}

export function fileRecordsQuery(id: string) {
  return infiniteQueryOptions({
    queryKey: [...FILES, "records", id],
    queryFn: async ({ pageParam, signal }) =>
      unwrap(
        await iki.GET("/api/v1/files/{file_id}/records", {
          params: { path: { file_id: id }, query: pageParam ? { cursor: pageParam } : {} },
          signal,
        }),
      ),
    initialPageParam: "",
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
}

export function fileRunsQuery(id: string) {
  return infiniteQueryOptions({
    queryKey: [...FILES, "runs", id],
    queryFn: async ({ pageParam, signal }) =>
      unwrap(
        await iki.GET("/api/v1/files/{file_id}/runs", {
          params: { path: { file_id: id }, query: pageParam ? { cursor: pageParam } : {} },
          signal,
        }),
      ),
    initialPageParam: "",
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
}

/** Where a file downloads from: a redirect to a short-lived link, under its name. */
export function downloadHref(id: string): string {
  return `/api/v1/files/${id}/content`;
}

/** "2026-03-14 09:21:07", "2026-03-14", "2026-03-14 09:21 +01:00": when its data was
 * acquired, as precisely as its source said, in its own time. Never converted to the
 * reader's zone: most instruments record their local time without one. */
export function acquiredText(at: string): string {
  const match =
    /^(\d{4}-\d{2}-\d{2})(?:T(\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)(Z|[+-]\d{2}:\d{2})?)?$/.exec(at);
  if (!match) return at;
  const [, day, time, offset] = match;
  return [day, time, offset === "Z" ? "UTC" : offset].filter(Boolean).join(" ");
}

/** What a file can state about where its data came from (ADR 0224), in the order its page
 * shows them. */
export const FACTS: readonly (readonly [keyof FileFacts, string])[] = [
  ["instrument", "Instrument"],
  ["manufacturer", "Manufacturer"],
  ["serial", "Serial number"],
  ["instrument_name", "Instrument name"],
  ["operator", "Operator"],
  ["software", "Software"],
  ["protocol", "Protocol"],
];

/** The facts a file states, each with its label, leaving out what it doesn't state. */
export function statedFacts(
  facts: FileFacts,
): { key: keyof FileFacts; label: string; value: string; statedIn: string }[] {
  return FACTS.flatMap(([key, label]) => {
    const fact = facts[key];
    return fact ? [{ key, label, value: fact.value, statedIn: fact.stated_in }] : [];
  });
}

/** "Aurora · N0354 · Admin": a Files list row's instrument, serial number and operator,
 * where its file states them, or "" when it states none. */
export function factsLine(facts: FileFacts): string {
  const instrument = facts.instrument ?? facts.instrument_name;
  return [instrument, facts.serial, facts.operator]
    .flatMap((fact) => (fact ? [fact.value] : []))
    .join(" · ");
}

/** The SAM specification's @RG tags (section 1.3), as a lab would name each. */
export const READ_GROUP_TAGS: readonly (readonly [keyof ReadGroup, string])[] = [
  ["ID", "ID"],
  ["SM", "Sample"],
  ["LB", "Library"],
  ["PL", "Platform"],
  ["PM", "Platform model"],
  ["PU", "Platform unit"],
  ["BC", "Barcode"],
  ["CN", "Sequencing centre"],
  ["DT", "Run date"],
  ["PI", "Insert size"],
  ["PG", "Program"],
  ["KS", "Key sequence"],
  ["FO", "Flow order"],
  ["DS", "Description"],
];

/** The tags any of the read groups gives, in {@link READ_GROUP_TAGS} order: its table's
 * columns. */
export function readGroupColumns(
  groups: readonly ReadGroup[],
): (readonly [keyof ReadGroup, string])[] {
  return READ_GROUP_TAGS.filter(([tag]) => groups.some((group) => group[tag] !== undefined));
}

/** "2 records · 1 run", "Not used", for a file's Used by. */
export function usedByText(used: { records: number; runs: number }): string {
  const parts = [
    used.records ? `${String(used.records)} ${used.records === 1 ? "record" : "records"}` : "",
    used.runs ? `${String(used.runs)} ${used.runs === 1 ? "run" : "runs"}` : "",
  ].filter(Boolean);
  return parts.length ? parts.join(" · ") : "Not used";
}

/** "r1–r2", "r5 on", "r3": a span of a record's revisions that held a file. */
export function spanText(span: { first: number; last: number | null }): string {
  if (span.last === null) return `r${String(span.first)} on`;
  if (span.last === span.first) return `r${String(span.first)}`;
  return `r${String(span.first)}–r${String(span.last)}`;
}

/** From an address's search: a list's filter, with anything unknown left out. */
export function validateFileSearch(search: Record<string, unknown>): {
  q?: string;
  format?: string;
  acquired_from?: string;
  acquired_to?: string;
  sample?: string;
  run?: string;
  sort?: FileSort;
  project?: string;
} {
  const text = (name: string) =>
    typeof search[name] === "string" && search[name] ? { [name]: search[name] } : {};
  const day = (name: string) =>
    typeof search[name] === "string" && /^\d{4}-\d{2}-\d{2}$/.test(search[name])
      ? { [name]: search[name] }
      : {};
  const sort = search["sort"];
  return {
    ...text("q"),
    ...text("format"),
    ...day("acquired_from"),
    ...day("acquired_to"),
    ...text("sample"),
    ...text("run"),
    ...text("project"),
    ...(typeof sort === "string" && SORTS.some((s) => s.value === sort)
      ? { sort: sort as FileSort }
      : {}),
  };
}

export type FileSearch = ReturnType<typeof validateFileSearch>;

/** An address's search as a list's filter. Formats are a comma-separated list there. */
export function filterOf(search: FileSearch): FileFilter {
  return {
    q: search.q ?? "",
    format: search.format ? search.format.split(",").filter(Boolean) : [],
    acquiredFrom: search.acquired_from,
    acquiredTo: search.acquired_to,
    sample: search.sample,
    run: search.run,
    sort: search.sort ?? "newest",
  };
}

/** A list's filter as an address's search: the defaults left out. */
export function searchOf(filter: FileFilter): FileSearch {
  return {
    ...(filter.q ? { q: filter.q } : {}),
    ...(filter.format.length ? { format: filter.format.join(",") } : {}),
    ...(filter.acquiredFrom ? { acquired_from: filter.acquiredFrom } : {}),
    ...(filter.acquiredTo ? { acquired_to: filter.acquiredTo } : {}),
    ...(filter.sample ? { sample: filter.sample } : {}),
    ...(filter.run ? { run: filter.run } : {}),
    ...(filter.sort !== "newest" ? { sort: filter.sort } : {}),
  };
}
