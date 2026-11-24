/**
 * Importing a spreadsheet into records (ADR 0178): the import's queries and changes, and
 * what its steps say. The steps themselves are components (pages/spreadsheet-import.tsx) a
 * plugin's page can use too.
 *
 * Each change is made from the revision of the import the person saw (If-Match), one at a
 * time (useImportSaves): a change sent first is saved first, and the page never goes back
 * to an older revision than one it has shown. If the import moved on elsewhere, in another
 * tab or a script, nothing is overwritten: the page shows what's saved, and what the person
 * chose that wasn't.
 */

import { ApiError, unwrap, type Schemas } from "@ikisci/sdk";
import {
  infiniteQueryOptions,
  queryOptions,
  replaceEqualDeep,
  useQueryClient,
} from "@tanstack/react-query";
import { useCallback, useRef, useState } from "react";

import { fieldErrors, iki, problemOf, signInAgain } from "./api";

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

/**
 * Whether `next` is older than `shown`: an earlier revision, or the same one as it was
 * before `shown` was written. A read sent before a change can be answered after it.
 */
export function older(shown: SheetImport | undefined, next: SheetImport): boolean {
  if (shown?.id !== next.id) return false;
  if (next.revision !== shown.revision) return next.revision < shown.revision;
  return Date.parse(next.updated_at) < Date.parse(shown.updated_at);
}

export function importQuery(id: string) {
  return queryOptions({
    queryKey: [SPREADSHEETS, id],
    queryFn: async ({ signal }) =>
      unwrap(
        await iki.GET("/api/v1/spreadsheet-imports/{import_id}", {
          params: { path: { import_id: id } },
          signal,
        }),
      ),
    refetchInterval: (query) => (busy(query.state.data?.state) ? 1000 : false),
    // Never back to what it was before something the page has shown.
    structuralSharing: (shown: unknown, next: unknown) =>
      older(shown as SheetImport | undefined, next as SheetImport)
        ? shown
        : replaceEqualDeep(shown, next),
  });
}

// Keyed by its round's counts too: a preview run again finds other rows.
export function rowsQuery(made: SheetImport, outcome: Outcome | undefined) {
  return infiniteQueryOptions({
    queryKey: [SPREADSHEETS, made.id, "rows", made.state, made.updated_at, outcome ?? "all"],
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam, signal }) =>
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
          signal,
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

/** If-Match: the revision a request is made from. */
function seen(revision: number) {
  return { "If-Match": `"${String(revision)}"` };
}

export async function changeImport(id: string, revision: number, change: SheetChange) {
  return unwrap(
    await iki.PATCH("/api/v1/spreadsheet-imports/{import_id}", {
      params: { path: { import_id: id }, header: seen(revision) },
      body: change,
    }),
  );
}

export async function previewImport(id: string, revision: number) {
  return unwrap(
    await iki.POST("/api/v1/spreadsheet-imports/{import_id}/preview", {
      params: { path: { import_id: id }, header: seen(revision) },
    }),
  );
}

export async function commitImport(
  id: string,
  revision: number,
  body: { skip_problems: boolean; reason: string },
) {
  return unwrap(
    await iki.POST("/api/v1/spreadsheet-imports/{import_id}/commit", {
      params: { path: { import_id: id }, header: seen(revision) },
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

export async function saveMapping(id: string, revision: number, name: string, replace?: string) {
  return unwrap(
    await iki.POST("/api/v1/spreadsheet-imports/{import_id}/mappings", {
      params: { path: { import_id: id }, header: seen(revision) },
      body: { name, ...(replace ? { replace } : {}) },
    }),
  );
}

/** It moved on from the revision a request was made from (412). */
export function isStale(error: unknown): error is ApiError {
  return error instanceof ApiError && error.problem.code === "stale";
}

// --- Saving changes in turn ---------------------------------------------------------------

/** What a change chooses, by the name `PATCH` gives it. */
export type ChoiceKey = keyof SheetChange;

/** A choice of the person's: what it says, for them, and the change it is, made of the
 * import as it is when it's sent, so a column's choice changes that column of the
 * columns saved then, not of those on screen when it was made. */
export interface Edit {
  said: string;
  keys: readonly ChoiceKey[];
  make: (latest: SheetImport) => SheetChange;
}

/** Makes an edit of a change, or of a way to make one of the import as it is then. */
export function edit(
  said: string,
  change: SheetChange | ((latest: SheetImport) => SheetChange),
  now: SheetImport,
): Edit {
  const make = typeof change === "function" ? change : () => change;
  return { said, keys: Object.keys(make(now)) as ChoiceKey[], make };
}

/** What each choice is called, as the steps label it. */
export const CHOICES: Record<ChoiceKey, string> = {
  sheet: "Sheet",
  header_row: "Header row",
  first_row: "First row",
  last_row: "Last row",
  skip: "Rows to leave out",
  columns: "Columns",
  mapping: "Saved mapping",
  locale: "Numbers and dates",
};

function columnChoice(c: SheetColumn) {
  return [
    c.index,
    c.field,
    c.unit,
    c.separator,
    c.dates,
    c.time_zone,
    c.decimal,
    c.serial_dates,
    c.values,
  ];
}

/** The choices that differ between two revisions of an import. */
export function moved(before: SheetImport, after: SheetImport): Set<ChoiceKey> {
  const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);
  const out = new Set<ChoiceKey>();
  for (const key of ["sheet", "header_row", "first_row", "last_row", "skip", "locale"] as const) {
    if (!same(before[key], after[key])) out.add(key);
  }
  if (!same(before.columns.map(columnChoice), after.columns.map(columnChoice))) {
    out.add("columns");
  }
  if (before.mapping?.id !== after.mapping?.id) out.add("mapping");
  return out;
}

/** The import as it is once `change` is saved, as far as the page can tell before then:
 * what the person chose, on what's saved. */
export function overlaid(made: SheetImport, change: SheetChange): SheetImport {
  const out: SheetImport = { ...made };
  if (change.sheet != null) out.sheet = change.sheet;
  if (change.header_row != null) out.header_row = change.header_row;
  if (change.first_row != null) out.first_row = change.first_row;
  if (change.last_row != null) out.last_row = change.last_row || null;
  if (change.skip != null) out.skip = change.skip;
  if (change.locale != null) out.locale = { ...made.locale, ...change.locale };
  const given = change.columns;
  if (given != null) {
    out.columns = made.columns.map((c) => {
      const chosen = given.find((g) => g.index === c.index);
      if (!chosen) return { ...c, field: null };
      return {
        ...c,
        field: chosen.field ?? null,
        unit: chosen.unit ?? null,
        separator: chosen.separator ?? ";",
        dates: chosen.dates ?? null,
        time_zone: chosen.time_zone ?? null,
        decimal: chosen.decimal ?? null,
        serial_dates: chosen.serial_dates ?? null,
        values: chosen.values ?? {},
      };
    });
  }
  return out;
}

/** Makes a change of the person's, said for them, of the import as it is when it's sent. */
export type Choose = (
  said: string,
  change: SheetChange | ((latest: SheetImport) => SheetChange),
) => void;

/** An edit not saved: on its way, refused as it is, or held back because the import moved
 * on elsewhere. */
export interface Unsaved {
  id: number;
  edit: Edit;
  /** Waiting its turn or sent; refused, saying what's wrong with it; or held back. */
  state: "saving" | "refused" | "held";
  /** Held back, and what it chose was changed elsewhere too. */
  clashes: boolean;
  /** The conflicts seen before it was made. */
  round: number;
  /** Why it was refused. */
  problem?: ApiError;
}

/** What's wrong with the person's choices, by where: each refused one's. */
export function refusedErrors(unsaved: readonly Unsaved[]): Record<string, string> {
  return Object.assign(
    {},
    ...unsaved.filter((u) => u.state === "refused").map((u) => fieldErrors(problemOf(u.problem))),
  ) as Record<string, string>;
}

/** The import moved on elsewhere: what changed there, and whether a preview was refused. */
export interface Moved {
  revision: number;
  changed: ChoiceKey[];
  preview: boolean;
}

class Halted extends Error {}

/**
 * Saves an import's changes one at a time, each from the revision the one before left it
 * at (If-Match), and keeps the page's copy of it at the latest. If the import moved on
 * elsewhere, nothing more of what was waiting is sent: the page shows what's saved, and
 * holds back what the person chose that wasn't, for them to save on what's there now or
 * let go.
 */
export function useImportSaves(id: string) {
  const client = useQueryClient();
  const tail = useRef<Promise<unknown>>(Promise.resolve());
  // One more with each conflict: what was waiting before it isn't sent.
  const round = useRef(0);
  const made = useRef(0);
  const [unsaved, setUnsaved] = useState<Unsaved[]>([]);
  // What went wrong that isn't about a choice: it's been imported, say.
  const [problem, setProblem] = useState<unknown>(null);
  const [previewProblem, setPreviewProblem] = useState<unknown>(null);
  const [conflict, setConflict] = useState<Moved | null>(null);
  const [previewing, setPreviewing] = useState(false);

  /** Someone else's revision: show it, and hold back what was waiting. */
  const catchUp = useCallback(
    async (known: SheetImport, before: number, preview: boolean) => {
      let fresh = known;
      try {
        fresh = await client.query({ ...importQuery(id), staleTime: 0 });
      } catch {
        // The page reads it again when it can.
      }
      const changed = moved(known, fresh);
      setUnsaved((list) =>
        list.map((u) =>
          u.state === "saving" && u.round <= before
            ? { ...u, state: "held", clashes: u.edit.keys.some((k) => changed.has(k)) }
            : u,
        ),
      );
      setConflict((was) => ({
        revision: fresh.revision,
        changed: [...new Set([...(was?.changed ?? []), ...changed])],
        preview: preview || (was?.preview ?? false),
      }));
    },
    [client, id],
  );

  /** Sends a request after those before it, from the revision their answers left. */
  const inTurn = useCallback(
    <T>(send: (latest: SheetImport) => Promise<T>, preview = false): Promise<T> => {
      const mine = round.current;
      const key = importQuery(id).queryKey;
      const turn = tail.current.then(async () => {
        const latest = client.getQueryData(key);
        if (mine !== round.current || !latest) throw new Halted();
        // A read on its way may be answered after this is saved.
        await client.cancelQueries({ queryKey: key, exact: true });
        try {
          return await send(latest);
        } catch (error) {
          if (isStale(error)) {
            // Now, before the next in turn looks: it isn't sent.
            round.current += 1;
            void catchUp(latest, mine, preview);
          } else if (error instanceof ApiError && error.status === 401) {
            signInAgain();
          }
          throw error;
        }
      });
      tail.current = turn.catch(() => undefined);
      return turn;
    },
    [client, id, catchUp],
  );

  const put = useCallback(
    (next: SheetImport) => {
      client.setQueryData(importQuery(id).queryKey, next);
    },
    [client, id],
  );

  const save = useCallback(
    (change: Edit) => {
      made.current += 1;
      const entry: Unsaved = {
        id: made.current,
        edit: change,
        state: "saving",
        clashes: false,
        round: round.current,
      };
      // A newer choice of the same thing replaces one refused or held back.
      setUnsaved((list) => [
        ...list.filter(
          (u) => u.state === "saving" || !u.edit.keys.some((k) => change.keys.includes(k)),
        ),
        entry,
      ]);
      setProblem(null);
      inTurn((latest) => changeImport(id, latest.revision, change.make(latest))).then(
        (next) => {
          put(next);
          setUnsaved((list) => list.filter((u) => u.id !== entry.id));
        },
        (error: unknown) => {
          // Held back: catchUp says so.
          if (error instanceof Halted || isStale(error)) return;
          if (error instanceof ApiError && Object.keys(fieldErrors(error.problem)).length) {
            // Kept as the person chose it, with what's wrong with it, until they change it.
            setUnsaved((list) =>
              list.map((u) => (u.id === entry.id ? { ...u, state: "refused", problem: error } : u)),
            );
            return;
          }
          setProblem(error);
          setUnsaved((list) => list.filter((u) => u.id !== entry.id));
          // It may have been saved, its answer lost: the page shows what's saved.
          void client.invalidateQueries({ queryKey: importQuery(id).queryKey, exact: true });
        },
      );
    },
    [client, id, inTurn, put],
  );

  /** Checks every row, once the changes before it are saved. */
  const preview = useCallback(() => {
    setPreviewing(true);
    setPreviewProblem(null);
    inTurn((latest) => previewImport(id, latest.revision), true)
      .then(put, (error: unknown) => {
        if (error instanceof Halted || isStale(error)) return;
        setPreviewProblem(error);
        void client.invalidateQueries({ queryKey: importQuery(id).queryKey, exact: true });
      })
      .finally(() => {
        setPreviewing(false);
      });
  }, [client, id, inTurn, put]);

  /** Saves what was held back, on what's saved now: but what was changed there too. */
  const saveHeld = useCallback(() => {
    const held = unsaved.filter((u) => u.state === "held" && !u.clashes);
    setUnsaved((list) => list.filter((u) => u.state !== "held"));
    setConflict(null);
    for (const u of held) save(u.edit);
  }, [save, unsaved]);

  const keepSaved = useCallback(() => {
    setUnsaved((list) => list.filter((u) => u.state === "saving"));
    setConflict(null);
  }, []);

  return {
    save,
    preview,
    inTurn,
    saveHeld,
    keepSaved,
    /** What's on its way, refused or held back. */
    unsaved,
    errors: refusedErrors(unsaved),
    saving: unsaved.some((u) => u.state === "saving"),
    previewing,
    problem,
    previewProblem,
    conflict,
  };
}

/** The import as the page shows it: what's saved, with the person's choices on their way
 * or held back on it, but those that clash with a change made elsewhere. */
export function shownWith(made: SheetImport, unsaved: readonly Unsaved[]): SheetImport {
  return unsaved
    .filter((u) => !u.clashes)
    .reduce((shown, u) => overlaid(shown, u.edit.make(shown)), made);
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

export type ImportSaves = ReturnType<typeof useImportSaves>;
