/** Dimensions and labeling of a rectangular storage box or rack. Positions start at 1. */
export interface Shape {
  rows: number;
  columns: number;
  labels: "letter_number" | "number";
  order: "by_row" | "by_column";
}

export interface Position {
  row: number | null;
  column: number | null;
}

/** Omit the identifier to describe an occupied position with restricted contents. */
export interface BoxOccupant extends Position {
  identifier?: string;
  name?: string;
  checkedOutBy?: string;
  /** How many items a nested container holds, including zero. */
  count?: number;
}

/** A row's letters, as a spreadsheet's columns are: 1 is A, 27 is AA. */
export function letters(n: number): string {
  if (!Number.isSafeInteger(n) || n < 1) return "";
  let out = "";
  let rest = n;
  while (rest > 0) {
    const m = (rest - 1) % 26;
    out = String.fromCharCode(65 + m) + out;
    rest = Math.floor((rest - 1) / 26);
  }
  return out;
}

/** Its place in the grid's order, from 0. */
export function indexOf(shape: Shape, row: number, column: number): number {
  return shape.order === "by_column"
    ? (column - 1) * shape.rows + (row - 1)
    : (row - 1) * shape.columns + (column - 1);
}

/** A position's name: A1 to H12, or 1 to 81 in the grid's order. */
export function positionLabel(shape: Shape, row: number, column: number): string {
  return shape.labels === "number"
    ? String(indexOf(shape, row, column) + 1)
    : `${letters(row)}${String(column)}`;
}

/** A row's header: its letter, or the number its first position has. */
export function rowLabel(shape: Shape, row: number): string {
  return shape.labels === "number" ? "" : letters(row);
}

export interface Slot<T = BoxOccupant> {
  row: number;
  column: number;
  label: string;
  occupant: T | undefined;
}

/** Every position, row by row as it's drawn, each with what's in it. */
export function slotsOf<T extends Position>(shape: Shape, contents: readonly T[]): Slot<T>[][] {
  const at = new Map<string, T>();
  for (const o of contents) {
    if (o.row != null && o.column != null) at.set(`${String(o.row)}:${String(o.column)}`, o);
  }
  return Array.from({ length: shape.rows }, (_, r) =>
    Array.from({ length: shape.columns }, (_, c) => ({
      row: r + 1,
      column: c + 1,
      label: positionLabel(shape, r + 1, c + 1),
      occupant: at.get(`${String(r + 1)}:${String(c + 1)}`),
    })),
  );
}

/** The free positions, in the grid's order: where the next things put away go. */
export function freeSlots<T extends Position>(shape: Shape, contents: readonly T[]): Slot<T>[] {
  return slotsOf(shape, contents)
    .flat()
    .filter((s) => !s.occupant)
    .sort((a, b) => indexOf(shape, a.row, a.column) - indexOf(shape, b.row, b.column));
}
