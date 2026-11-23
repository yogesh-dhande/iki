/**
 * Where material is kept (ADR 0180): freezers, racks, boxes and what's in each position,
 * where a record is, its location history, receipts and reconciliations.
 */

import { unwrap, type Problem, type Schemas } from "@ikisci/sdk";
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
export type Outcome = "matched" | "missing" | "extra" | "damaged" | "rejected";
export type ObservationEvent = Schemas["ObservationEventOut"];
export type User = Schemas["UserOut"];

export type Receipt = Schemas["ReceiptOut"];
export type ReceiptLine = Schemas["ReceiptLineOut"];
export type ReceiptScan = Schemas["ReceiptScanOut"];
export type ReceiptCheck = Schemas["CheckedReceiptOut"];

export type ReconciliationLine = Schemas["ReconciliationLineOut"];
export type Reconciliation = Schemas["ReconciliationOut"];
/** A scan in a count: which event, on which physical line, saw what where, by whom, when. */
export type CountObservation = Schemas["CountScanEvidenceOut"];
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
    queryFn: async () => readReceipt(id),
  });
}

export async function readReceipt(id: string): Promise<Receipt> {
  return unwrap(
    await iki.GET("/api/v1/receipts/{receipt_id}", { params: { path: { receipt_id: id } } }),
  );
}

// --- Observation history (ADR 0180) -------------------------------------------------------------

/** Whose history: a receipt's, or a count's. */
export type Observed = "receipts" | "reconciliations";

async function historyPage(
  parent: Observed,
  id: string,
  query: { line_id?: string; cursor?: string; limit?: number },
) {
  return parent === "receipts"
    ? unwrap(
        await iki.GET("/api/v1/receipts/{receipt_id}/history", {
          params: { path: { receipt_id: id }, query },
        }),
      )
    : unwrap(
        await iki.GET("/api/v1/reconciliations/{reconciliation_id}/history", {
          params: { path: { reconciliation_id: id }, query },
        }),
      );
}

/** Where a parent's history queries are, to read again after a change. */
export function historyKey(parent: Observed, id: string) {
  return [...STORAGE, "history", parent, id] as const;
}

/** Who recorded what, and each correction: newest first, a page at a time; or one line's. */
export function historyQuery(parent: Observed, id: string, line?: string) {
  return infiniteQueryOptions({
    queryKey: [...historyKey(parent, id), line ?? "all"],
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam }) =>
      historyPage(parent, id, {
        limit: 20,
        ...(line ? { line_id: line } : {}),
        ...(pageParam ? { cursor: pageParam } : {}),
      }),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
}

/** What changed after the plan revision someone saw: the newest page's later events. */
export async function changesSince(
  parent: Observed,
  id: string,
  revision: number,
): Promise<ObservationEvent[]> {
  const latest = await historyPage(parent, id, { limit: 50 });
  return latest.items.filter((e) => e.revision > revision);
}

// --- Recording and correcting observations -------------------------------------------------------

/** A correction's reason: 1–2,000 characters once trimmed. */
export const REASON_MAX = 2000;

/** What a correction carries: the line version the person reviewed, and why. */
export interface Correction {
  line_revision: number;
  reason: string;
}

/**
 * A scan: no version, so two people scanning different tubes don't wait on each other.
 * `code` is what the scanner sent, as it sent it: the history keeps it exactly. A choice
 * names the tube the receiver picked for it, and the server checks the pair again.
 */
export async function scanReceipt(
  receipt: string,
  code: string,
  how: { mode?: ScanMode; choice?: string } = {},
): Promise<ReceiptScan> {
  return unwrap(
    await iki.POST("/api/v1/receipts/{receipt_id}/scan", {
      params: { path: { receipt_id: receipt } },
      body: { code, ...how },
    }),
  );
}

/**
 * A line's outcome and condition note. The first is sent as it is; a change to what was
 * recorded carries the version seen and why. A first one may answer that someone else had
 * already checked it: theirs stands.
 */
export async function checkReceiptLine(
  receipt: string,
  line: string,
  check: Schemas["ReceiptLineIn"],
): Promise<ReceiptCheck> {
  return unwrap(
    await iki.PATCH("/api/v1/receipts/{receipt_id}/lines/{line_id}", {
      params: { path: { receipt_id: receipt, line_id: line } },
      body: check,
    }),
  );
}

export async function removeReceiptLine(
  receipt: string,
  line: string,
  correction: Correction,
): Promise<Receipt> {
  return unwrap(
    await iki.DELETE("/api/v1/receipts/{receipt_id}/lines/{line_id}", {
      params: { path: { receipt_id: receipt, line_id: line } },
      body: correction,
    }),
  );
}

export async function restoreReceiptLine(
  receipt: string,
  line: string,
  correction: Correction,
): Promise<Receipt> {
  return unwrap(
    await iki.POST("/api/v1/receipts/{receipt_id}/lines/{line_id}/restore", {
      params: { path: { receipt_id: receipt, line_id: line } },
      body: correction,
    }),
  );
}

export async function describeReceipt(
  receipt: string,
  handoff: Schemas["ReceiptIn"],
): Promise<Receipt> {
  return unwrap(
    await iki.PATCH("/api/v1/receipts/{receipt_id}", {
      params: { path: { receipt_id: receipt } },
      body: handoff,
    }),
  );
}

export async function cancelReceipt(
  receipt: string,
  cancel: Schemas["CancelReceiptIn"],
): Promise<Receipt> {
  return unwrap(
    await iki.POST("/api/v1/receipts/{receipt_id}/cancel", {
      params: { path: { receipt_id: receipt } },
      body: cancel,
    }),
  );
}

export async function completeReceipt(
  receipt: string,
  approval: Schemas["CompleteReceiptIn"],
): Promise<Receipt> {
  return unwrap(
    await iki.POST("/api/v1/receipts/{receipt_id}/complete", {
      params: { path: { receipt_id: receipt } },
      body: approval,
    }),
  );
}

export type Reviewed = Schemas["ReviewedCountLine"];

/** The lines a refused count or receipt change names: the ones to review, at their versions now. */
export function affectedLines(problem: Problem | undefined): Reviewed[] | null {
  return problem?.affected_lines ?? null;
}

/**
 * A count's scans. A fresh observation is sent as it is; one that replaces what's recorded
 * names each line it supersedes, at the version seen, and, if it changes it, why.
 */
export async function scanCount(count: string, scans: Schemas["ScansIn"]): Promise<Reconciliation> {
  return unwrap(
    await iki.POST("/api/v1/reconciliations/{reconciliation_id}/scans", {
      params: { path: { reconciliation_id: count } },
      body: scans,
    }),
  );
}

export async function resolveCountLine(
  count: string,
  line: string,
  decision: Schemas["ResolveIn"],
): Promise<Reconciliation> {
  return unwrap(
    await iki.PATCH("/api/v1/reconciliations/{reconciliation_id}/lines/{line_id}", {
      params: { path: { reconciliation_id: count, line_id: line } },
      body: decision,
    }),
  );
}

export async function cancelCount(
  count: string,
  cancel: Schemas["CancelReconciliationIn"],
): Promise<Reconciliation> {
  return unwrap(
    await iki.POST("/api/v1/reconciliations/{reconciliation_id}/cancel", {
      params: { path: { reconciliation_id: count } },
      body: cancel,
    }),
  );
}

/** The records a scanned code names, among those its reader can read. */
export async function scanFor(code: string): Promise<ScanMatch[]> {
  return unwrap(await iki.GET("/api/v1/storage/scan", { params: { query: { code } } })).items;
}

// --- Which tube a scan identifies ---------------------------------------------------------------

export type ScanMode = Schemas["ScanMode"];
export type ScanCandidate = Schemas["ScanCandidateOut"];
export type ScanResolved = Schemas["ScanResolveOut"];
/** What confirms a record is the one in hand: a scanned label, or its printed Iki ID typed. */
export type Proof = string | Schemas["IkiIdConfirmationIn"];

/**
 * What Iki says when a scan can't be used here: a tube the person can't read is involved,
 * whether or not it matched. It says nothing about how many matched, or which.
 */
export const SCAN_REFUSED =
  "This scan can’t identify a tube here. Use its printed Iki ID or ask an authorized reviewer.";

/**
 * What Iki says when a typed Iki ID can't be used: none matched, or one the person can't
 * read did. The same words either way, so it never says whether an Iki ID exists.
 */
export const IKI_ID_REFUSED =
  "No tube you can open has this Iki ID. Check the ID, or ask an authorized reviewer.";

/** The unique code an Iki label carries: “IKI:” and the record's UUID, in 32 hex digits. */
export function ikiCode(id: string): string {
  return `IKI:${id.replaceAll("-", "").toUpperCase()}`;
}

/** An Iki ID typed from a tube's label: matched only against Iki IDs, not vendor barcodes. */
export function ikiId(hrid: string): Schemas["IkiIdConfirmationIn"] {
  return { code: hrid.trim(), mode: "iki_id" };
}

/** What a scan identifies where it's used: one record, several to choose from, or none. */
export async function resolveScan(scan: Schemas["ScanResolveIn"]): Promise<ScanResolved> {
  return unwrap(await iki.POST("/api/v1/storage/scan/resolve", { body: scan }));
}

/** Why a scan wasn't used: it can't identify a tube here, or it matches several readable. */
export type ScanRefusal =
  { kind: "refused" } | { kind: "ambiguous"; candidates: ScanCandidate[]; index: number | null };

/**
 * A refusal about the scan itself, apart from a conflict with what someone else recorded:
 * those name `affected_lines`, and mean the person reviews what's recorded now.
 */
export function scanRefusal(problem: Problem | undefined): ScanRefusal | null {
  if (!problem) return null;
  if (problem.code === "scan_unavailable") return { kind: "refused" };
  if (problem.code === "scan_ambiguous") {
    const candidates = problem.candidates ?? [];
    // Without its candidates, a choice can't be offered: it's said as a refusal.
    return candidates.length
      ? { kind: "ambiguous", candidates, index: problem.index ?? null }
      : { kind: "refused" };
  }
  return null;
}

/** Whether a 409 is about the scan, not about a newer version of what's recorded. */
export function isScanConflict(problem: Problem | undefined): boolean {
  return problem?.code === "scan_unavailable" || problem?.code === "scan_ambiguous";
}

/** Where a candidate is: “BOX-000014 Kinase retains, A2”, or that it isn't in a box. */
export function candidatePlace(c: ScanCandidate): string {
  if (!c.box) return c.position ? `At ${c.position}` : "Not in a box you can open";
  const box =
    c.box.title && c.box.title !== c.box.hrid ? `${c.box.hrid} ${c.box.title}` : c.box.hrid;
  return c.position ? `${box}, ${c.position}` : box;
}

/** How a count's scan identified its tube. */
export const IDENTITY_BASES: Record<Schemas["IdentityBasis"], string> = {
  record_link: "by its Iki label or record link",
  iki_id: "by its Iki ID, typed",
  position: "by its label at its recorded position",
  choice: "by the tube chosen for its label",
  label: "by its label",
};

/** How a receipt's scan identified its tube, as its history keeps it. */
export const RECEIPT_BASES: Record<string, string> = {
  record_code: "identified by its Iki label",
  record_link: "identified by an Iki record link",
  iki_id: "identified by its Iki ID, typed",
  vendor: "identified by its vendor barcode",
  hrid: "identified by its Iki ID",
  hrid_and_vendor: "identified by its Iki ID and vendor barcode",
  unmatched: "matched no record",
  ambiguous: "left unlinked: more than one readable record has this label",
};

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
