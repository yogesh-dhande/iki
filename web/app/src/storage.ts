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
  queryFn: async ({ signal }) => unwrap(await iki.GET("/api/v1/storage/places", { signal })),
});

export function containerQuery(ref: string) {
  return queryOptions({
    queryKey: [...STORAGE, "container", ref.toUpperCase()],
    queryFn: async ({ signal }) =>
      unwrap(
        await iki.GET("/api/v1/storage/containers/{ref}", { params: { path: { ref } }, signal }),
      ),
  });
}

export function locationQuery(ref: string) {
  return queryOptions({
    queryKey: [...STORAGE, "location", ref.toUpperCase()],
    queryFn: async ({ signal }) =>
      unwrap(
        await iki.GET("/api/v1/entities/{ref}/location", { params: { path: { ref } }, signal }),
      ),
  });
}

export function locationEventsQuery(ref: string) {
  return infiniteQueryOptions({
    queryKey: [...STORAGE, "events", ref.toUpperCase()],
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam, signal }) =>
      unwrap(
        await iki.GET("/api/v1/entities/{ref}/location/events", {
          params: {
            path: { ref },
            query: { limit: 20, ...(pageParam ? { cursor: pageParam } : {}) },
          },
          signal,
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
    queryFn: async ({ signal }) =>
      unwrap(
        await iki.GET("/api/v1/storage/checked-out", {
          params: { query: custodian ? { custodian } : {} },
          signal,
        }),
      ),
  });
}

export function reconciliationsQuery(ref: string) {
  return queryOptions({
    queryKey: [...STORAGE, "reconciliations", ref.toUpperCase()],
    queryFn: async ({ signal }) =>
      unwrap(
        await iki.GET("/api/v1/storage/containers/{ref}/reconciliations", {
          params: { path: { ref } },
          signal,
        }),
      ),
  });
}

export const labelSizesQuery = queryOptions({
  queryKey: [...STORAGE, "label-sizes"],
  queryFn: async ({ signal }) => unwrap(await iki.GET("/api/v1/storage/labels", { signal })),
  staleTime: Infinity,
});

export function receiptsQuery(ref: string) {
  return queryOptions({
    queryKey: [...STORAGE, "receipts", ref.toUpperCase()],
    queryFn: async ({ signal }) =>
      unwrap(
        await iki.GET("/api/v1/entities/{ref}/receipts", { params: { path: { ref } }, signal }),
      ),
  });
}

export function reconciliationQuery(id: string) {
  return queryOptions({
    queryKey: [...STORAGE, "reconciliation", id],
    queryFn: async ({ signal }) =>
      unwrap(
        await iki.GET("/api/v1/reconciliations/{reconciliation_id}", {
          params: { path: { reconciliation_id: id } },
          signal,
        }),
      ),
  });
}

export function receiptQuery(id: string) {
  return queryOptions({
    queryKey: [...STORAGE, "receipt", id],
    queryFn: async ({ signal }) =>
      unwrap(
        await iki.GET("/api/v1/receipts/{receipt_id}", {
          params: { path: { receipt_id: id } },
          signal,
        }),
      ),
  });
}

/** The records a scanned code names, among those its reader can read. */
export async function scanFor(code: string): Promise<ScanMatch[]> {
  return unwrap(await iki.GET("/api/v1/storage/scan", { params: { query: { code } } })).items;
}

// --- Positions ----------------------------------------------------------------------------------

export {
  indexOf,
  slotsOf,
  freeSlots,
  letters,
  positionLabel,
  rowLabel,
} from "@ikisci/science/box-map";

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
  no_longer_found: "No longer found",
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
