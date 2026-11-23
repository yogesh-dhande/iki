/**
 * Where material is kept (ADR 0180): freezers, racks, boxes and what's in each position,
 * where a record is, its location history, receipts and reconciliations.
 */

import { unwrap, type Schemas } from "@ikisci/sdk";
import { infiniteQueryOptions, queryOptions } from "@tanstack/react-query";

import { iki } from "./api";
import type { EntityType } from "./entities";

export type Shape = Schemas["ShapeOut"];
export type Container = Schemas["ContainerOut"];
export type Occupant = Schemas["OccupantOut"];
export type Location = Schemas["LocationOut"];
export type Step = Schemas["StepOut"];
export type LocationEvent = Schemas["EventOut"];
export type Place = Schemas["PlaceOut"];
export type ScanMatch = Schemas["ScanMatchOut"];
export type Receipt = Schemas["ReceiptOut"];
export type ReceiptLine = Schemas["ReceiptLineOut"];
export type Outcome = "matched" | "missing" | "extra" | "damaged" | "rejected";
export type Reconciliation = Schemas["ReconciliationOut"];
export type ReconciliationLine = Schemas["ReconciliationLineOut"];
export type Finding = ReconciliationLine["finding"];
export type Resolution = "moved" | "removed" | "placed" | "kept";
export type LabelSize = Schemas["LabelSizeOut"];
export type PreparedLabel = Schemas["LabelOut"];
export type TypeStorage = Schemas["TypeStorageOut"];

export const STORAGE = ["storage"] as const;

export const placesQuery = queryOptions({
  queryKey: [...STORAGE, "places"],
  queryFn: async () => unwrap(await iki.GET("/api/v1/storage/places")),
});

export function containerQuery(ref: string) {
  return queryOptions({
    queryKey: [...STORAGE, "container", ref.toUpperCase()],
    queryFn: async () =>
      unwrap(await iki.GET("/api/v1/storage/containers/{ref}", { params: { path: { ref } } })),
  });
}

export function locationQuery(ref: string) {
  return queryOptions({
    queryKey: [...STORAGE, "location", ref.toUpperCase()],
    queryFn: async () =>
      unwrap(await iki.GET("/api/v1/entities/{ref}/location", { params: { path: { ref } } })),
  });
}

export function locationEventsQuery(ref: string) {
  return infiniteQueryOptions({
    queryKey: [...STORAGE, "events", ref.toUpperCase()],
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam }) =>
      unwrap(
        await iki.GET("/api/v1/entities/{ref}/location/events", {
          params: {
            path: { ref },
            query: { limit: 20, ...(pageParam ? { cursor: pageParam } : {}) },
          },
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
}

export type CheckedOut = Schemas["CheckedOutOut"];

/** What's checked out: to everyone, or to one person. */
export function checkedOutQuery(custodian?: string) {
  return queryOptions({
    queryKey: [...STORAGE, "checked-out", custodian ?? "all"],
    queryFn: async () =>
      unwrap(
        await iki.GET("/api/v1/storage/checked-out", {
          params: { query: custodian ? { custodian } : {} },
        }),
      ),
  });
}

export function reconciliationsQuery(ref: string) {
  return queryOptions({
    queryKey: [...STORAGE, "reconciliations", ref.toUpperCase()],
    queryFn: async () =>
      unwrap(
        await iki.GET("/api/v1/storage/containers/{ref}/reconciliations", {
          params: { path: { ref } },
        }),
      ),
  });
}

export const labelSizesQuery = queryOptions({
  queryKey: [...STORAGE, "label-sizes"],
  queryFn: async () => unwrap(await iki.GET("/api/v1/storage/labels")),
  staleTime: Infinity,
});

export function receiptsQuery(ref: string) {
  return queryOptions({
    queryKey: [...STORAGE, "receipts", ref.toUpperCase()],
    queryFn: async () =>
      unwrap(await iki.GET("/api/v1/entities/{ref}/receipts", { params: { path: { ref } } })),
  });
}

export function reconciliationQuery(id: string) {
  return queryOptions({
    queryKey: [...STORAGE, "reconciliation", id],
    queryFn: async () =>
      unwrap(
        await iki.GET("/api/v1/reconciliations/{reconciliation_id}", {
          params: { path: { reconciliation_id: id } },
        }),
      ),
  });
}

export function receiptQuery(id: string) {
  return queryOptions({
    queryKey: [...STORAGE, "receipt", id],
    queryFn: async () =>
      unwrap(
        await iki.GET("/api/v1/receipts/{receipt_id}", { params: { path: { receipt_id: id } } }),
      ),
  });
}

/** The records a scanned code names, among those its reader can read. */
export async function scanFor(code: string): Promise<ScanMatch[]> {
  return unwrap(await iki.GET("/api/v1/storage/scan", { params: { query: { code } } })).items;
}

// --- Positions ----------------------------------------------------------------------------------

/** A row's letters, as a spreadsheet's columns are: 1 is A, 27 is AA. */
export function letters(n: number): string {
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

export interface Slot {
  row: number;
  column: number;
  label: string;
  occupant: Occupant | undefined;
}

/** Every position, row by row as it's drawn, each with what's in it. */
export function slotsOf(shape: Shape, contents: readonly Occupant[]): Slot[][] {
  const at = new Map<string, Occupant>();
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
export function freeSlots(shape: Shape, contents: readonly Occupant[]): Slot[] {
  return slotsOf(shape, contents)
    .flat()
    .filter((s) => !s.occupant)
    .sort((a, b) => indexOf(shape, a.row, a.column) - indexOf(shape, b.row, b.column));
}

/** Where its type says its records are kept, or hold others: none, for any other. */
export function storageOf(type: EntityType | undefined): TypeStorage | null {
  return type?.storage ?? null;
}

export function isContainer(type: EntityType | undefined): boolean {
  return !!type?.storage?.holds.length;
}

/** A step's name: its title, or its HRID. */
export function stepName(step: Pick<Step, "hrid" | "title">): string {
  return step.title && step.title !== step.hrid ? step.title : step.hrid;
}

/** A path as one line: Freezer 3 › Rack C, 3 › Kinase retains, A3. */
export function pathText(location: Location): string {
  if (!location.path.length) {
    return location.stored ? "In a place you can’t open" : "Not in storage";
  }
  const steps = location.path.map((s) =>
    s.position ? `${stepName(s)}, ${s.position}` : stepName(s),
  );
  return steps.join(" › ") + (location.hidden ? " › a place you can’t open" : "");
}

// --- Outcomes and findings ----------------------------------------------------------------------

export const OUTCOMES: { value: Outcome; label: string; help: string }[] = [
  { value: "matched", label: "Matched", help: "Arrived as expected." },
  { value: "missing", label: "Missing", help: "Expected, but not in the shipment." },
  { value: "damaged", label: "Damaged", help: "Arrived, but damaged. Received; say how." },
  { value: "rejected", label: "Rejected", help: "Arrived, and not accepted. Say why." },
  { value: "extra", label: "Extra", help: "Arrived, but wasn’t expected." },
];

export const FINDINGS: Record<Finding, string> = {
  matched: "Matched",
  missing: "Missing",
  unexpected: "Unexpected",
  wrong_slot: "Wrong position",
  unchecked: "Not scanned yet",
  empty: "Empty",
  checked_out: "Checked out",
};

export const RESOLUTIONS: Record<Resolution, string> = {
  moved: "Move it to where it was found",
  removed: "Take it out of storage",
  placed: "Record it where it was found",
  kept: "Leave the record as it is",
};

/** Whether a finding needs someone to say what to do about it. */
export function needsResolving(line: ReconciliationLine): boolean {
  return resolutionsFor(line).length > 0;
}

/** What each finding may be resolved as, as the server allows. */
export function resolutionsFor(line: ReconciliationLine): Resolution[] {
  switch (line.finding) {
    case "wrong_slot":
      return ["moved", "kept"];
    case "missing":
      return ["removed", "kept"];
    case "unexpected":
      return line.entity ? ["placed", "kept"] : ["kept"];
    case "unchecked":
    case "matched":
    case "empty":
    case "checked_out":
    default:
      return [];
  }
}

export const EVENT_KINDS: Record<LocationEvent["kind"], string> = {
  placed: "Put away",
  moved: "Moved",
  removed: "Taken out of storage",
  checked_out: "Checked out",
  checked_in: "Checked in",
  handed_over: "Handed over",
  received: "Received",
};
