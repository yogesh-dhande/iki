/**
 * Receiving a request's material (ADR 0180): what it expects as a checklist, each scanned
 * or marked matched, missing, damaged or rejected, and anything unexpected as extra; who
 * handed it over; where each received tube goes, a free position in a box or the receiver's
 * custody, chosen for each; then one change that takes custody, puts each away as shown,
 * and tells the requester what didn't arrive as expected. Complete waits until every
 * received tube has its decision, made with the box's free positions as they're shown.
 *
 * A first observation is one step: a scan, or an outcome chosen. Changing what someone
 * recorded (another outcome, Clear, a saved note, removing or restoring an extra, the
 * handover, cancelling) asks why, names the version seen, and keeps both in the history.
 */

import { ApiError, unwrap, type Problem, type Schemas } from "@ikisci/sdk";
import { buttonState } from "@ikisci/ui/button";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";

import { fieldErrors, iki, problemOf, useApiMutation } from "../api";
import { BoxMap, MapLegend, slotKey } from "../box-map";
import { entityQuery, personName, typeLabel, typesQuery, type EntityType } from "../entities";
import { usePageTitle } from "../layout";
import {
  HistoryDisclosure,
  LineHistory,
  ReasonDialog,
  StaleNote,
  type StaleItem,
} from "../observations";
import { useSteadyBlock } from "../steady";
import {
  affectedLines,
  isScanConflict,
  cancelReceipt,
  checkReceiptLine,
  completeReceipt,
  placeableQuery,
  previewReceiptPlacement,
  containerQuery,
  describeReceipt,
  freeSlots,
  historyKey,
  ikiCode,
  OUTCOMES,
  readReceipt,
  receiptsQuery,
  removeReceiptLine,
  resolveScan,
  restoreReceiptLine,
  scanReceipt,
  scanRefusal,
  STORAGE,
  stepName,
  type Container,
  type Outcome,
  type ScanCandidate,
  type ScanMode,
  type Receipt,
  type ReceiptLine,
} from "../storage";
import { IkiIdEntry, ScanBar, ScanChooser, ScanRefusedNote } from "../storage-ui";
import { UtcTime } from "./revision-text";
import {
  Button,
  CheckboxField,
  Loading,
  PageHeader,
  ProblemAlert,
  SelectField,
  TextField,
  Time,
  useMessages,
} from "../ui";

/** What's received, and so put away: as the server has it. */
const RECEIVED: readonly string[] = ["matched", "damaged"];
/** What needs a note saying what was wrong. */
const NOTED: readonly string[] = ["damaged", "rejected", "extra"];

function lineName(line: ReceiptLine): string {
  if (line.ref)
    return line.ref.title !== line.ref.hrid ? `${line.ref.hrid} ${line.ref.title}` : line.ref.hrid;
  return line.code;
}

/** A line's short name: its HRID, else its code. */
function shortName(line: ReceiptLine): string {
  return line.ref?.hrid ?? line.code;
}

function outcomeLabel(outcome: Outcome | null | undefined): string {
  return OUTCOMES.find((o) => o.value === outcome)?.label ?? "Not checked";
}

/** Each line's name, active or removed, for a history. */
function namesOf(receipt: Receipt): Map<string, string> {
  return new Map([...receipt.lines, ...receipt.removed_lines].map((l) => [l.id, shortName(l)]));
}

export function ReceivePage() {
  const { ref } = useParams({ from: "/storage/receive/$ref" });
  const request = useQuery(entityQuery(ref));
  const receipts = useQuery(receiptsQuery(ref));
  const hrid = request.data?.hrid ?? ref;
  usePageTitle(`Receive ${hrid}`);
  const data = receipts.data;
  const open = data?.receipts.find((r) => r.status === "open");
  const done = data?.receipts.filter((r) => r.status === "completed") ?? [];
  const cancelled = data?.receipts.filter((r) => r.status === "cancelled") ?? [];
  return (
    <>
      <PageHeader
        crumbs={[
          <Link to="/requests/">Requests</Link>,
          <Link to="/entities/$ref/" params={{ ref: hrid }}>
            {hrid}
          </Link>,
          "Receive",
        ]}
        title={`Receive ${hrid}`}
        description={
          request.data && request.data.title !== request.data.hrid ? request.data.title : undefined
        }
      />
      {receipts.error ? <ProblemAlert problem={problemOf(receipts.error)} /> : null}
      {!data ? <Loading label="Loading what it expects…" /> : null}
      {data && open ? <OpenReceipt key={open.id} receipt={open} request={ref} /> : null}
      {data && !open ? (
        <StartReceipt
          request={ref}
          expected={data.expected}
          canReceive={data.can_receive}
          whyNot={data.why_not}
        />
      ) : null}
      {done.length ? <PastReceipts receipts={done} /> : null}
      {cancelled.length ? <CancelledReceipts receipts={cancelled} /> : null}
    </>
  );
}

function StartReceipt({
  request,
  expected,
  canReceive,
  whyNot,
}: {
  request: string;
  expected: number;
  canReceive: boolean;
  whyNot: string | null | undefined;
}) {
  const [by, setBy] = useState("");
  const start = useApiMutation(async () => {
    const started = unwrap(
      await iki.POST("/api/v1/entities/{ref}/receipts", { params: { path: { ref: request } } }),
    );
    if (!by.trim()) return started;
    // Nothing was recorded yet, so there's nothing to say why it changed.
    return describeReceipt(started.id, {
      plan_revision: started.plan_revision,
      handed_over_by: by.trim(),
    });
  }, [STORAGE]);
  const problem = start.error ? problemOf(start.error) : undefined;
  return (
    <section className="card" aria-labelledby="start-title">
      <h2 className="section-title" id="start-title">
        {expected
          ? `${String(expected)} ${expected === 1 ? "item" : "items"} expected`
          : "Nothing left to receive"}
      </h2>
      {whyNot ? (
        <div className="alert alert-warning" role="note">
          <p>{whyNot}</p>
        </div>
      ) : null}
      {canReceive && expected ? (
        <>
          <ProblemAlert problem={problem} />
          <TextField
            label="Handed over by"
            value={by}
            onChange={setBy}
            optional
            maxLength={200}
            error={fieldErrors(problem)["handed_over_by"]}
            help="Who brought it: a courier and its waybill, or a colleague. Needed before it’s completed."
          />
          <div className="actions">
            <Button
              variant="primary"
              busy={start.isPending}
              busyText="Starting…"
              onClick={() => {
                start.mutate(undefined);
              }}
            >
              Start receiving
            </Button>
          </div>
        </>
      ) : null}
    </section>
  );
}

// --- Changing what was recorded ------------------------------------------------------------------

/** A change to what's recorded: each but a first observation asks why. */
export type Fix =
  /** Another outcome, Clear (null), or a note: left out, the saved note stays. */
  | { do: "outcome"; line: string; outcome: Outcome | null; note?: string }
  | { do: "remove"; line: string }
  | { do: "restore"; line: string }
  | { do: "handoff"; by: string; note: string }
  | { do: "cancel" };

/** Whether a change replaces something recorded, and so needs a reason. */
export function needsReason(fix: Fix, receipt: Receipt): boolean {
  switch (fix.do) {
    case "outcome": {
      // As the server has it: changing a checked outcome, or a saved note, is a correction;
      // a first note isn't.
      const line = receipt.lines.find((l) => l.id === fix.line);
      if (!line) return false;
      const note = fix.note?.trim() ?? line.note;
      return (
        (!!line.outcome && line.outcome !== fix.outcome) || (!!line.note && note !== line.note)
      );
    }
    case "handoff":
      return (
        (!!receipt.handed_over_by && fix.by !== receipt.handed_over_by) ||
        (!!receipt.note && fix.note !== receipt.note)
      );
    case "remove":
    case "restore":
    case "cancel":
      return true;
  }
}

/** The line a change is about, wherever it is now: on the checklist, or removed. */
function lineOf(receipt: Receipt, fix: Fix): ReceiptLine | undefined {
  if (!("line" in fix)) return undefined;
  return [...receipt.lines, ...receipt.removed_lines].find((l) => l.id === fix.line);
}

/** A change on its way, as Complete's wait names it: “TUB-000001’s outcome”. */
function writing(fix: Fix, receipt: Receipt): string {
  const line = lineOf(receipt, fix);
  const name = line ? shortName(line) : "the line";
  switch (fix.do) {
    case "outcome":
      return fix.outcome === line?.outcome ? `the note on ${name}` : `${name}’s outcome`;
    case "remove":
      return `the removal of ${name}`;
    case "restore":
      return `restoring ${name}`;
    case "handoff":
      return "the handover";
    case "cancel":
      return "the cancellation";
  }
}

/** Sends a change, with the version of what it replaces as it's shown now. */
async function send(receipt: Receipt, fix: Fix, reason: string | undefined): Promise<Receipt> {
  const id = receipt.id;
  const why = reason ?? "";
  const given = reason ? { reason } : {};
  const line = lineOf(receipt, fix);
  const revision = line?.line_revision ?? 0;
  switch (fix.do) {
    case "outcome": {
      const checked = await checkReceiptLine(id, fix.line, {
        outcome: fix.outcome,
        // Left out, the saved note stays: sent only when it was edited, null when cleared.
        ...edited(fix.note, line?.note ?? ""),
        line_revision: revision,
        ...given,
      });
      return checked.receipt;
    }
    case "remove":
      return removeReceiptLine(id, fix.line, { line_revision: revision, reason: why });
    case "restore":
      return restoreReceiptLine(id, fix.line, { line_revision: revision, reason: why });
    case "handoff":
      return describeReceipt(id, {
        plan_revision: receipt.plan_revision,
        ...(fix.by !== receipt.handed_over_by ? { handed_over_by: fix.by } : {}),
        ...edited(fix.note, receipt.note),
        ...given,
      });
    case "cancel":
      return cancelReceipt(id, { plan_revision: receipt.plan_revision, reason: why });
  }
}

/** A note to send: only when it was edited, and null when it was cleared. */
export function edited(text: string | undefined, saved: string): { note?: string | null } {
  if (text === undefined) return {};
  const note = text.trim();
  return note === saved ? {} : { note: note || null };
}

/** A change being asked about: what was shown when it was asked, and what changed since. */
interface Asking {
  fix: Fix;
  /** After a refusal: what others changed. */
  changes: StaleItem[] | null;
}

/** A newer version of what's recorded: not a scan that can't identify a tube, also a 409. */
function isStale(error: unknown): boolean {
  return error instanceof ApiError && error.status === 409 && !isScanConflict(error.problem);
}

/**
 * After a refusal: each line the server names as changed, as it's recorded now, and the
 * handover if that changed. Each line's history says who changed what, and why.
 */
export function changedSince(
  shown: Receipt,
  fresh: Receipt,
  affected: readonly { line_id: string }[] | null,
): StaleItem[] {
  const lines = [...fresh.lines, ...fresh.removed_lines];
  const items: StaleItem[] = [];
  for (const { line_id } of affected ?? []) {
    const line = lines.find((l) => l.id === line_id);
    if (line)
      items.push({
        key: line.id,
        about: shortName(line),
        body: fresh.removed_lines.includes(line) ? "Now removed from the checklist" : lineNow(line),
      });
  }
  if (shown.handed_over_by !== fresh.handed_over_by || shown.note !== fresh.note) {
    const said = [
      fresh.handed_over_by ? `handed over by ${fresh.handed_over_by}` : "",
      fresh.note ? `note “${fresh.note}”` : "",
    ].filter(Boolean);
    items.push({
      key: "handover",
      about: "The handover",
      body: said.length ? `Now ${said.join(" · ")}` : "Now cleared",
    });
  }
  return items;
}

/** What's recorded on a line now, and who checked it. */
function lineNow(line: ReceiptLine): ReactNode {
  if (!line.outcome) return line.note ? `Not checked; note “${line.note}”` : "Now not checked";
  return (
    <>
      Now {outcomeLabel(line.outcome).toLowerCase()}
      {line.note ? ` · note “${line.note}”` : ""}, checked by {personName(line.checked_by)}
      {line.checked_at ? (
        <>
          , <UtcTime value={line.checked_at} />
        </>
      ) : null}
    </>
  );
}

// --- Where each received tube goes --------------------------------------------------------------

/** A received tube's decision, as the screen shows it. */
type Disposition =
  | { kind: "put"; position: string }
  | { kind: "custody" }
  /** Not decided yet: completing waits for it. */
  | { kind: "open" };

/** A line's choice to keep it in custody, beside the positions it can go in. */
const KEEP = "keep";

/**
 * Each received line's decision. Kept in custody, when the receiver chose that for it.
 * Otherwise, only in a box whose free positions are shown (`free`): the position chosen for
 * it, or else the next free one no other line has. Without a box shown, or when what was
 * chosen isn't free, it's open.
 */
function dispositions(
  lines: readonly ReceiptLine[],
  free: readonly string[] | null,
  where: Readonly<Record<string, string>>,
  kept: ReadonlySet<string>,
  /** Lines that can't go in the box shown: archived, or a kind it doesn't hold. */
  cannot: ReadonlySet<string> = new Set(),
): Map<string, Disposition> {
  const chosen = new Set(
    lines
      .filter((l) => !kept.has(l.id) && !cannot.has(l.id))
      .map((l) => where[l.id])
      .filter((w): w is string => !!w && !!free?.includes(w)),
  );
  const left = (free ?? []).filter((w) => !chosen.has(w));
  const out = new Map<string, Disposition>();
  for (const l of lines) {
    const choice = where[l.id];
    if (kept.has(l.id)) out.set(l.id, { kind: "custody" });
    else if (!free || cannot.has(l.id)) out.set(l.id, { kind: "open" });
    else if (choice !== undefined)
      out.set(
        l.id,
        choice && free.includes(choice) ? { kind: "put", position: choice } : { kind: "open" },
      );
    else {
      const next = left.shift();
      out.set(l.id, next ? { kind: "put", position: next } : { kind: "open" });
    }
  }
  return out;
}

/** What Complete sends: the revision reviewed, and one decision for each received tube. */
interface CompletePlan {
  automatic: Set<string>;
  plan_revision: number;
  missing_rest: boolean;
  put_away: { line: string; to: string; position: string }[];
  keep_in_custody: string[];
}

/** A refused completion's error, with the line it's about, and the put-away row it's on. */
interface Refusal {
  key: string;
  about: string;
  detail: string;
  /** The put-away row to show it on: a placement or a custody choice that was sent. */
  row: string | null;
}

/** Field names that aren't a line, as people know them. */
const FIELDS: Record<string, string> = { handed_over_by: "Handed over by", note: "Note" };

/**
 * Each error in a refused completion: `lines/<id>` names its line; `put_away/<i>` and
 * `keep_in_custody/<i>` name the entry sent, and so its line, by where it was in the list.
 */
function refusalsOf(
  problem: Problem | undefined,
  sent: CompletePlan | undefined,
  names: ReadonlyMap<string, string>,
): Refusal[] {
  return (problem?.errors ?? []).map((error, i) => {
    const field = error.pointer?.replace(/^#\//, "") ?? error.parameter ?? "";
    const [list, index] = field.split("/");
    const at = Number(index);
    const row =
      list === "put_away"
        ? (sent?.put_away[at]?.line ?? null)
        : list === "keep_in_custody"
          ? (sent?.keep_in_custody[at] ?? null)
          : null;
    const line = list === "lines" ? (index ?? null) : row;
    return {
      key: `${field}-${String(i)}`,
      about: line ? (names.get(line) ?? "A line") : (FIELDS[field] ?? ""),
      detail: error.detail,
      row,
    };
  });
}

/** The box shown, for its plan's caption: “BOX-000009 Incoming, in Freezer 3 · 4 free”. */
function boxCaption(box: Container, free: number): string {
  const name = box.title && box.title !== box.hrid ? `${box.hrid} ${box.title}` : box.hrid;
  const path = box.location.path.map(stepName).join(" › ");
  return `${name}${path ? `, in ${path}` : ""} · ${String(free)} free`;
}

/** A type and each it extends, as the server checks what a box holds. */
function lineage(types: readonly EntityType[], name: string): Set<string> {
  const out = new Set<string>();
  for (let at: string | null | undefined = name; at && !out.has(at);) {
    out.add(at);
    at = types.find((t) => t.name === at)?.extends;
  }
  return out;
}

/** The lines whose version changed between two readings of a receipt, or that came or went. */
function revisedLines(shown: Receipt, now: Receipt): { line_id: string }[] {
  const seen = (r: Receipt) =>
    new Map(
      [...r.lines, ...r.removed_lines].map((l) => [
        l.id,
        `${String(l.line_revision)}${r.removed_lines.includes(l) ? " removed" : ""}`,
      ]),
    );
  const before = seen(shown);
  return [...seen(now)]
    .filter(([line, version]) => before.get(line) !== version)
    .map(([line_id]) => ({ line_id }));
}

/** Names in a sentence: “A”, “A and B”, “A, B and C”. */
function listed(names: readonly string[]): string {
  if (names.length < 2) return names.join("");
  return `${names.slice(0, -1).join(", ")} and ${names.at(-1) ?? ""}`;
}

/** What a plan does, in a phrase: “2 put away in BOX-000135 and 1 kept in your custody”. */
function planSummary(put: number, kept: number, box: string | undefined): string {
  return [
    put ? `${String(put)} put away in ${box ?? "the box"}` : "",
    kept ? `${String(kept)} kept in your custody` : "",
  ]
    .filter(Boolean)
    .join(" and ");
}

function OpenReceipt({ receipt: initial, request }: { receipt: Receipt; request: string }) {
  const client = useQueryClient();
  const [receipt, setReceipt] = useState(initial);
  // Someone else's change, read in the background: shown when it's newer than ours.
  const [read, setRead] = useState(initial);
  if (initial !== read) {
    setRead(initial);
    if (initial.plan_revision > receipt.plan_revision) setReceipt(initial);
  }
  // The newest receipt, for handlers: what's shown, or a response not yet rendered.
  const latest = useRef(receipt);
  useEffect(() => {
    if (receipt.plan_revision >= latest.current.plan_revision) latest.current = receipt;
  }, [receipt]);
  const [heard, setHeard] = useState("");
  const [restorable, setRestorable] = useState<string | null>(null);
  const [lastLine, setLastLine] = useState<string | null>(null);
  const [by, setBy] = useState(initial.handed_over_by);
  const [note, setNote] = useState(initial.note);
  const [box, setBox] = useState("");
  const [boxShown, setBoxShown] = useState("");
  // What was typed or scanned for the box shown: Complete waits while the field says otherwise.
  const [lookedUp, setLookedUp] = useState("");
  // A position chosen for a line, or "" to leave it undecided; left out, its next free one.
  const [where, setWhere] = useState<Record<string, string>>({});
  // The lines the receiver chose, one by one, to keep in their custody without a location.
  const [kept, setKept] = useState<ReadonlySet<string>>(() => new Set());
  const [missingRest, setMissingRest] = useState(false);
  const [asking, setAsking] = useState<Asking | null>(null);
  const [handoffStale, setHandoffStale] = useState<StaleItem[] | null>(null);
  // Said beside the handover's Save, not above the page: nothing below it moves.
  const [handoffSaid, setHandoffSaid] = useState("");
  const [completeStale, setCompleteStale] = useState<{
    what: string;
    changes: StaleItem[];
  } | null>(null);
  const [lineStale, setLineStale] = useState<{ line: string; changes: StaleItem[] } | null>(null);
  const { say } = useMessages();
  const id = receipt.id;
  const names = namesOf(receipt);
  /** The newest receipt wins: two people's scans can answer in either order. */
  const update = (next: Receipt) => {
    if (next.plan_revision >= latest.current.plan_revision) {
      latest.current = next;
      setReceipt(next);
    }
    void client.invalidateQueries({ queryKey: [...STORAGE, "receipts"] });
    void client.invalidateQueries({ queryKey: historyKey("receipts", id) });
  };
  // Writes still on their way: Complete waits for each, so what it approves includes them.
  const [writes, setWrites] = useState<readonly { key: number; what: string }[]>([]);
  const nextWrite = useRef(0);
  /** A write starting: what it is, as Complete's wait says it; call what's returned once settled. */
  const begin = (what: string) => {
    const key = ++nextWrite.current;
    setWrites((now) => [...now, { key, what }]);
    return () => {
      setWrites((now) => now.filter((w) => w.key !== key));
    };
  };
  /** After a refusal: the receipt as it is now, and what the server says changed. */
  const refetch = async (error: unknown) => {
    const shown = latest.current;
    const fresh = await readReceipt(id);
    update(fresh);
    return { fresh, changes: changedSince(shown, fresh, affectedLines(problemOf(error))) };
  };
  // What the scanner sent, kept exactly while a choice is made: the history records it.
  const scan = useApiMutation(
    async ({ code, mode, choice }: { code: string; mode?: ScanMode; choice?: string }) =>
      scanReceipt(id, code, { ...(mode ? { mode } : {}), ...(choice ? { choice } : {}) }),
    [],
  );
  // A scan that needs more: a choice among readable tubes, or another way to identify it.
  const [choosing, setChoosing] = useState<{ code: string; candidates: ScanCandidate[] } | null>(
    null,
  );
  // How the scan the server refused was given: a label, or a typed Iki ID.
  const [refused, setRefused] = useState<ScanMode | null>(null);
  const scanned = (sent: { code: string; mode?: ScanMode; choice?: string }) => {
    setChoosing(null);
    setRefused(null);
    const { code } = sent;
    scan.mutate(sent, {
      onSettled: begin(`the scan of ${code.trim()}`),
      onSuccess: (done) => {
        update(done.receipt);
        setLastLine(done.line);
        setRestorable(done.found === "removed" ? done.line : null);
        const line = [...done.receipt.lines, ...done.receipt.removed_lines].find(
          (l) => l.id === done.line,
        );
        const name = line ? lineName(line) : code.trim();
        switch (done.found) {
          case "matched":
            setHeard(`${name}: matched.`);
            break;
          case "again":
            setHeard(
              line?.checked_by
                ? `${name} is already checked: ${outcomeLabel(line.outcome).toLowerCase()}, by ${personName(line.checked_by)}. Nothing changed.`
                : `${name} is already checked. Nothing changed.`,
            );
            break;
          case "removed":
            setHeard(
              `${name} was removed from this checklist earlier. Nothing changed: restore it if it’s part of this delivery.`,
            );
            break;
          case "extra":
            setHeard(`${name} wasn’t expected: added as extra. Say what it is in its note.`);
            break;
          case "ambiguous_extra":
            setHeard(
              `${code.trim()} wasn’t expected: added as extra, not linked to a record, because more than one tube you can read has this label. Say what it is in its note.`,
            );
        }
      },
      onError: (error) => {
        setRestorable(null);
        const refusal = scanRefusal(problemOf(error));
        if (refusal?.kind === "ambiguous") {
          setHeard("");
          setChoosing({ code, candidates: refusal.candidates });
        } else if (refusal) {
          setHeard("");
          setRefused(sent.mode ?? "label");
        } else setHeard(problemOf(error).detail);
      },
    });
  };
  const check = useApiMutation(
    async ({ line, outcome, note }: { line: string; outcome: Outcome; note: string }) =>
      checkReceiptLine(id, line, {
        outcome,
        ...edited(note, latest.current.lines.find((l) => l.id === line)?.note ?? ""),
      }),
    [],
  );
  const fixing = useApiMutation(
    async ({ fix, reason }: { fix: Fix; reason: string | undefined }) =>
      send(latest.current, fix, reason),
    [],
  );
  /** A change: straight away if it replaces nothing; else asked about first. */
  const change = (fix: Fix) => {
    fixing.reset();
    if (needsReason(fix, latest.current)) {
      setAsking({ fix, changes: null });
      return;
    }
    fixing.mutate(
      { fix, reason: undefined },
      {
        onSettled: begin(writing(fix, latest.current)),
        onSuccess: (next) => {
          update(next);
          setHandoffStale(null);
          setLineStale(null);
          if (fix.do === "handoff") setHandoffSaid("Saved.");
        },
        onError: (error) => {
          if (!isStale(error)) return;
          const line = "line" in fix ? fix.line : undefined;
          void refetch(error).then(({ fresh, changes }) => {
            // Someone recorded it first: now it's a change to theirs, and needs a reason.
            if (needsReason(fix, fresh)) {
              fixing.reset();
              setAsking({ fix, changes });
            } else if (fix.do === "handoff") setHandoffStale(changes);
            else if (line) setLineStale({ line, changes });
          });
        },
      },
    );
  };
  const confirm = (reason: string) => {
    if (!asking) return;
    const { fix } = asking;
    fixing.mutate(
      { fix, reason },
      {
        onSettled: begin(writing(fix, latest.current)),
        onSuccess: (next) => {
          update(next);
          setAsking(null);
          if (fix.do === "restore") setRestorable(null);
          if (fix.do === "cancel") {
            say(`Cancelled receiving ${request}.`);
            void client.invalidateQueries({ queryKey: STORAGE });
          } else if (fix.do === "handoff") setHandoffSaid("Saved, with your reason.");
          // Where scans are heard, in space kept for it: the checklist doesn't move.
          else {
            const line = lineOf(next, fix);
            setHeard(`${line ? shortName(line) : "It"}: saved, with your reason.`);
          }
        },
        onError: (error) => {
          if (!isStale(error)) return;
          // Never sent again by itself: the person reviews what's recorded now.
          void refetch(error).then(({ changes }) => {
            setAsking({ fix, changes });
          });
        },
      },
    );
  };
  // The box typed or scanned, by its barcode, ID or Iki label; shown by its ID once found.
  const [boxFound, setBoxFound] = useState<
    | { kind: "ambiguous"; code: string; candidates: ScanCandidate[] }
    | { kind: "refused" }
    | { kind: "none"; code: string }
    | null
  >(null);
  // Only the latest lookup is shown: an earlier one answering late changes nothing.
  const lookup = useRef(0);
  const findBox = useApiMutation(async ({ code, chosen }: { code: string; chosen?: string }) => {
    const asked = ++lookup.current;
    setBoxFound(null);
    setBoxShown("");
    setWhere({});
    let id = chosen;
    if (!id) {
      try {
        const found = await resolveScan({ scope: "destination", code });
        if (asked !== lookup.current) return;
        id = found.status === "one" ? found.candidates[0]?.id : undefined;
        if (found.status === "ambiguous")
          setBoxFound({ kind: "ambiguous", code, candidates: found.candidates });
        else if (!id) setBoxFound({ kind: "none", code });
      } catch (error) {
        if (asked === lookup.current && scanRefusal(problemOf(error)))
          setBoxFound({ kind: "refused" });
        throw error;
      }
    }
    if (!id) return;
    // Its free positions as they are now, not as they were when it was last looked at.
    await client.query({ ...containerQuery(id), staleTime: 0 });
    if (asked !== lookup.current) return;
    setLookedUp(code);
    setBoxShown(id);
  }, []);
  // The box's slots read again, after the server refused a position in it.
  const reread = useApiMutation(async (ref: string) => {
    await client.query({ ...containerQuery(ref), staleTime: 0 });
  }, []);
  const container = useQuery({ ...containerQuery(boxShown || "-"), enabled: !!boxShown });
  const putting = receipt.lines.filter(
    (l) => l.outcome && RECEIVED.includes(l.outcome) && l.entity,
  );
  const shape = boxShown ? (container.data?.shape ?? null) : null;
  const free = useMemo(
    () => (shape && container.data ? freeSlots(shape, container.data.contents) : []),
    [shape, container.data],
  );
  // Where the received tubes can go: a box whose free positions are shown, or not yet known.
  const shownBox = boxShown && shape ? container.data : undefined;
  const typesRead = useQuery(typesQuery);
  const types = typesRead.data;
  // Whether each tube can go in this box at all, as completing will check: its project's
  // data, say, isn't readable by the box's project.
  const placeable = useQuery({
    ...placeableQuery(receipt.id, receipt.plan_revision, shownBox?.id ?? "-"),
    enabled: receipt.status === "open" && !!shownBox && putting.length > 0,
  });
  const waitingFor = findBox.isPending
    ? "Finding the box and its free positions…"
    : boxFound?.kind === "ambiguous"
      ? `Choose which box “${boxFound.code.trim()}” is, above.`
      : (boxShown && container.isPending) ||
          reread.isPending ||
          (shownBox && typesRead.isPending) ||
          (shownBox && putting.length > 0 && placeable.isPending)
        ? "Loading the box’s free positions…"
        : null;
  const refusedHere = new Map(
    (placeable.data ?? []).flatMap((p) => (p.problem ? [[p.line, p.problem] as const] : [])),
  );
  // Only wells that can be used are offered: the server checks again when it's completed.
  const boxWhyNot = !shownBox
    ? null
    : shownBox.archived
      ? `${shownBox.hrid} is archived: choose another box, or keep them in your custody.`
      : !shownBox.can.keep
        ? `Putting things in ${shownBox.hrid} needs Editor in its project: choose another box, or keep them in your custody.`
        : null;
  const whyNot = new Map<string, string>();
  if (shownBox && types && !boxWhyNot) {
    for (const l of putting) {
      if (!l.ref) continue;
      if (l.ref.archived)
        whyNot.set(
          l.id,
          `${l.ref.hrid} is archived: restore it to put it away, or keep it in your custody.`,
        );
      else if (!shownBox.holds.some((h) => lineage(types, l.ref?.type ?? "").has(h)))
        whyNot.set(
          l.id,
          `${shownBox.hrid} doesn’t hold a ${typeLabel(types, l.ref.type)}: choose another box, or keep it in your custody.`,
        );
      else {
        const refused = refusedHere.get(l.id);
        if (refused) whyNot.set(l.id, `${refused} Choose another box, or keep it in your custody.`);
      }
    }
  }
  const cannot = new Set(boxWhyNot ? putting.map((l) => l.id) : whyNot.keys());
  const decided = dispositions(
    putting,
    shownBox && !waitingFor ? free.map((s) => s.label) : null,
    where,
    kept,
    cannot,
  );
  const undecided = putting.filter((l) => decided.get(l.id)?.kind === "open");
  const planned = shownBox
    ? putting.flatMap((l) => {
        const d = decided.get(l.id);
        return d?.kind === "put" ? [{ line: l, position: d.position }] : [];
      })
    : [];
  const keeping = putting.filter((l) => decided.get(l.id)?.kind === "custody");
  const complete = useApiMutation(
    async (
      plan: CompletePlan,
    ): Promise<{ done: Receipt } | { changed: Receipt } | { placement: Schemas["MovesOut"] }> => {
      // The receipt as it is now: what's approved is what was reviewed, every write included.
      const now = await readReceipt(id);
      if (now.plan_revision !== plan.plan_revision) return { changed: now };
      const put_away = plan.put_away.map((p) =>
        plan.automatic.has(p.line) ? { ...p, position: null } : p,
      );
      let approved: string | undefined;
      if (plan.automatic.size) {
        const preview = await previewReceiptPlacement(id, {
          plan_revision: plan.plan_revision,
          put_away,
        });
        if (
          !preview.plan ||
          preview.items.length !== plan.put_away.length ||
          preview.items.some(
            (item, i) => item.problems.length || item.to?.position !== plan.put_away[i]?.position,
          )
        ) {
          return { placement: preview };
        }
        approved = preview.plan;
      }
      return {
        done: await completeReceipt(id, {
          plan_revision: plan.plan_revision,
          missing_rest: plan.missing_rest,
          put_away,
          ...(approved ? { plan: approved } : {}),
          keep_in_custody: plan.keep_in_custody,
        }),
      };
    },
    [STORAGE],
  );
  // A refusal of what was sent: each error on the line it's about, and listed by Complete.
  const problem =
    complete.error && !isStale(complete.error) && !scanRefusal(problemOf(complete.error))
      ? problemOf(complete.error)
      : undefined;
  const errors = fieldErrors(problem);
  const refusals = refusalsOf(problem, complete.variables, names);
  const rowErrors = new Map<string, string>();
  for (const r of refusals) if (r.row && !rowErrors.has(r.row)) rowErrors.set(r.row, r.detail);
  const lineProblem =
    check.error ?? (fixing.error && !asking && !isStale(fixing.error) ? fixing.error : null);
  const unchecked = receipt.lines.filter((l) => !l.outcome).length;
  const discrepancies = receipt.lines.filter(
    (l) => l.outcome && ["missing", "extra", "damaged", "rejected"].includes(l.outcome),
  ).length;
  const marks = new Map<string, string>();
  for (const { position } of planned) {
    const slot = free.find((s) => s.label === position);
    if (slot) marks.set(slotKey(slot.row, slot.column), "suggested");
  }
  // The Box field names another box than the one shown: what would be put away isn't known.
  const typed = box.trim();
  const boxChanged = !!shownBox && !!planned.length && typed !== lookedUp.trim();
  // Completing waits for every write on its way, and a decision on every received tube, made
  // with the box as shown.
  const ready = !writes.length && !undecided.length && !boxChanged;
  const status = writes.length
    ? writes.length === 1
      ? `Waiting for ${writes[0]?.what ?? ""} to be saved.`
      : `Waiting for ${String(writes.length)} changes to be saved.`
    : !putting.length
      ? ""
      : waitingFor && !ready
        ? waitingFor
        : boxChanged
          ? `Show the free positions in ${typed || "a box"} first, or put the text back to ${lookedUp.trim()}.`
          : !ready
            ? `Decide for ${listed(undecided.map(shortName))}: put ${undecided.length === 1 ? "it" : "each"} away in a free position, or keep ${undecided.length === 1 ? "it" : "them"} in your custody.`
            : `Ready: ${planSummary(planned.length, keeping.length, shownBox?.hrid)}.`;
  const statusId = useId();
  // After a refusal, the keyboard and screen reader go to what it says.
  const feedback = useRef<HTMLDivElement>(null);
  const [refusedAt, setRefusedAt] = useState(0);
  useEffect(() => {
    if (refusedAt) feedback.current?.focus();
  }, [refusedAt]);
  // What Complete would send, while it can: a press completes only the plan it began on.
  const planKey = ready
    ? JSON.stringify([
        receipt.plan_revision,
        missingRest,
        shownBox?.id ?? null,
        planned.map((p) => [p.line.id, p.position]),
        keeping.map((l) => l.id),
      ])
    : null;
  const pressed = useRef<{ plan: string | null } | null>(null);
  const completeButton = buttonState(
    {
      variant: "primary",
      busy: complete.isPending,
      busyText: "Completing…",
      disabled: !ready,
      describedBy: status ? statusId : undefined,
    },
    "Complete receiving",
  );
  const completeNow = () => {
    if (!ready || complete.isPending) return;
    // What's shown is what's sent: this receipt's revision, and each tube's decision.
    const plan: CompletePlan = {
      automatic: new Set(
        planned.filter(({ line }) => where[line.id] === undefined).map(({ line }) => line.id),
      ),
      plan_revision: receipt.plan_revision,
      missing_rest: missingRest,
      put_away: shownBox
        ? planned.map(({ line, position }) => ({
            line: line.id,
            // The box shown, by its unique Iki code: what's put away is what was seen.
            to: ikiCode(shownBox.id),
            position,
          }))
        : [],
      keep_in_custody: keeping.map((l) => l.id),
    };
    const into = shownBox?.hrid;
    const shown = receipt;
    const showPlacement = (moves: Schemas["MovesOut"]) => {
      const changes: StaleItem[] = moves.items.flatMap((item, i) => {
        const before = plan.put_away[i];
        if (!before || (!item.problems.length && before.position === item.to?.position)) return [];
        return [
          {
            key: before.line,
            about: names.get(before.line) ?? item.record.hrid,
            body: item.problems.length
              ? item.problems.join(" ")
              : `Previously ${before.position}, now ${item.to?.position ?? "unavailable"}`,
          },
        ];
      });
      setCompleteStale({ what: "The put-away plan", changes });
      if (boxShown) reread.mutate(boxShown);
      setRefusedAt((n) => n + 1);
    };
    complete.mutate(plan, {
      onSuccess: (result) => {
        if ("placement" in result) {
          showPlacement(result.placement);
          return;
        }
        if ("changed" in result) {
          // Newer than what was reviewed: nothing is sent; the person reviews it, then completes.
          setMissingRest(false);
          update(result.changed);
          setCompleteStale({
            what: "The checklist",
            changes: changedSince(shown, result.changed, revisedLines(shown, result.changed)),
          });
          setRefusedAt((n) => n + 1);
          return;
        }
        setCompleteStale(null);
        say(
          `Received ${receipt.request.hrid}${putting.length ? `: ${planSummary(plan.put_away.length, plan.keep_in_custody.length, into)}` : ""}.`,
        );
        void client.invalidateQueries({ queryKey: STORAGE });
      },
      onError: (error) => {
        if (
          error instanceof ApiError &&
          error.problem.code === "moves_plan_changed" &&
          error.problem.moves
        ) {
          showPlacement(error.problem.moves);
          return;
        }
        const refusal = scanRefusal(problemOf(error));
        if (refusal) {
          // The box the resolver can't settle: chosen again, then reviewed again.
          setBoxShown("");
          setWhere({});
          setBoxFound(
            refusal.kind === "ambiguous"
              ? { kind: "ambiguous", code: box, candidates: refusal.candidates }
              : { kind: "refused" },
          );
          return;
        }
        if (!isStale(error)) {
          // A position refused, such as one taken meanwhile: each tube keeps the position it
          // was shown, those refused are left to decide again, and the slots are read again.
          const refused = new Set(
            refusalsOf(problemOf(error), plan, names).flatMap((r) =>
              r.row && plan.put_away.some((p) => p.line === r.row) ? [r.row] : [],
            ),
          );
          if (refused.size && boxShown) {
            setWhere((now) => ({
              ...now,
              ...Object.fromEntries(
                plan.put_away.map((p) => [p.line, refused.has(p.line) ? "" : p.position]),
              ),
            }));
            reread.mutate(boxShown);
          }
          setRefusedAt((n) => n + 1);
          return;
        }
        // What's to be missing may have changed: it's ticked again once reviewed.
        setMissingRest(false);
        if (boxShown)
          void client.invalidateQueries({ queryKey: containerQuery(boxShown).queryKey });
        void refetch(error).then(({ changes }) => {
          setCompleteStale({ what: "The checklist", changes });
          setRefusedAt((n) => n + 1);
        });
      },
    });
  };
  const restoring = restorable ? receipt.removed_lines.find((l) => l.id === restorable) : undefined;
  const handoffChanged = by.trim() !== receipt.handed_over_by || note.trim() !== receipt.note;
  // The checklist, drawn as it was while a press is on any of the receipt's cards: another
  // line's save or scan answered, or a colleague's change read, is drawn once it's let go,
  // so nothing moves under the press. The cards below it hold still with it.
  const [checklist, press] = useSteadyBlock(
    <>
      <div className="section-head">
        <h2 className="section-title" id="receipt-title">
          What’s expected
        </h2>
        <p className="muted">
          Started by {personName(receipt.started_by)} <Time value={receipt.started_at} />.{" "}
          {unchecked ? `${String(unchecked)} still to check.` : "All checked."}
        </p>
      </div>
      <ScanBar
        label="Scan each tube"
        help="Each scanned is matched; one that isn’t expected is added as extra."
        // Never busy: the next tube can be scanned before the last one's answer is back.
        disabled={!!asking}
        onScan={(code) => {
          scanned({ code });
        }}
      />
      {refused ? <ScanRefusedNote mode={refused} /> : null}
      {choosing ? (
        <ScanChooser
          code={choosing.code}
          candidates={choosing.candidates}
          busy={scan.isPending}
          onChoose={(c) => {
            scanned({ code: choosing.code, choice: c.id });
          }}
          onCancel={() => {
            setChoosing(null);
            setHeard("Nothing was recorded for that scan.");
          }}
        />
      ) : null}
      <IkiIdEntry
        open={refused !== null}
        disabled={!!asking}
        onEnter={(hrid) => {
          scanned({ code: hrid, mode: "iki_id" });
        }}
      />
      <p className="scan-heard" role="status" aria-live="polite">
        {heard}
      </p>
      {restoring ? (
        <div className="actions scan-follow">
          <Button
            onClick={() => {
              change({ do: "restore", line: restoring.id });
            }}
          >
            {`Restore ${shortName(restoring)}`}
          </Button>
        </div>
      ) : null}
      <ProblemAlert problem={lineProblem ? problemOf(lineProblem) : undefined} />
      <ol className="receive-list" aria-label="Checklist">
        {receipt.lines.map((line) => (
          <ReceiveLine
            key={line.id}
            receipt={id}
            line={line}
            last={line.id === lastLine}
            error={errors[`lines/${line.id}`]}
            stale={
              lineStale?.line === line.id ? (
                <StaleNote changes={lineStale.changes} what={shortName(line)} />
              ) : null
            }
            busy={
              (check.isPending && check.variables.line === line.id) ||
              (fixing.isPending &&
                "line" in fixing.variables.fix &&
                fixing.variables.fix.line === line.id)
            }
            onCheck={(outcome, text) => {
              if (line.note && text.trim() !== line.note) {
                change({ do: "outcome", line: line.id, outcome, note: text });
                return;
              }
              check.mutate(
                { line: line.id, outcome, note: text },
                {
                  onSettled: begin(`${shortName(line)}’s outcome`),
                  onSuccess: (done) => {
                    update(done.receipt);
                    if (done.found === "again") {
                      const now = done.receipt.lines.find((l) => l.id === line.id);
                      setHeard(
                        `${shortName(line)} was already checked by ${personName(now?.checked_by)}: ${outcomeLabel(now?.outcome).toLowerCase()}. Theirs is kept; change it if it’s wrong.`,
                      );
                    }
                  },
                },
              );
            }}
            onChange={(fix) => {
              setLineStale(null);
              change(fix);
            }}
          />
        ))}
      </ol>
      {receipt.removed_lines.length ? (
        <section className="removed-lines" aria-labelledby="removed-title">
          <h3 id="removed-title">Removed extras</h3>
          <p className="muted">
            Not received or put away. Each is kept with its history, and can be restored.
          </p>
          <ul className="receive-list" aria-labelledby="removed-title">
            {receipt.removed_lines.map((line) => (
              <li key={line.id} className="receive-line receive-removed">
                <div className="receive-what">
                  <strong className="storage-code">{lineName(line)}</strong>
                  {line.note ? <span>{line.note}</span> : null}
                </div>
                <div className="receive-outcomes">
                  <Button
                    label={`Restore ${shortName(line)}`}
                    onClick={() => {
                      change({ do: "restore", line: line.id });
                    }}
                  >
                    Restore
                  </Button>
                </div>
                <LineHistory parent="receipts" id={id} line={line.id} name={shortName(line)} />
              </li>
            ))}
          </ul>
        </section>
      ) : null}
      <HistoryDisclosure
        parent="receipts"
        id={id}
        names={names}
        label="this receipt"
        summary="This receipt’s history: who recorded what, and each change"
      />
    </>,
  );
  return (
    <>
      <section className="card" aria-labelledby="receipt-title" onPointerDown={press}>
        {checklist}
      </section>
      <section className="card" aria-labelledby="handover-title" onPointerDown={press}>
        <h2 className="section-title" id="handover-title">
          Handed over
        </h2>
        {handoffStale ? <StaleNote changes={handoffStale} what="The receipt" /> : null}
        <div className="handover-fields">
          <TextField
            label="Handed over by"
            value={by}
            onChange={setBy}
            maxLength={200}
            error={errors["handed_over_by"]}
            help="A courier and its waybill, or the colleague who brought it."
          />
          <TextField
            label="Note"
            value={note}
            onChange={setNote}
            optional
            multiline
            maxLength={2000}
            help="The shipment’s condition: its temperature logger, dry ice left, the box."
          />
        </div>
        <div className="actions">
          <Button
            busy={fixing.isPending && fixing.variables.fix.do === "handoff" && !asking}
            busyText="Saving…"
            disabled={!handoffChanged}
            onClick={() => {
              setHandoffSaid("");
              change({ do: "handoff", by: by.trim(), note: note.trim() });
            }}
          >
            Save
          </Button>
          <span className="muted" role="status">
            {handoffChanged ? "" : handoffSaid}
          </span>
        </div>
      </section>
      <section className="card" aria-labelledby="putaway-title" onPointerDown={press}>
        <div className="section-head">
          <h2 className="section-title" id="putaway-title">
            Put away
          </h2>
          <p className="muted">
            {putting.length
              ? `${String(putting.length)} received: put each away in a box, or keep it in your custody. Rejected ones stay in your custody, not stored.`
              : "Nothing received yet to put away."}
          </p>
        </div>
        <form
          className="putaway-box"
          onSubmit={(e) => {
            e.preventDefault();
            if (box.trim()) findBox.mutate({ code: box });
          }}
        >
          <TextField
            label="Box"
            value={box}
            onChange={setBox}
            optional
            maxLength={200}
            help="Scan or type the box they go in: its barcode or ID, such as BOX-000014. Each goes in its next free position."
          />
          <div className="actions">
            <Button
              type="submit"
              disabled={!box.trim()}
              busy={findBox.isPending}
              busyText="Finding…"
            >
              Show its free positions
            </Button>
          </div>
        </form>
        {boxFound?.kind === "refused" ? <ScanRefusedNote /> : null}
        {boxFound?.kind === "none" ? (
          <div className="alert alert-warning" role="alert">
            <p>No box you can read has the code “{boxFound.code.trim()}”.</p>
          </div>
        ) : null}
        {boxFound?.kind === "ambiguous" ? (
          <ScanChooser
            code={boxFound.code}
            candidates={boxFound.candidates}
            noun="box"
            onChoose={(c) => {
              findBox.mutate({ code: boxFound.code, chosen: c.id });
            }}
            onCancel={() => {
              setBoxFound(null);
            }}
          />
        ) : null}
        {findBox.error && !scanRefusal(problemOf(findBox.error)) ? (
          <ProblemAlert problem={problemOf(findBox.error)} />
        ) : null}
        {container.error ? <ProblemAlert problem={problemOf(container.error)} /> : null}
        {boxWhyNot ? (
          <div className="alert alert-warning" role="note">
            <p>{boxWhyNot}</p>
          </div>
        ) : null}
        {boxShown && container.data && !shape ? (
          <div className="alert alert-warning" role="note">
            <p>{container.data.hrid} has no positions: choose a box.</p>
          </div>
        ) : null}
        {putting.length ? (
          <div className={shownBox ? "putaway" : undefined}>
            {shownBox && shape ? (
              <div className="putaway-map">
                <BoxMap
                  label={`${stepName(shownBox)}’s positions, with where each goes`}
                  shape={shape}
                  contents={shownBox.contents}
                  findings={marks}
                  showFree
                  compact
                />
                <MapLegend planned />
              </div>
            ) : null}
            <div className="table-scroll">
              <table className="table putaway-plan">
                <caption>
                  {shownBox ? boxCaption(shownBox, free.length) : "Where each goes"}
                </caption>
                <thead>
                  <tr>
                    <th scope="col">Record</th>
                    <th scope="col">Where it goes</th>
                  </tr>
                </thead>
                <tbody>
                  {putting.map((l) => {
                    const d = decided.get(l.id) ?? { kind: "open" };
                    // A position no other line has chosen; never one taken twice.
                    const others = new Set(
                      putting
                        .filter((o) => o.id !== l.id && !kept.has(o.id))
                        .map((o) => where[o.id]),
                    );
                    const wells =
                      shownBox && !cannot.has(l.id) ? free.filter((s) => !others.has(s.label)) : [];
                    return (
                      <tr key={l.id}>
                        <th scope="row">{lineName(l)}</th>
                        <td>
                          <SelectField
                            label={`Where ${shortName(l)} goes`}
                            compact
                            value={d.kind === "put" ? d.position : d.kind === "custody" ? KEEP : ""}
                            options={[
                              ...(d.kind === "open" || !shownBox
                                ? [{ value: "", label: "Not decided" }]
                                : []),
                              ...wells.map((s) => ({
                                value: s.label,
                                label: `Put away in ${shownBox?.hrid ?? ""}, ${s.label}`,
                              })),
                              { value: KEEP, label: "Keep in my custody" },
                            ]}
                            onChange={(v) => {
                              const next = new Set(kept);
                              if (v === KEEP) next.add(l.id);
                              else next.delete(l.id);
                              setKept(next);
                              const rest = Object.fromEntries(
                                Object.entries(where).filter(([line]) => line !== l.id),
                              );
                              setWhere(v === KEEP ? rest : { ...rest, [l.id]: v });
                            }}
                            error={rowErrors.get(l.id)}
                            {...(whyNot.has(l.id) ? { help: whyNot.get(l.id) ?? "" } : {})}
                          />
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        ) : null}
      </section>
      <section className="card" aria-labelledby="complete-title" onPointerDown={press}>
        <h2 className="section-title" id="complete-title">
          Complete
        </h2>
        <div className="complete-feedback" ref={feedback} tabIndex={-1}>
          {completeStale ? (
            <StaleNote
              {...completeStale}
              again="Review the changes, then choose Complete receiving."
            />
          ) : null}
          {problem ? (
            <div className="alert alert-error" role="alert">
              {refusals.length ? (
                <>
                  <p>Nothing was completed. Check each of these, then complete it again:</p>
                  <ul className="complete-refusals">
                    {refusals.map((r) => (
                      <li key={r.key}>
                        {r.about ? <strong className="storage-code">{r.about}: </strong> : null}
                        {r.detail}
                      </li>
                    ))}
                  </ul>
                </>
              ) : (
                <p>{problem.detail}</p>
              )}
            </div>
          ) : null}
        </div>
        <p>
          Completing takes custody of what’s received and puts each away where you chose, as one
          change.
          {discrepancies
            ? ` The requester is told of ${String(discrepancies)} that didn’t arrive as expected.`
            : ""}
        </p>
        {unchecked ? (
          <CheckboxField
            label={`Mark the ${String(unchecked)} not checked as missing`}
            checked={missingRest}
            onChange={setMissingRest}
          />
        ) : null}
        <div className="actions">
          <button
            {...completeButton.attributes}
            onPointerDown={() => {
              pressed.current = { plan: planKey };
            }}
            onKeyDown={(event) => {
              if (event.key === "Enter" || event.key === " ") pressed.current = { plan: planKey };
            }}
            onClick={(event) => {
              // A press held while the plan changed, such as a box's wells appearing under
              // it, completes nothing: only one that began on the plan shown completes it.
              const began = pressed.current;
              pressed.current = null;
              const fresh = began ? began.plan !== null && began.plan === planKey : !event.detail;
              if (!fresh) {
                event.preventDefault();
                return;
              }
              completeButton.press(event, completeNow);
            }}
          >
            {completeButton.content}
          </button>
          <Button
            variant="quiet"
            onClick={() => {
              change({ do: "cancel" });
            }}
          >
            Cancel this receipt
          </Button>
        </div>
        {/* Below the buttons: what it's waiting for changes, and never moves them. */}
        {status ? (
          <p className="muted complete-status" id={statusId}>
            {status}
          </p>
        ) : null}
      </section>
      {asking ? (
        <FixDialog
          receipt={receipt}
          asking={asking}
          busy={fixing.isPending}
          problem={fixing.error ? problemOf(fixing.error) : undefined}
          onNote={(text) => {
            if (asking.fix.do === "outcome")
              setAsking({ ...asking, fix: { ...asking.fix, note: text } });
          }}
          onConfirm={confirm}
          onClose={() => {
            fixing.reset();
            setAsking(null);
          }}
        />
      ) : null}
    </>
  );
}

/** Asks why, for a change to what's recorded: what it was, what it'll be. */
function FixDialog({
  receipt,
  asking,
  busy,
  problem,
  onNote,
  onConfirm,
  onClose,
}: {
  receipt: Receipt;
  asking: Asking;
  busy: boolean;
  problem: ReturnType<typeof problemOf> | undefined;
  onNote: (note: string) => void;
  onConfirm: (reason: string) => void;
  onClose: () => void;
}) {
  const { fix, changes } = asking;
  const line = lineOf(receipt, fix);
  const name = line ? shortName(line) : "";
  const active = !!line && receipt.lines.includes(line);
  const stale = changes ? <StaleNote changes={changes} what={line ? name : "The receipt"} /> : null;
  const common = { busy, problem, stale, onConfirm, onClose };
  switch (fix.do) {
    case "outcome": {
      const before = line?.outcome ?? null;
      const noteNeeded = !!fix.outcome && ["damaged", "rejected"].includes(fix.outcome);
      const noteShown = fix.note ?? line?.note ?? "";
      const reclassify = fix.outcome !== before;
      const title = !reclassify
        ? `Change the note on ${name}?`
        : fix.outcome
          ? `Change ${name} to ${outcomeLabel(fix.outcome).toLowerCase()}?`
          : `Clear ${name}’s outcome?`;
      return (
        <ReasonDialog
          {...common}
          title={title}
          confirmLabel={fix.outcome ? "Save the change" : "Clear it"}
          confirmDisabled={!active || (noteNeeded && !noteShown.trim())}
          focusReason={!(reclassify && noteNeeded)}
        >
          {!active ? <p className="alert alert-warning">It’s no longer on the checklist.</p> : null}
          {reclassify ? (
            <p className="observation-change">
              <span className="visually-hidden">From </span>
              <span className="diff-before">{outcomeLabel(before)}</span>
              <span aria-hidden="true"> → </span>
              <span className="visually-hidden"> to </span>
              <span className="diff-after">{outcomeLabel(fix.outcome)}</span>
            </p>
          ) : (
            <p className="observation-change">
              <span className="visually-hidden">From </span>
              <span className="diff-before">“{line?.note}”</span>
              <span aria-hidden="true"> → </span>
              <span className="visually-hidden"> to </span>
              <span className="diff-after">{fix.note ? `“${fix.note}”` : "no note"}</span>
            </p>
          )}
          {line?.checked_by ? (
            <p className="muted">
              Recorded by {personName(line.checked_by)}
              {line.checked_at ? (
                <>
                  {" "}
                  <Time value={line.checked_at} />
                </>
              ) : null}
              . What was recorded stays in its history.
            </p>
          ) : null}
          {reclassify && fix.outcome && NOTED.includes(fix.outcome) ? (
            <TextField
              label={`Condition note on ${name}`}
              value={noteShown}
              onChange={onNote}
              optional={!noteNeeded}
              multiline
              maxLength={2000}
              help={`What’s wrong with it: the condition it arrived in. Separate from why you’re changing it.`}
            />
          ) : null}
        </ReasonDialog>
      );
    }
    case "remove":
      return (
        <ReasonDialog
          {...common}
          title={`Remove ${name} from the checklist?`}
          question="Why are you removing it?"
          confirmLabel="Remove it"
          confirmDisabled={!active}
        >
          {!active ? <p className="alert alert-warning">It’s already been removed.</p> : null}
          <p>
            It won’t be received or put away. It’s kept in this receipt’s history, and can be
            restored.
          </p>
        </ReasonDialog>
      );
    case "restore":
      return (
        <ReasonDialog
          {...common}
          title={`Restore ${name} to the checklist?`}
          question="Why are you restoring it?"
          confirmLabel="Restore it"
          confirmDisabled={!line || active}
        >
          {active ? (
            <p className="alert alert-warning">It’s already back on the checklist.</p>
          ) : null}
          <p>It comes back as an extra, as it was recorded.</p>
        </ReasonDialog>
      );
    case "handoff":
      return (
        <ReasonDialog {...common} title="Change the handover?" confirmLabel="Save the change">
          <ul className="observation-changes">
            {fix.by !== receipt.handed_over_by ? (
              <li>
                Handed over by:{" "}
                <span className="diff-before">{receipt.handed_over_by || "nobody"}</span>
                <span aria-hidden="true"> → </span>
                <span className="visually-hidden"> changed to </span>
                <span className="diff-after">{fix.by || "nobody"}</span>
              </li>
            ) : null}
            {fix.note !== receipt.note ? (
              <li>
                Note:{" "}
                <span className="diff-before">{receipt.note ? `“${receipt.note}”` : "none"}</span>
                <span aria-hidden="true"> → </span>
                <span className="visually-hidden"> changed to </span>
                <span className="diff-after">{fix.note ? `“${fix.note}”` : "none"}</span>
              </li>
            ) : null}
          </ul>
        </ReasonDialog>
      );
    case "cancel":
      return (
        <ReasonDialog
          {...common}
          title="Cancel this receipt?"
          question="Why are you cancelling it?"
          confirmLabel="Cancel the receipt"
          cancelLabel="Keep it"
        >
          <p>What’s been checked is kept as cancelled; nothing is received or put away.</p>
        </ReasonDialog>
      );
  }
}

function ReceiveLine({
  receipt,
  line,
  last,
  error,
  stale,
  busy,
  onCheck,
  onChange,
}: {
  receipt: string;
  line: ReceiptLine;
  last: boolean;
  error: string | undefined;
  stale: ReactNode;
  busy: boolean;
  /** A first observation: no reason, no version. */
  onCheck: (outcome: Outcome, note: string) => void;
  /** A change to what's recorded. */
  onChange: (fix: Fix) => void;
}) {
  const id = useId();
  const [note, setNote] = useState(line.note);
  // Someone else's note arrives; what's being typed stays.
  const [saved, setSaved] = useState(line.note);
  if (line.note !== saved) {
    if (note === saved) setNote(line.note);
    setSaved(line.note);
  }
  const [wanting, setWanting] = useState<Outcome | null>(null);
  const outcome = line.outcome ?? null;
  const choices = line.expected
    ? OUTCOMES.filter((o) => o.value !== "extra")
    : OUTCOMES.filter((o) => o.value === "extra");
  const shown = wanting ?? outcome;
  const needsNote = !!shown && NOTED.includes(shown);
  const name = lineName(line);
  const short = shortName(line);
  const noteChanged = note.trim() !== line.note;
  return (
    <li
      className={[
        "receive-line",
        outcome ? `receive-${outcome}` : "receive-unchecked",
        last ? "receive-last" : "",
        error ? "receive-invalid" : "",
      ]
        .filter(Boolean)
        .join(" ")}
      aria-labelledby={`${id}-name`}
    >
      <div className="receive-what">
        <strong id={`${id}-name`}>
          {line.ref ? (
            <Link to="/entities/$ref/" params={{ ref: line.ref.hrid }}>
              {line.ref.hrid}
            </Link>
          ) : (
            <span className="storage-code">{line.code}</span>
          )}
        </strong>
        {line.ref && line.ref.title !== line.ref.hrid ? <span>{line.ref.title}</span> : null}
        {line.code && line.ref && line.code !== line.ref.hrid ? (
          <span className="cell-meta storage-code">{line.code}</span>
        ) : null}
        <span className="cell-meta">{line.expected ? "Expected" : "Not expected"}</span>
        {/* A line of its own, there before it's checked: who checked it never moves what's
            below. */}
        <span className="cell-meta receive-checked">
          {line.checked_by && line.checked_at ? (
            <>
              Checked by {personName(line.checked_by)} <Time value={line.checked_at} />
            </>
          ) : (
            "Not checked yet"
          )}
        </span>
        {error ? (
          <p className="field-error" role="alert">
            {error}
          </p>
        ) : null}
      </div>
      <div className="receive-outcomes" role="group" aria-label={`${name}’s outcome`}>
        {choices.map((o) => (
          <button
            key={o.value}
            type="button"
            className={`button outcome outcome-${o.value}`}
            aria-pressed={shown === o.value}
            disabled={busy}
            title={o.help}
            onClick={() => {
              if (outcome) {
                // What's recorded changes only when asked why.
                if (o.value !== outcome) {
                  setWanting(null);
                  onChange({ do: "outcome", line: line.id, outcome: o.value });
                }
                return;
              }
              if (NOTED.includes(o.value) && !note.trim()) {
                setWanting(o.value);
                return;
              }
              setWanting(null);
              onCheck(o.value, note.trim());
            }}
          >
            {o.label}
          </button>
        ))}
        {line.expected ? (
          // Its place is kept while there's nothing to clear, so the outcomes beside it
          // don't move when one is recorded.
          <button
            type="button"
            className={`button button-quiet outcome${outcome ? "" : " outcome-spare"}`}
            disabled={busy || !outcome}
            aria-hidden={outcome ? undefined : true}
            onClick={() => {
              setWanting(null);
              onChange({ do: "outcome", line: line.id, outcome: null });
            }}
          >
            Clear
          </button>
        ) : null}
        {!line.expected ? (
          <button
            type="button"
            className="button button-quiet outcome"
            disabled={busy}
            onClick={() => {
              onChange({ do: "remove", line: line.id });
            }}
          >
            Remove
          </button>
        ) : null}
      </div>
      {stale ? <div className="receive-stale">{stale}</div> : null}
      {/* There from the start, and the same height whatever's chosen: a note opening, or a
          save coming back, never moves this line's outcomes or the lines below. */}
      <form
        className="receive-note"
        onSubmit={(e) => {
          e.preventDefault();
          if (wanting && !outcome) {
            onCheck(wanting, note.trim());
            setWanting(null);
          } else if (outcome && noteChanged) {
            onChange({ do: "outcome", line: line.id, outcome, note: note.trim() });
          }
        }}
      >
        <TextField
          label={`Note on ${short}`}
          value={note}
          onChange={setNote}
          optional={!needsNote}
          maxLength={2000}
          help={
            line.expected
              ? "Needed if it’s damaged or rejected: say what’s wrong with it."
              : "Needed: say what it is, and anything wrong with it."
          }
        />
        <Button
          type="submit"
          busy={busy}
          busyText="Saving…"
          disabled={
            wanting && !outcome
              ? !note.trim()
              : !outcome || !noteChanged || (needsNote && !note.trim())
          }
        >
          {wanting && !outcome
            ? `Mark ${OUTCOMES.find((o) => o.value === wanting)?.label.toLowerCase() ?? ""}`
            : "Save note"}
        </Button>
      </form>
      <LineHistory parent="receipts" id={receipt} line={line.id} name={short} />
    </li>
  );
}

function PastReceipts({ receipts }: { receipts: Receipt[] }) {
  return (
    <section className="card" aria-labelledby="past-title">
      <h2 className="section-title" id="past-title">
        Received
      </h2>
      {receipts.map((r) => {
        const wrong = r.lines.filter((l) => l.outcome && l.outcome !== "matched");
        return (
          <details className="disclosure" key={r.id}>
            <summary>
              {r.completed_at ? <Time value={r.completed_at} /> : null} by{" "}
              {personName(r.received_by)}, from {r.handed_over_by}:{" "}
              {OUTCOMES.filter((o) => (r.outcomes[o.value] ?? 0) > 0)
                .map((o) => `${String(r.outcomes[o.value] ?? 0)} ${o.label.toLowerCase()}`)
                .join(", ")}
            </summary>
            {wrong.length ? (
              <p className="muted">
                The requester was told of {wrong.length} that didn’t arrive as expected.
              </p>
            ) : null}
            <div className="table-scroll">
              <table className="table">
                <caption className="visually-hidden">What this receipt checked</caption>
                <thead>
                  <tr>
                    <th scope="col">Record</th>
                    <th scope="col">Outcome</th>
                    <th scope="col">Note</th>
                    <th scope="col">Put away in</th>
                  </tr>
                </thead>
                <tbody>
                  {r.lines.map((l) => (
                    <tr key={l.id}>
                      <th scope="row">{lineName(l)}</th>
                      <td>{OUTCOMES.find((o) => o.value === l.outcome)?.label ?? "—"}</td>
                      <td>{l.note}</td>
                      <td>
                        {l.put_away ? (
                          <Link to="/storage/$ref/" params={{ ref: l.put_away.hrid }}>
                            {stepName(l.put_away)}
                            {l.put_away.position ? `, ${l.put_away.position}` : ""}
                          </Link>
                        ) : l.received_unplaced ? (
                          "Not put away: kept in custody"
                        ) : (
                          "—"
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <HistoryDisclosure
              parent="receipts"
              id={r.id}
              names={namesOf(r)}
              label="this receipt"
              summary="Its history: who recorded what, and each change"
            />
          </details>
        );
      })}
    </section>
  );
}

function CancelledReceipts({ receipts }: { receipts: Receipt[] }) {
  return (
    <section className="card" aria-labelledby="cancelled-title">
      <h2 className="section-title" id="cancelled-title">
        Cancelled
      </h2>
      {receipts.map((r) => (
        <details className="disclosure" key={r.id}>
          <summary>
            {r.completed_at ? <Time value={r.completed_at} /> : null} by{" "}
            {personName(r.cancelled_by)}
            {r.cancel_reason ? `: “${r.cancel_reason}”` : ""}
          </summary>
          <HistoryDisclosure
            parent="receipts"
            id={r.id}
            names={namesOf(r)}
            label="this receipt"
            summary="Its history: who recorded what, and each change"
          />
        </details>
      ))}
    </section>
  );
}
