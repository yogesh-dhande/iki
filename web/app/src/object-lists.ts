/**
 * A list of objects, such as a plasmid's annotated features, as people read it: never its
 * JSON. A record's page shows it as a small table of the keys its objects share (name,
 * type, where it lies, strand); a grid's cell, which has one line, as how many there are
 * and their names. One reading for both, so a value reads the same wherever it's shown.
 */

/** One of the table's columns: a key of the objects, or start and end read as one. */
export interface ObjectColumn {
  key: string;
  label: string;
  /** Numbers, aligned right. */
  numeric: boolean;
  text: (row: Record<string, unknown>) => string;
}

export interface ObjectList {
  columns: ObjectColumn[];
  rows: Record<string, unknown>[];
  /** Each row's name, where its objects have one: what a one-line summary lists. */
  names: string[];
}

/** Keys read first, in this order: what a thing is called, what kind it is, where it lies. */
const FIRST = ["name", "label", "title", "id", "type", "kind", "start", "end", "strand"];
/** The keys a row is named by, the first it has. */
const NAMED_BY = ["name", "label", "title", "id"];
/** The most columns a table has: a value's cell is narrower than a page. */
const MOST_COLUMNS = 6;
/** The names a one-line summary lists; the count says how many there are. */
const NAMES_LISTED = 5;

const numbers = new Intl.NumberFormat("en-GB", { maximumFractionDigits: 6 });

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isScalar(value: unknown): boolean {
  return value === null || ["string", "number", "boolean"].includes(typeof value);
}

/** "rep_origin" → "Rep origin"; "copyNumber" → "Copy number". */
export function keyLabel(key: string): string {
  const words = key
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/[_-]+/g, " ")
    .trim()
    .toLowerCase();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function scalarText(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "number") return numbers.format(value);
  if (typeof value === "boolean") return value ? "Yes" : "No";
  return typeof value === "string" ? value : JSON.stringify(value);
}

/** A list of objects as a table of the keys they hold plain values in: null for anything
 * else, an empty list included. Keys holding objects or lists, such as a feature's
 * qualifiers, are left out: the audit trail and the record's export have them whole. */
export function objectList(value: unknown): ObjectList | null {
  if (!Array.isArray(value) || value.length === 0 || !value.every(isObject)) return null;
  const rows = value;
  const seen: string[] = [];
  for (const row of rows) {
    for (const key of Object.keys(row)) if (!seen.includes(key)) seen.push(key);
  }
  const plain = seen.filter((key) => rows.every((row) => isScalar(row[key] ?? null)));
  const ordered = [
    ...FIRST.filter((key) => plain.includes(key)),
    ...plain.filter((key) => !FIRST.includes(key)),
  ];
  const span =
    ordered.includes("start") &&
    ordered.includes("end") &&
    rows.every((r) => typeof r["start"] === "number" && typeof r["end"] === "number");
  const columns: ObjectColumn[] = [];
  for (const key of ordered) {
    if (span && key === "end") continue;
    if (span && key === "start") {
      columns.push({
        key: "start-end",
        label: "Position",
        numeric: true,
        text: (row) => `${scalarText(row["start"])}–${scalarText(row["end"])}`,
      });
      continue;
    }
    columns.push({
      key,
      label: keyLabel(key),
      numeric: rows.every((row) => row[key] === null || typeof row[key] === "number"),
      text: (row) => scalarText(row[key]),
    });
  }
  const namedBy = NAMED_BY.find((key) => rows.some((row) => typeof row[key] === "string"));
  const names = namedBy
    ? rows.map((row) => row[namedBy]).filter((n): n is string => typeof n === "string" && !!n)
    : [];
  return { columns: columns.slice(0, MOST_COLUMNS), rows, names };
}

/** A list of objects on one line, as a grid's cell shows it: how many, then their names. */
export function objectListText(list: ObjectList): string {
  const count = list.rows.length.toLocaleString("en-GB");
  if (!list.names.length) return `${count} ${list.rows.length === 1 ? "item" : "items"}`;
  const listed = list.names.slice(0, NAMES_LISTED).join(", ");
  return `${count} · ${listed}${list.names.length > NAMES_LISTED ? ", …" : ""}`;
}
