/**
 * Editing a type's records as a sheet (ADR 0228): what the records table's sheet holds,
 * and what each keystroke, paste and fill does to it. The sheet itself is a component
 * (pages/record-sheet.tsx); everything here is plain data, so it's tested on its own.
 *
 * It feels like a plain spreadsheet. A cell holds text, as it would be typed back: a
 * reference's HRID, a person's email, a number in full, a date in ISO 8601, a list's items
 * apart by semicolons. A paste fills right and down from the selected cell, in the copied
 * order, with no header matched: each cell's check, by the server, says what's wrong.
 * Nothing is written until Save, and every change until then can be undone.
 */

import { unwrap, type Schemas } from "@ikisci/sdk";

import { iki } from "./api";
import type { Entity, EntityType, FieldDef } from "./entities";
import { writableFields } from "./requests";

export type SheetLocale = Schemas["SheetEditLocaleIn"];
export type CheckRow = Schemas["SheetEditCheckRowIn"];
export type Checked = Schemas["SheetEditRowOut"];
export type CheckProblem = Schemas["SheetEditProblemOut"];
export type SaveRow = Schemas["SheetEditSaveRowIn"];
export type Saved = Schemas["SheetEditSaveOut"];

/** The columns only a new row has: where it's kept, and how much it starts with. */
export const LOCATION = "location";
export const AMOUNT = "amount";
/** The record's ID: the first column, never changed. */
export const ID = "id";
/** The most rows the API checks at once; a larger change is checked in parts. */
export const CHECK_ROWS = 200;
/** The most rows one save takes: more go through an import. */
export const SAVE_ROWS = 1000;
/** A list's items in a cell, as a spreadsheet import reads them. */
export const SEPARATOR = "; ";

// --- Columns ---------------------------------------------------------------------------

export interface SheetColumn {
  /** A field's name, or ID, LOCATION or AMOUNT. */
  key: string;
  /** Its heading: a quantity's with the unit its cells are in. */
  label: string;
  field?: FieldDef;
  /** Why none of its cells can change, if they can't. */
  readOnly?: string;
  /** Only a new row's cells change: an existing record's place and amount have their
   * own reasons and checks, on its page or in Storage. */
  newOnly?: boolean;
}

/** Why a field's cells can't change in a sheet, as the import says it can't map a column
 * to it; null if they can. */
export function whyNotInACell(field: FieldDef, service = false): string | null {
  if (field.detached) return "It’s been removed from the type.";
  if (field.computed) return "It’s computed: only its plugin’s actions set it.";
  if (field.type === "state") return "It’s a state: set it on the record’s page.";
  if (service) return "It’s the service’s: set once a request is submitted.";
  if (field.type === "file") return "It holds a file: add files on the record’s page.";
  if (field.type === "json" || field.items === "row") {
    return "It holds structured data that a cell can’t.";
  }
  if (field.type === "list" && ["file", "json", "list", "row"].includes(field.items ?? "")) {
    return "It’s a list of what a cell can’t hold.";
  }
  return null;
}

/** The unit a quantity's cells are in, as its heading says: its display unit, if it has
 * one, as the grid shows it. */
export function cellUnit(field: FieldDef): string {
  if (field.display_scale != null && field.display_unit) {
    return field.display_unit_symbol ?? field.display_unit;
  }
  return field.unit_symbol ?? field.unit ?? "";
}

/** A field's heading, as the type's import template has it. */
export function heading(field: FieldDef): string {
  const unit = field.type === "quantity" ? cellUnit(field) : "";
  return unit ? `${field.label} (${unit})` : field.label;
}

/** Whether a new record of the type can say where it's kept: it's kept in storage, and
 * putting one away needs no scan. */
export function placeable(type: EntityType): boolean {
  return Boolean(type.storage && !type.storage.confirm.includes("move"));
}

/**
 * The sheet's columns, in the order a paste fills them: the ID, the fields a cell can
 * change in the type's order (as its import template lists them), a new row's place and
 * amount, then the fields it shows but can't change.
 */
export function sheetColumns(type: EntityType): SheetColumn[] {
  const writable = new Set(writableFields(type).map((f) => f.name));
  const service = new Set(type.request?.service_fields ?? []);
  const shown = type.fields.filter((f) => !f.detached);
  const editable = shown.filter((f) => writable.has(f.name) && !whyNotInACell(f));
  const columns: SheetColumn[] = [
    { key: ID, label: "ID", readOnly: "A record’s ID never changes." },
    ...editable.map((f) => ({ key: f.name, label: heading(f), field: f })),
  ];
  if (placeable(type)) columns.push({ key: LOCATION, label: "Location", newOnly: true });
  if (type.amount) {
    const unit = type.amount.display_unit ?? type.amount.unit;
    columns.push({ key: AMOUNT, label: `${type.amount.label} (${unit})`, newOnly: true });
  }
  for (const f of shown) {
    if (editable.includes(f)) continue;
    const why =
      whyNotInACell(f, service.has(f.name)) ??
      (writable.has(f.name) ? null : "It’s the other side’s of the request.");
    columns.push({ key: f.name, label: heading(f), field: f, ...(why ? { readOnly: why } : {}) });
  }
  return columns;
}

// --- Cells' text -----------------------------------------------------------------------

/** A number in full, as a spreadsheet reads it back: 37, -20, 11.981. */
function numberText(value: number): string {
  return String(value);
}

/** A quantity in the unit its cells are in. Twelve significant digits, as the grid, so
 * 11981 u is 11.981 kDa, not 11.981000000000002. */
function inCellUnit(field: FieldDef, value: number): number {
  if (field.display_scale == null || !field.display_unit) return value;
  const converted = value * field.display_scale + (field.display_offset ?? 0);
  return Number(converted.toPrecision(12));
}

type Named = Pick<Entity, "refs"> & Partial<Pick<Entity, "people">>;

/**
 * A value as a cell has it, to be typed back and read the same, as the server says what
 * a check read (`shown`): a reference by its HRID, a person by their email (their ID when
 * it can't be seen), numbers in full, dates and times in ISO 8601, a list's items apart
 * by semicolons.
 */
export function cellText(field: FieldDef, value: unknown, entity?: Named): string {
  if (value === null || value === undefined) return "";
  if (Array.isArray(value)) {
    const item: FieldDef = { ...field, type: field.items ?? "string" };
    return value.map((v) => cellText(item, v, entity)).join(SEPARATOR);
  }
  switch (field.type) {
    case "bool":
      return value === true ? "Yes" : "No";
    case "int":
    case "float":
      return numberText(value as number);
    case "quantity":
      return numberText(inCellUnit(field, value as number));
    case "ref":
      return entity?.refs[value as string]?.hrid ?? (value as string);
    case "person":
      return entity?.people?.[value as string]?.email ?? (value as string);
    default:
      return typeof value === "string" ? value : JSON.stringify(value);
  }
}

// --- Rows ------------------------------------------------------------------------------

/** A row of the sheet: a record as it was read, or a new one. */
export interface SheetRow {
  /** The record's ID, or a new row's own key. */
  key: string;
  /** The record, as it was when its first cell changed; none for a new row. */
  entity?: Entity;
  /** A new row's number, from 1: "New 1". */
  number?: number;
}

/** What the sheet holds besides its records: undone and redone as one. */
export interface SheetState {
  /** New rows' keys, top first. */
  added: string[];
  /** Each changed cell's text, by row and column. */
  edits: Record<string, Record<string, string>>;
  /** The next new row's number. */
  next: number;
}

export const EMPTY: SheetState = { added: [], edits: {}, next: 1 };

export const NEW_ROW = "new-";

export function isNew(key: string): boolean {
  return key.startsWith(NEW_ROW);
}

/** A cell's text as its record has it, before it's changed. */
export function baseText(row: SheetRow, column: SheetColumn): string {
  const entity = row.entity;
  if (column.key === ID) return entity?.hrid ?? `New ${String(row.number ?? "")}`;
  if (!entity || !column.field) return "";
  return cellText(column.field, entity.values[column.field.name], entity);
}

/** A cell's text: as it was typed, or as its record has it. */
export function textAt(state: SheetState, row: SheetRow, column: SheetColumn): string {
  return state.edits[row.key]?.[column.key] ?? baseText(row, column);
}

/** Whether a cell's text differs from its record's. */
export function changed(state: SheetState, row: SheetRow, column: SheetColumn): boolean {
  const typed = state.edits[row.key]?.[column.key];
  return typed !== undefined && typed !== baseText(row, column);
}

/** The labels of the state fields that lock a record. */
function lockedBy(entity: Entity, type: EntityType): string {
  const labels = entity.locked_by.map(
    (name) => type.fields.find((f) => f.name === name)?.label ?? name,
  );
  return labels.length ? labels.join(" and ") : "its state";
}

/**
 * Why a cell can't change, said as the form and the server say it; null if it can. The
 * server decides: this only keeps a cell from being typed in that couldn't be saved.
 */
export function whyLocked(
  row: SheetRow,
  column: SheetColumn,
  type: EntityType,
  project: string,
): string | null {
  const entity = row.entity;
  if (column.key === ID) return column.readOnly ?? null;
  if (!entity) return column.readOnly ?? null;
  if (column.newOnly) {
    return column.key === LOCATION
      ? "Move a record from its page or in Storage."
      : "Its amount changes through its ledger: draw, transfer or adjust it on its page.";
  }
  if (column.readOnly) return column.readOnly;
  if (entity.archived) return `${entity.hrid} is archived: restore it first.`;
  if (entity.locked) return `${entity.hrid} is locked by ${lockedBy(entity, type)}.`;
  if (!entity.can.edit) {
    const owner = entity.owner_project?.name ?? project;
    return `Only Editors in its owner project, ${owner}, can change it.`;
  }
  const field = column.field;
  if (!field) return null;
  if (!writableFields(type, entity).some((f) => f.name === field.name)) {
    return "It’s the other side’s of the request.";
  }
  const value = entity.values[field.name];
  const set = value !== null && value !== undefined && value !== "";
  if (set && entity.entry && field.registry) {
    return field.registry === "fact"
      ? "A registry fact: only its registry’s registrars change it."
      : "Its registry entry’s: it can’t change.";
  }
  if (set && field.immutable) return "Set once: it can’t change.";
  return null;
}

// --- Selection -------------------------------------------------------------------------

export interface Point {
  row: number;
  col: number;
}

export interface Range {
  top: number;
  left: number;
  bottom: number;
  right: number;
}

export function rangeOf(anchor: Point, focus: Point): Range {
  return {
    top: Math.min(anchor.row, focus.row),
    bottom: Math.max(anchor.row, focus.row),
    left: Math.min(anchor.col, focus.col),
    right: Math.max(anchor.col, focus.col),
  };
}

export function inRange(range: Range, row: number, col: number): boolean {
  return row >= range.top && row <= range.bottom && col >= range.left && col <= range.right;
}

/** Where a key moves the active cell, in a sheet of `rows` by `cols`; null for a key that
 * doesn't move it. As the ARIA grid pattern has it: arrows by one, Home and End to the
 * row's ends (with Ctrl, the sheet's), Page Up and Down by `page`. */
export function moved(
  at: Point,
  key: string,
  { ctrl, rows, cols, page }: { ctrl: boolean; rows: number; cols: number; page: number },
): Point | null {
  const clamp = (row: number, col: number): Point => ({
    row: Math.max(0, Math.min(rows - 1, row)),
    col: Math.max(0, Math.min(cols - 1, col)),
  });
  switch (key) {
    case "ArrowUp":
      return clamp(ctrl ? 0 : at.row - 1, at.col);
    case "ArrowDown":
      return clamp(ctrl ? rows - 1 : at.row + 1, at.col);
    case "ArrowLeft":
      return clamp(at.row, ctrl ? 0 : at.col - 1);
    case "ArrowRight":
      return clamp(at.row, ctrl ? cols - 1 : at.col + 1);
    case "Home":
      return clamp(ctrl ? 0 : at.row, 0);
    case "End":
      return clamp(ctrl ? rows - 1 : at.row, cols - 1);
    case "PageUp":
      return clamp(at.row - page, at.col);
    case "PageDown":
      return clamp(at.row + page, at.col);
    default:
      return null;
  }
}

// --- The clipboard ---------------------------------------------------------------------

/**
 * A block of cells from tab-separated text, as Excel, Sheets and Numbers copy it: a cell
 * with a tab, a line break or a quote in it is quoted, its quotes doubled. The last line
 * break, which spreadsheets add, isn't a row. Rows are made as long as the longest.
 */
export function parseTsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = "";
  let quoted = false;
  let started = true;
  const source = text.replace(/\r\n?/g, "\n");
  for (let i = 0; i < source.length; i++) {
    const c = source.charAt(i);
    if (quoted) {
      if (c === '"' && source[i + 1] === '"') {
        cell += '"';
        i++;
      } else if (c === '"') {
        quoted = false;
      } else {
        cell += c;
      }
      continue;
    }
    if (c === '"' && started) {
      quoted = true;
      started = false;
      continue;
    }
    started = false;
    if (c === "\t") {
      row.push(cell);
      cell = "";
      started = true;
    } else if (c === "\n") {
      row.push(cell);
      rows.push(row);
      row = [];
      cell = "";
      started = true;
    } else {
      cell += c;
    }
  }
  if (!started || cell || row.length) {
    row.push(cell);
    rows.push(row);
  }
  const width = Math.max(0, ...rows.map((r) => r.length));
  return rows.map((r) => [...r, ...Array<string>(width - r.length).fill("")]);
}

/** A block of cells as tab-separated text, as spreadsheets read it back. */
export function toTsv(block: readonly (readonly string[])[]): string {
  const cell = (text: string) =>
    /[\t\n\r"]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
  return block.map((row) => row.map(cell).join("\t")).join("\n");
}

/** A block of cells from an HTML table, as a web page or Word copies one; null if the
 * HTML has none. A cell spanning columns fills only its first. */
export function parseHtmlTable(html: string): string[][] | null {
  const doc = new DOMParser().parseFromString(html, "text/html");
  const table = doc.querySelector("table");
  if (!table) return null;
  const rows: string[][] = [];
  for (const tr of Array.from(table.querySelectorAll("tr"))) {
    const row: string[] = [];
    for (const cell of Array.from(tr.querySelectorAll("td, th"))) {
      for (const br of Array.from(cell.querySelectorAll("br"))) br.replaceWith("\n");
      row.push(cell.textContent.replace(/[ \t\r\f]+/g, " ").trim());
      const span = Number(cell.getAttribute("colspan") ?? "1");
      for (let i = 1; i < span && i < 100; i++) row.push("");
    }
    rows.push(row);
  }
  const width = Math.max(0, ...rows.map((r) => r.length));
  return rows.map((r) => [...r, ...Array<string>(width - r.length).fill("")]);
}

/** What was copied, as a block of cells: its tab-separated text, or its HTML table when
 * that has more columns than the text (as a web page's table, copied, has). */
export function clipboardBlock(text: string, html: string): string[][] {
  const plain = parseTsv(text);
  const table = html ? parseHtmlTable(html) : null;
  if (table?.length && (table[0]?.length ?? 0) > (plain[0]?.length ?? 0)) return table;
  return plain;
}

// --- Changes ---------------------------------------------------------------------------

/** A cell to write: by its row and column in the sheet. */
export interface Write {
  row: number;
  col: number;
  text: string;
}

export interface Placed {
  writes: Write[];
  /** Rows the block has past the sheet's last. */
  rowsPast: number;
  /** Columns the block has past the sheet's last. */
  colsPast: number;
}

/**
 * Where a pasted block goes: right and down from the selection's top left cell, in its
 * order. A single value fills the whole selection, and a block fills a selection that's
 * as many times its size, as spreadsheets do.
 */
export function placePaste(
  block: readonly (readonly string[])[],
  selection: Range,
  size: { rows: number; cols: number },
): Placed {
  const height = block.length;
  const width = block[0]?.length ?? 0;
  const writes: Write[] = [];
  if (!height || !width) return { writes, rowsPast: 0, colsPast: 0 };
  const tallRows = selection.bottom - selection.top + 1;
  const wideCols = selection.right - selection.left + 1;
  const tiles = tallRows % height === 0 && wideCols % width === 0;
  const rows = tiles ? Math.max(tallRows, height) : height;
  const cols = tiles ? Math.max(wideCols, width) : width;
  for (let r = 0; r < rows; r++) {
    const row = selection.top + r;
    if (row >= size.rows) continue;
    for (let c = 0; c < cols; c++) {
      const col = selection.left + c;
      if (col >= size.cols) continue;
      writes.push({ row, col, text: block[r % height]?.[c % width] ?? "" });
    }
  }
  return {
    writes,
    rowsPast: Math.max(0, selection.top + rows - size.rows),
    colsPast: Math.max(0, selection.left + cols - size.cols),
  };
}

/** What filling down writes: each column's top cell of the selection, into the cells
 * below it; with one row selected, the row above it, into it. */
export function fillDown(range: Range, text: (row: number, col: number) => string): Write[] {
  const from = range.top === range.bottom ? range.top - 1 : range.top;
  if (from < 0) return [];
  const writes: Write[] = [];
  for (let col = range.left; col <= range.right; col++) {
    const value = text(from, col);
    for (let row = from + 1; row <= range.bottom; row++) writes.push({ row, col, text: value });
  }
  return writes;
}

/** The sheet with `writes` made, each to a cell of `rows` by `columns`; a cell that
 * gets its record's text back is unchanged again. */
export function written(
  state: SheetState,
  writes: readonly Write[],
  rows: readonly SheetRow[],
  columns: readonly SheetColumn[],
): SheetState {
  const edits = new Map(
    Object.entries(state.edits).map(([k, v]) => [k, new Map(Object.entries(v))]),
  );
  for (const w of writes) {
    const row = rows[w.row];
    const column = columns[w.col];
    if (!row || !column) continue;
    const cells = edits.get(row.key) ?? new Map<string, string>();
    if (w.text === baseText(row, column) && !isNew(row.key)) cells.delete(column.key);
    else cells.set(column.key, w.text);
    if (cells.size) edits.set(row.key, cells);
    else edits.delete(row.key);
  }
  return {
    ...state,
    edits: Object.fromEntries([...edits].map(([k, cells]) => [k, Object.fromEntries(cells)])),
  };
}

/** The sheet with `count` new rows more, after the new rows it has at `at` (or at the
 * end of them). */
export function withNewRows(state: SheetState, count: number, at = state.added.length): SheetState {
  const made = Array.from({ length: count }, (_, i) => `${NEW_ROW}${String(state.next + i)}`);
  const added = [...state.added.slice(0, at), ...made, ...state.added.slice(at)];
  return { ...state, added, next: state.next + count };
}

/** The sheet without the new rows `keys`, and what was typed in them. */
export function withoutNewRows(state: SheetState, keys: ReadonlySet<string>): SheetState {
  const edits = Object.fromEntries(Object.entries(state.edits).filter(([k]) => !keys.has(k)));
  return { ...state, added: state.added.filter((k) => !keys.has(k)), edits };
}

/** Undo and redo: what the sheet was, and what it was before an undo. */
export interface History {
  past: SheetState[];
  present: SheetState;
  future: SheetState[];
}

/** The most steps back undo goes. */
const UNDO_STEPS = 200;

export function did(history: History, next: SheetState): History {
  // A step that changes nothing (a cell typed back as it was) isn't one to undo.
  if (next === history.present || JSON.stringify(next) === JSON.stringify(history.present)) {
    return history;
  }
  return {
    past: [...history.past, history.present].slice(-UNDO_STEPS),
    present: next,
    future: [],
  };
}

export function undo(history: History): History {
  const previous = history.past.at(-1);
  if (!previous) return history;
  return {
    past: history.past.slice(0, -1),
    present: previous,
    future: [history.present, ...history.future],
  };
}

export function redo(history: History): History {
  const next = history.future[0];
  if (!next) return history;
  return {
    past: [...history.past, history.present],
    present: next,
    future: history.future.slice(1),
  };
}

// --- Checking and saving ---------------------------------------------------------------

/**
 * What a row sends to be checked, or null if it changes nothing: a record's changed
 * cells, at the revision it was read at; a new row's cells, and where it's kept and how
 * much it starts with.
 */
export function checkRow(
  state: SheetState,
  row: SheetRow,
  columns: readonly SheetColumn[],
): CheckRow | null {
  const typed = state.edits[row.key] ?? {};
  const cells: Record<string, string | null> = {};
  let location: string | null = null;
  let amount: string | null = null;
  for (const column of columns) {
    const text = typed[column.key];
    if (text === undefined || column.key === ID) continue;
    if (row.entity && (column.newOnly || text === baseText(row, column))) continue;
    if (column.key === LOCATION) location = text.trim() || null;
    else if (column.key === AMOUNT) amount = text.trim() || null;
    else if (column.field && !column.readOnly) {
      if (row.entity || text.trim()) cells[column.field.name] = text;
    }
  }
  if (!Object.keys(cells).length && !location && !amount) return null;
  if (row.entity) {
    return { key: row.key, entity: row.entity.id, revision: row.entity.revision, cells };
  }
  return { key: row.key, cells, location, amount };
}

/** A check's answer belongs to what was sent, and how the sheet said its cells read. */
export function version(sent: CheckRow, locale: SheetLocale): string {
  return JSON.stringify([sent, locale]);
}

/** A row's problems at a cell: its field's, its place's, its amount's, or the row's own
 * (at its ID). */
export function problemsAt(checked: Checked | undefined, column: SheetColumn): CheckProblem[] {
  if (!checked) return [];
  const at =
    column.key === ID
      ? ""
      : column.key === LOCATION || column.key === AMOUNT
        ? column.key
        : `values/${column.key}`;
  return checked.problems.filter((p) => p.at === at);
}

/** What a checked row saves: what the check read, the revision it read the record at, and
 * a new record's exact place, so what's saved is what was reviewed (ADR 0207). Null for a
 * row that changes nothing. */
export function saveRow(sent: CheckRow, checked: Checked): SaveRow | null {
  if (checked.kind === "unchanged" || checked.kind === "blank") return null;
  return {
    key: sent.key,
    entity: sent.entity ?? null,
    revision: sent.revision ?? null,
    values: checked.values,
    location: checked.location
      ? { container: checked.location.container, position: checked.location.position }
      : null,
    amount: checked.amount ?? null,
  };
}

export async function checkRows(
  project: string,
  type: string,
  rows: CheckRow[],
  locale: SheetLocale,
  signal: AbortSignal | null = null,
): Promise<Checked[]> {
  const out: Checked[] = [];
  for (let start = 0; start < rows.length; start += CHECK_ROWS) {
    const found = unwrap(
      await iki.POST("/api/v1/projects/{project_id}/sheet-edits/check", {
        params: { path: { project_id: project } },
        body: { type, locale, rows: rows.slice(start, start + CHECK_ROWS) },
        signal,
      }),
    );
    out.push(...found.rows);
  }
  return out;
}

export async function saveRows(project: string, body: Schemas["SheetEditSaveIn"]): Promise<Saved> {
  return unwrap(
    await iki.POST("/api/v1/projects/{project_id}/sheet-edits", {
      params: { path: { project_id: project } },
      body,
    }),
  );
}

/** A type's import template, as the API writes it. */
export function templateUrl(type: string): string {
  return `/api/v1/entity-types/${type}/import-template.csv`;
}
