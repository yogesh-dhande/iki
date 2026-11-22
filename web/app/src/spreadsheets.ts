/**
 * Importing a spreadsheet into records (ADR 0178): the import's queries and changes, and
 * what its steps say. The steps themselves are components (pages/spreadsheet-import.tsx) a
 * plugin's page can use too.
 */

import { unwrap, type Schemas } from "@ikisci/sdk";
import { infiniteQueryOptions, queryOptions } from "@tanstack/react-query";

import { iki } from "./api";

export type SheetImport = Schemas["SpreadsheetImportOut"];
export type SheetColumn = Schemas["SheetColumnOut"];
export type SheetField = Schemas["FieldTargetOut"];
export type SheetLocale = Schemas["SheetLocaleOut"];
export type PreviewRow = Schemas["PreviewRowOut"];
export type SheetProblem = Schemas["SheetProblemOut"];
export type Outcome = PreviewRow["outcome"];
export type ColumnChoice = Schemas["SheetColumnIn"];
export type SheetChange = Schemas["SpreadsheetImportChange"];
export type SheetLocaleChange = Schemas["SheetLocaleIn"];

/** The files an import reads: Excel workbooks and CSV files (and TSV, as CSV). */
export const WORKBOOK_TYPES = [
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  "text/csv",
] as const;
export const WORKBOOK_ACCEPT = ".xlsx,.xlsm,.csv,.tsv,.txt," + WORKBOOK_TYPES.join(",");

export const SPREADSHEETS = "spreadsheet-imports";

/** Where an import is, said for people. */
export const STATES: Record<SheetImport["state"], string> = {
  reading: "Reading the workbook",
  mapping: "Choosing what to import",
  previewing: "Checking its rows",
  previewed: "Checked",
  committing: "Starting the import",
  importing: "Importing",
  imported: "Imported",
  failed: "Failed",
  cancelled: "Cancelled",
  discarded: "Discarded",
};

/** What importing a row does, as the preview counts it. */
export const OUTCOMES: Record<Outcome, { label: string; one: string; many: string }> = {
  create: { label: "New record", one: "new record", many: "new records" },
  register: {
    label: "New registry entry",
    one: "new record of a new registry entry",
    many: "new records of new registry entries",
  },
  add: {
    label: "Registered already",
    one: "new record of an entry registered already",
    many: "new records of entries registered already",
  },
  reuse: {
    label: "In the project already",
    one: "record the project has already, kept as it is",
    many: "records the project has already, kept as they are",
  },
  same: {
    label: "Same as an earlier row",
    one: "row the same as an earlier one",
    many: "rows the same as earlier ones",
  },
  problem: { label: "Has problems", one: "row with problems", many: "rows with problems" },
};

/** What's still going on, so it's asked again until it's done. */
export function busy(state: SheetImport["state"] | undefined): boolean {
  return (
    state === "reading" || state === "previewing" || state === "committing" || state === "importing"
  );
}

export function importQuery(id: string) {
  return queryOptions({
    queryKey: [SPREADSHEETS, id],
    queryFn: async () =>
      unwrap(
        await iki.GET("/api/v1/spreadsheet-imports/{import_id}", {
          params: { path: { import_id: id } },
        }),
      ),
    refetchInterval: (query) => (busy(query.state.data?.state) ? 1000 : false),
  });
}

// Keyed by its round's counts too: a preview run again finds other rows.
export function rowsQuery(made: SheetImport, outcome: Outcome | undefined) {
  return infiniteQueryOptions({
    queryKey: [SPREADSHEETS, made.id, "rows", made.state, made.updated_at, outcome ?? "all"],
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam }) =>
      unwrap(
        await iki.GET("/api/v1/spreadsheet-imports/{import_id}/rows", {
          params: {
            path: { import_id: made.id },
            query: {
              limit: 100,
              ...(outcome ? { outcome: [outcome] } : {}),
              ...(pageParam ? { cursor: pageParam } : {}),
            },
          },
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
}

export async function startImport(projectId: string, file: string, type: string) {
  return unwrap(
    await iki.POST("/api/v1/projects/{project_id}/spreadsheet-imports", {
      params: { path: { project_id: projectId } },
      body: { file, type },
    }),
  );
}

export async function changeImport(id: string, change: SheetChange) {
  return unwrap(
    await iki.PATCH("/api/v1/spreadsheet-imports/{import_id}", {
      params: { path: { import_id: id } },
      body: change,
    }),
  );
}

export async function previewImport(id: string) {
  return unwrap(
    await iki.POST("/api/v1/spreadsheet-imports/{import_id}/preview", {
      params: { path: { import_id: id } },
    }),
  );
}

export async function commitImport(id: string, body: { skip_problems: boolean; reason: string }) {
  return unwrap(
    await iki.POST("/api/v1/spreadsheet-imports/{import_id}/commit", {
      params: { path: { import_id: id } },
      body,
    }),
  );
}

export async function discardImport(id: string) {
  unwrap(
    await iki.DELETE("/api/v1/spreadsheet-imports/{import_id}", {
      params: { path: { import_id: id } },
    }),
  );
}

export async function saveMapping(id: string, name: string, replace?: string) {
  return unwrap(
    await iki.POST("/api/v1/spreadsheet-imports/{import_id}/mappings", {
      params: { path: { import_id: id } },
      body: { name, ...(replace ? { replace } : {}) },
    }),
  );
}

// --- What the steps say -------------------------------------------------------------------

export function plural(count: number, one: string, many: string): string {
  return `${count.toLocaleString("en")} ${count === 1 ? one : many}`;
}

/** A column's name as people read it: its letter, then its header if it has one. */
export function columnName(column: Pick<SheetColumn, "letter" | "header">): string {
  return column.header === `Column ${column.letter}`
    ? column.header
    : `${column.letter}: ${column.header}`;
}

/** The columns as `PATCH` takes them: each as it is, but `index`'s set to `field`. */
export function columnsWith(
  columns: readonly SheetColumn[],
  index: number,
  change: Partial<ColumnChoice>,
): ColumnChoice[] {
  return columns.map((c) => {
    const kept: ColumnChoice = {
      index: c.index,
      field: c.field,
      unit: c.unit,
      separator: c.separator === ";" ? null : c.separator,
      dates: c.dates as NonNullable<ColumnChoice["dates"]> | null,
      time_zone: c.time_zone,
      decimal: c.decimal as NonNullable<ColumnChoice["decimal"]> | null,
      serial_dates: c.serial_dates,
      values: Object.keys(c.values).length ? c.values : null,
    };
    if (c.index !== index) return kept;
    // A column given another field reads as that one does: its options were the other's.
    const fresh: ColumnChoice =
      "field" in change && change.field !== c.field ? { index: c.index, field: null } : kept;
    return { ...fresh, ...change };
  });
}

/**
 * Rows to leave out, as people write them ("5-7, 12"), as the API takes them ([[5, 7],
 * [12, 12]]). Throws an Error saying what's wrong.
 */
export function parseRanges(text: string): number[][] {
  const out: number[][] = [];
  for (const part of text.split(/[,;\s]+/)) {
    if (!part) continue;
    const found = /^(\d+)(?:[-–](\d+))?$/.exec(part);
    if (!found) throw new Error(`“${part}” isn’t a row or rows, such as 5 or 5-7.`);
    const from = Number(found[1]);
    const to = found[2] ? Number(found[2]) : from;
    if (from < 1 || to < from) throw new Error(`“${part}” isn’t rows from first to last.`);
    out.push([from, to]);
  }
  if (out.length > 1000) throw new Error("Give at most 1,000 rows or ranges.");
  return out;
}

export function rangesText(ranges: readonly (readonly number[])[]): string {
  return ranges.map(([a, b]) => (a === b ? String(a) : `${String(a)}-${String(b)}`)).join(", ");
}

/** What the preview found, in the order the person reads it. */
export function counted(made: SheetImport): { outcome: Outcome; count: number }[] {
  const counts = made.preview?.counts ?? {};
  return (Object.keys(OUTCOMES) as Outcome[])
    .map((outcome) => ({ outcome, count: counts[outcome] ?? 0 }))
    .filter((c) => c.count > 0);
}

/** Rows a commit writes or keeps: everything but the rows with problems. */
export function importable(made: SheetImport): number {
  const counts = made.preview?.counts ?? {};
  return (["create", "register", "add", "reuse", "same"] as const).reduce(
    (sum, o) => sum + (counts[o] ?? 0),
    0,
  );
}

/** The locale's choices, as the steps offer them. */
export const DECIMALS = [
  { value: "", label: "Not said yet" },
  { value: "point", label: "A decimal point: 1,234.5" },
  { value: "comma", label: "A decimal comma: 1.234,5" },
] as const;

export const DATE_ORDERS = [
  { value: "", label: "Not said yet" },
  { value: "day_first", label: "Day first: 31/12/2026" },
  { value: "month_first", label: "Month first: 12/31/2026" },
] as const;
