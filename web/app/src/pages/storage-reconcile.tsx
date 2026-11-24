/**
 * Reconciling a container by scanning it (ADR 0180): each position scanned, in order or
 * chosen on the map, says what's there or that it's empty. What's found is set against
 * what's recorded: matched, missing, unexpected, or in the wrong position. Each that isn't
 * matched is resolved, and completing applies every resolution as one change.
 *
 * A fresh scan or a first decision is one step. A scan that replaces what's recorded at a
 * position (an empty one too, or the same tube seen elsewhere), or a changed decision, asks
 * why and names the versions it replaces; the history keeps both.
 */

import { ApiError, unwrap, type Schemas } from "@ikisci/sdk";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";

import { fieldErrors, iki, problemOf, useApiMutation } from "../api";
import { BoxMap, slotKey } from "../box-map";
import { personName } from "../entities";
import { usePageTitle } from "../layout";
import {
  eventItems,
  HistoryDisclosure,
  LineHistory,
  ReasonDialog,
  StaleNote,
} from "../observations";
import {
  affectedLines,
  cancelCount,
  changesSince,
  FINDINGS,
  historyKey,
  IDENTITY_BASES,
  indexOf,
  isScanConflict,
  needsResolving,
  positionLabel,
  reconciliationQuery,
  RESOLUTIONS,
  resolutionsFor,
  resolveCountLine,
  scanCount,
  scanRefusal,
  STORAGE,
  stepName,
  type ObservationEvent,
  type Occupant,
  type Reconciliation,
  type ReconciliationLine,
  type Resolution,
  type Reviewed,
  type ScanCandidate,
  type ScanMode,
  type UnregisteredIntent,
  type UnregisteredSource,
  unregisteredChoice,
} from "../storage";
import { IkiIdEntry, ScanBar, ScanChooser, ScanRefusedNote } from "../storage-ui";
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
import { UtcTime } from "./revision-text";

export function ReconcilePage() {
  const { id } = useParams({ from: "/storage/reconciliations/$id" });
  const found = useQuery(reconciliationQuery(id));
  const r = found.data;
  usePageTitle(r ? `Reconcile ${r.container.hrid}` : "Reconcile");
  if (found.error) {
    return (
      <>
        <PageHeader crumbs={[<Link to="/storage/">Storage</Link>, "Reconcile"]} title="Reconcile" />
        <ProblemAlert problem={problemOf(found.error)} />
      </>
    );
  }
  if (!r) return <Loading label="Loading the reconciliation…" />;
  return <ReconcileView key={r.id} r={r} />;
}

/** Each position's finding, for the map: where it was found, else where it's recorded. */
function findingsAt(r: Reconciliation): Map<string, string> {
  const out = new Map<string, string>();
  const parse = (label: string | null | undefined) => {
    if (!label) return null;
    for (let row = 1; row <= r.shape.rows; row++) {
      for (let column = 1; column <= r.shape.columns; column++) {
        if (positionLabel(r.shape, row, column) === label) return slotKey(row, column);
      }
    }
    return null;
  };
  for (const line of r.lines) {
    const at = parse(line.found) ?? parse(line.expected);
    if (!at) continue;
    if (line.finding === "unchecked" && out.has(at)) continue;
    out.set(at, line.finding);
  }
  return out;
}

/** The map's contents: what was scanned in each position, else what's recorded there. */
function contentsOf(r: Reconciliation): Occupant[] {
  const out: Occupant[] = [];
  const seen = new Set<string>();
  const at = (label: string) => {
    for (let row = 1; row <= r.shape.rows; row++) {
      for (let column = 1; column <= r.shape.columns; column++) {
        if (positionLabel(r.shape, row, column) === label) return { row, column };
      }
    }
    return null;
  };
  const lines = [...r.lines].sort((a, b) => Number(!!b.found) - Number(!!a.found));
  for (const line of lines) {
    const label = line.found ?? line.expected;
    if (!label || seen.has(label) || line.finding === "empty") continue;
    const p = at(label);
    if (!p) continue;
    seen.add(label);
    out.push({
      id: line.entity ?? null,
      ref: line.ref ?? null,
      row: p.row,
      column: p.column,
      position: label,
      code: line.code || null,
      custodian: null,
      custody_since: null,
      container: false,
      count: 0,
    });
  }
  return out;
}

function lineName(line: ReconciliationLine): string {
  return line.ref ? line.ref.hrid : line.code || "An unknown code";
}

function describeFinding(line: ReconciliationLine): string {
  const expected = line.expected ?? "";
  const found = line.found ?? "";
  switch (line.finding) {
    case "wrong_slot":
      return `Recorded at ${expected}, found at ${found}.`;
    case "matched":
      return `Recorded and found at ${expected}.`;
    case "checked_out":
      return `Checked out from ${expected}.`;
    case "empty":
      return `Scanned ${found} as empty.`;
    case "missing":
      return `Recorded at ${expected}, not found there.`;
    case "unchecked":
      return `Recorded at ${expected}, not scanned yet.`;
    case "no_longer_found":
      return "This record no longer has a scanned position in this container.";
    case "unexpected":
      return line.ref
        ? `Found at ${found}, recorded elsewhere or nowhere.`
        : `Found at ${found}. No readable record matches this code.`;
  }
}

/** Scans can change a finding or its destination without replacing its line. */
function decisionKey(line: ReconciliationLine): string {
  return JSON.stringify([
    line.id,
    line.entity,
    line.code,
    line.expected,
    line.found,
    line.finding,
    line.resolution_stale,
    line.resolution,
    line.note,
  ]);
}

function confirmed(line: ReconciliationLine): boolean {
  if (line.resolution_stale || !line.resolution || !resolutionsFor(line).includes(line.resolution))
    return false;
  if (line.resolution === "kept") return !!line.note.trim();
  if (line.resolution === "moved" || line.resolution === "placed")
    return !!(line.entity && line.ref && line.found);
  return true;
}

/** A name for any line, an empty position's too, for a history. */
function historyName(line: ReconciliationLine): string {
  if (line.ref) return line.ref.hrid;
  if (line.code) return line.code;
  return line.found ? `Empty at ${line.found}` : "A position";
}

/**
 * Each line's name, and where it was scanned when another line has the same name: several
 * tubes can carry one unregistered label, “BUFFER A at A1” and “BUFFER A at A2”.
 */
function distinctNames(
  r: Reconciliation,
  name: (line: ReconciliationLine) => string,
): Map<string, string> {
  const uses = new Map<string, number>();
  for (const line of r.lines) uses.set(name(line), (uses.get(name(line)) ?? 0) + 1);
  return new Map(
    r.lines.map((line) => {
      const shown = name(line);
      return [
        line.id,
        (uses.get(shown) ?? 0) > 1 && line.found ? `${shown} at ${line.found}` : shown,
      ];
    }),
  );
}

function namesOf(r: Reconciliation): Map<string, string> {
  return distinctNames(r, historyName);
}

function same(a: string | null | undefined, b: string): boolean {
  return !!a && a.toUpperCase() === b.toUpperCase();
}

/** Where a line's own scan saw it. */
function seenAt(line: ReconciliationLine): string | null {
  return line.found ?? null;
}

/** Evidence a scan would replace: what's recorded at the position, and where else the tube was. */
export interface Superseded {
  /** The physical line whose observation it is. */
  line: ReconciliationLine;
  /** At the position scanned; else the tube, scanned elsewhere. */
  here: boolean;
}

/** A scan of a label no readable record has: its text is evidence about its slot alone. */
function unregistered(line: ReconciliationLine): boolean {
  return !line.entity && !line.ref && !!line.code;
}

/**
 * What a scan of `code` (null: empty) at `at` supersedes, as the server has it: the scan
 * recorded at that position (an explicit empty one too), and the same tube's scan at
 * another. Each is a physical line's own scan; a missing tube's line only points to one.
 *
 * An unregistered label seen in another slot is another tube's evidence, unless the person
 * said it's that tube, moved: `moving` names that line, and only it is superseded.
 */
export function supersededBy(
  r: Reconciliation,
  at: string,
  code: string | null,
  moving?: string | null,
): Superseded[] {
  const out: Superseded[] = [];
  for (const line of r.lines) {
    if (!line.found) continue;
    if (line.found === at) out.push({ line, here: true });
    else if (line.id === moving) out.push({ line, here: false });
    else if (
      code !== null &&
      !unregistered(line) &&
      (same(line.code, code) || same(line.ref?.hrid, code))
    )
      out.push({ line, here: false });
  }
  return out;
}

/**
 * The earlier slots offered for an unregistered label, as this count shows them now: each
 * still at the slot it was offered at, with the same label. One that's moved or changed
 * since can't be chosen.
 */
export function offeredLines(
  r: Reconciliation,
  sources: readonly UnregisteredSource[],
  code: string,
): ReconciliationLine[] {
  return sources.flatMap((source) => {
    const line = r.lines.find((l) => l.id === source.line_id);
    return line && unregistered(line) && line.found === source.position && same(line.code, code)
      ? [line]
      : [];
  });
}

/** Whether this count shows each offered slot as the server offered it. */
function showsOffer(r: Reconciliation, sources: readonly UnregisteredSource[]): boolean {
  return sources.every((source) =>
    r.lines.some(
      (l) =>
        l.id === source.line_id &&
        l.found === source.position &&
        l.line_revision === source.line_revision,
    ),
  );
}

/** Scanning what's already recorded, again: new evidence of the same fact. */
function identical(superseded: readonly Superseded[], code: string | null): boolean {
  if (superseded.length !== 1) return false;
  const [only] = superseded;
  if (!only?.here) return false;
  const recorded =
    only.line.observation?.code ?? (only.line.finding === "empty" ? null : only.line.code);
  return code === null
    ? recorded === null || recorded === ""
    : same(recorded, code) || same(only.line.ref?.hrid, code);
}

/** What a superseded observation said: “TUB-000012”, “Empty”. */
function evidenceText(line: ReconciliationLine): string {
  if (line.finding === "empty" || line.observation?.code === null) return "Empty";
  const code = line.observation?.code ?? line.code;
  return line.ref?.hrid ?? (code === "" ? "a record you can’t open" : code);
}

/** Who scanned it, and when, if that's known; and how it identified the tube. */
function ScannedBy({ line }: { line: ReconciliationLine }) {
  const seen = line.observation;
  if (!seen?.scanned_by) return null;
  return (
    <>
      , scanned by {personName(seen.scanned_by)}
      {seen.scanned_at ? (
        <>
          {" "}
          <UtcTime value={seen.scanned_at} />
        </>
      ) : null}
      {seen.identity_basis ? `, identified ${IDENTITY_BASES[seen.identity_basis]}` : null}
    </>
  );
}

/** A change being asked about, what was shown when it was asked, and what changed since. */
type Asking = {
  seen: number;
  changes: ObservationEvent[] | null;
} & (
  | ({
      do: "scan";
      at: string;
      code: string | null;
      /** The lines a refusal said this scan replaces: exactly those are reviewed. */
      required: string[] | null;
      /** For an unregistered label seen in other slots: those slots, to choose among. */
      offer?: UnregisteredSource[] | undefined;
      /** What changed, for a stale note: the slot scanned, or the one a tube moved from. */
      about?: string | undefined;
    } & How)
  | { do: "resolve"; line: string; resolution: Resolution | null; note: string }
  | { do: "cancel" }
);

/** A newer version of what's recorded: not a scan that can't identify a tube, also a 409. */
/** How a scan identifies its tube: by its label (the default), or a typed Iki ID; and the
 * tube chosen for a label several readable tubes share. */
interface How {
  mode?: ScanMode | undefined;
  choice?: ScanCandidate | undefined;
  /** For an unregistered label seen elsewhere: another tube, or a named earlier one moved. */
  unregistered?: UnregisteredIntent | undefined;
}

/** A position's scan, as sent: its mode and choices only when they say something. */
export function scanItem(at: string, code: string | null, how: How): Schemas["ScanIn"] {
  return {
    position: at,
    code,
    ...(how.mode && how.mode !== "label" ? { mode: how.mode } : {}),
    ...(how.choice ? { choice: how.choice.id } : {}),
    ...(how.unregistered ? { unregistered: how.unregistered } : {}),
  };
}

/** The line a scan says moved here, if it says one did. */
function movingLine(how: How): string | null {
  return how.unregistered?.action === "move" ? how.unregistered.line_id : null;
}

/** A count's scan that needs more: a choice among readable tubes, or another way. */
type Unidentified =
  | { kind: "ambiguous"; at: string; code: string; candidates: ScanCandidate[] }
  | { kind: "refused"; mode: ScanMode | undefined }
  | {
      kind: "unregistered";
      at: string;
      code: string;
      how: How;
      sources: UnregisteredSource[];
    };

function isStale(error: unknown): boolean {
  return error instanceof ApiError && error.status === 409 && !isScanConflict(error.problem);
}

/** The line IDs a refused count change named, if it named them. */
function affectedIds(error: unknown): string[] | null {
  return affectedLines(problemOf(error))?.map((l) => l.line_id) ?? null;
}

/**
 * Whether a decision replaces one saved, and so needs a reason, as the server has it: a
 * changed or stale decision does, and so does any change to a saved note; adding the first
 * note to a decision doesn't.
 */
export function decisionNeedsReason(
  line: ReconciliationLine,
  resolution: Resolution | null,
  text: string,
): boolean {
  if (!line.resolution && !line.note) return false;
  if (line.resolution !== resolution || line.resolution_stale) return true;
  return !!line.note.trim() && text.trim() !== line.note;
}

/** A decision to send: its note only when it was edited, and a reason only when given. */
function decisionOf(
  line: ReconciliationLine,
  resolution: Resolution | null,
  text: string,
  reason?: string,
): Schemas["ResolveIn"] {
  const note = text.trim();
  return {
    line_revision: line.line_revision,
    resolution,
    // Left out, the saved note stays; clearing the decision clears its note too.
    ...(resolution !== null && note !== line.note ? { note: note || null } : {}),
    ...(reason ? { reason } : {}),
  };
}

/**
 * What a scan replaces, as shown and as sent: those the server said it replaces, once it
 * has, else those it's seen to replace here; and the earlier line the person said moved.
 */
function replacedBy(
  r: Reconciliation,
  at: string,
  code: string | null,
  required: readonly string[] | null,
  how: How,
): Superseded[] {
  const moving = movingLine(how);
  const out = required
    ? r.lines
        .filter((l) => required.includes(l.id))
        .map((line) => ({ line, here: seenAt(line) === at }))
    : supersededBy(r, at, code, moving);
  const source = moving ? r.lines.find((l) => l.id === moving) : undefined;
  if (source && !out.some(({ line }) => line.id === source.id))
    out.push({ line: source, here: false });
  return out;
}

/** The lines a scan names as reviewed, at the versions shown now. */
function reviewedFor(
  r: Reconciliation,
  at: string,
  code: string | null,
  required: readonly string[] | null,
  how: How,
): Reviewed[] {
  return replacedBy(r, at, code, required, how).map(({ line }) => ({
    line_id: line.id,
    line_revision: line.line_revision,
  }));
}

/**
 * The lines a refusal named, apart from the earlier slots offered: which of those moved is
 * chosen again, and only the one chosen is reviewed.
 */
function withoutOffered(
  required: readonly string[] | null,
  offer: readonly UnregisteredSource[],
): string[] | null {
  if (!required) return null;
  const offered = new Set(offer.map((source) => source.line_id));
  const rest = required.filter((id) => !offered.has(id));
  return rest.length ? rest : null;
}

/** Where a refused choice's evidence changed: the slot a tube was chosen from, else the scan's. */
function aboutOf(r: Reconciliation, asking: { at: string } & How): string {
  const moving = movingLine(asking);
  return (moving ? r.lines.find((l) => l.id === moving)?.found : null) ?? asking.at;
}

/** What the person said a scanned unregistered label is, in words for the status line. */
function intentText(r: Reconciliation, how: How): string | null {
  if (how.unregistered?.action === "additional") return "recorded as another tube";
  const moving = movingLine(how);
  const from = moving ? r.lines.find((l) => l.id === moving) : undefined;
  return from?.found ? `moved from ${from.found}` : null;
}

function ReconcileView({ r }: { r: Reconciliation }) {
  const client = useQueryClient();
  const { say } = useMessages();
  const [reviewedRevision, setReviewedRevision] = useState(r.plan_revision);
  const shape = r.shape;
  const order = useMemo(() => {
    const all: { row: number; column: number; label: string }[] = [];
    for (let row = 1; row <= shape.rows; row++) {
      for (let column = 1; column <= shape.columns; column++) {
        all.push({ row, column, label: positionLabel(shape, row, column) });
      }
    }
    return all.sort((a, b) => indexOf(shape, a.row, a.column) - indexOf(shape, b.row, b.column));
  }, [shape]);
  const [cursor, setCursor] = useState(0);
  const [heard, setHeard] = useState("");
  const [note, setNote] = useState("");
  const unchecked = r.lines.filter((line) => line.finding === "unchecked");
  const uncheckedKey = unchecked
    .map((line) => line.id)
    .sort()
    .join(",");
  const [acknowledgedMissing, setAcknowledgedMissing] = useState<string | null>(null);
  const missingRest = unchecked.length > 0 && acknowledgedMissing === uncheckedKey;
  const [asking, setAsking] = useState<Asking | null>(null);
  const [unidentified, setUnidentified] = useState<Unidentified | null>(null);
  const [lineStale, setLineStale] = useState<{ line: string; changes: ObservationEvent[] } | null>(
    null,
  );
  const here = order[cursor];
  const open = r.status === "open" && r.can_change;
  const names = namesOf(r);
  const lineNames = distinctNames(r, lineName);
  const queryKey = reconciliationQuery(r.id).queryKey;
  const current = () => client.getQueryData<Reconciliation>(queryKey) ?? r;
  const update = (next: Reconciliation) => {
    const latest = client.getQueryData<Reconciliation>(queryKey);
    if (latest && latest.plan_revision > next.plan_revision) return;
    if (next.plan_revision !== reviewedRevision) {
      setNote("");
      setAcknowledgedMissing(null);
      setReviewedRevision(next.plan_revision);
    }
    void client.cancelQueries({ queryKey, exact: true });
    client.setQueryData(queryKey, next);
    void client.invalidateQueries({ queryKey: historyKey("reconciliations", r.id) });
  };
  /** After a refusal: the count as it is now, and what changed since `seen`. */
  const refetch = async (seen: number, lines: readonly string[] = []) => {
    const fresh = await client.query({ ...reconciliationQuery(r.id), staleTime: 0 });
    let changes: ObservationEvent[] = [];
    try {
      changes = await changesSince("reconciliations", r.id, seen);
    } catch {
      // The count is current; who changed it is a courtesy.
    }
    void client.invalidateQueries({ queryKey: historyKey("reconciliations", r.id) });
    const own = lines.length ? changes.filter((e) => e.line_id && lines.includes(e.line_id)) : [];
    return { fresh, changes: own.length ? own : changes };
  };
  const scan = useApiMutation(
    async (sent: Parameters<typeof scanCount>[1]) => scanCount(r.id, sent),
    [],
  );
  const resolve = useApiMutation(
    async ({
      line,
      decision,
    }: {
      line: string;
      decision: Parameters<typeof resolveCountLine>[2];
    }) => resolveCountLine(r.id, line, decision),
    [],
  );
  const complete = useApiMutation(
    async (approval: Schemas["CompleteReconciliationIn"]) =>
      unwrap(
        await iki.POST("/api/v1/reconciliations/{reconciliation_id}/complete", {
          params: { path: { reconciliation_id: r.id } },
          body: approval,
        }),
      ),
    [STORAGE],
  );
  const cancel = useApiMutation(
    async (reason: string) => cancelCount(r.id, { plan_revision: current().plan_revision, reason }),
    [STORAGE],
  );
  const writing = useRef(false);
  const [saving, setSaving] = useState(false);
  const problem = complete.error ? problemOf(complete.error) : undefined;
  // Another person's change, read in the background or met on completing: the approval is
  // for the plan as it was reviewed, so it's reviewed again. Scans and decisions carry
  // their own lines' versions, and go on.
  const mustReview = r.plan_revision > reviewedRevision || problem?.status === 409;
  const locked = saving;
  const reviewChanges = useQuery({
    queryKey: [...historyKey("reconciliations", r.id), "since", reviewedRevision, r.plan_revision],
    queryFn: ({ signal }) => changesSince("reconciliations", r.id, reviewedRevision, signal),
    enabled: mustReview,
  });
  async function saveChange(
    send: () => Promise<Reconciliation>,
    saved?: (next: Reconciliation) => void,
    failed?: (error: unknown) => void,
  ) {
    // Keyboard scans and a second click can arrive before a mutation observer renders.
    if (writing.current) return;
    writing.current = true;
    setSaving(true);
    try {
      const next = await send();
      update(next);
      saved?.(next);
    } catch (error) {
      // Each mutation retains its error for the form. A failed scan is also announced.
      failed?.(error);
    } finally {
      writing.current = false;
      setSaving(false);
    }
  }
  const scanned =
    (at: string, code: string | null, said: string | null = null) =>
    (next: Reconciliation) => {
      const line = next.lines.find((l) => l.found === at);
      setUnidentified(null);
      setHeard(
        code === null
          ? `${at}: empty.`
          : `${at}: ${
              line ? `${lineName(line)}, ${said ?? FINDINGS[line.finding].toLowerCase()}` : code
            }.`,
      );
      const i = order.findIndex((o) => o.label === at);
      setCursor(Math.min(Math.max(i, 0) + 1, order.length - 1));
    };
  /**
   * An unregistered label seen in other slots: nothing was saved, and the person says which
   * tube it is. What's shown is what the server offered: the count is read again first if
   * it shows any of those slots differently.
   */
  const offerChoice = async (at: string, code: string, how: How, sources: UnregisteredSource[]) => {
    if (!showsOffer(current(), sources)) {
      try {
        await client.query({ ...reconciliationQuery(r.id), staleTime: 0 });
      } catch {
        // Offered from what's shown; the server checks the slot chosen again.
      }
    }
    setUnidentified({ kind: "unregistered", at, code, how, sources });
  };
  /**
   * A scan: sent straight away if it's fresh or the same again; else asked about first.
   * `offer` is the choice it answers, for an unregistered label seen in other slots: asked
   * again, if what it replaces changed.
   */
  const record = (code: string | null, how: How = {}, offer?: UnregisteredSource[]) => {
    if (!here || writing.current) return;
    const at = here.label;
    const count = current();
    // A choice about an unregistered label stays shown until it's recorded.
    if (!how.unregistered) setUnidentified(null);
    const replaced = replacedBy(count, at, code, null, how);
    if (replaced.length && !identical(replaced, code)) {
      scan.reset();
      setAsking({
        do: "scan",
        at,
        code,
        ...how,
        offer,
        required: null,
        seen: count.plan_revision,
        changes: null,
      });
      return;
    }
    const reviewed = replaced.map(({ line }) => ({
      line_id: line.id,
      line_revision: line.line_revision,
    }));
    void saveChange(
      () =>
        scan.mutateAsync({
          scans: [scanItem(at, code, how)],
          ...(reviewed.length ? { reviewed_lines: reviewed } : {}),
        }),
      scanned(at, code, intentText(count, how)),
      (error) => {
        const problem = problemOf(error);
        // The same unregistered label was seen in another slot: which tube is this?
        const choice = unregisteredChoice(problem);
        if (choice && code !== null) {
          setHeard("");
          void offerChoice(at, code, how, choice.sources);
          return;
        }
        // A label several tubes share, or one that can't be used here: nothing was recorded.
        const refusal = scanRefusal(problem);
        if (refusal && code !== null) {
          setHeard("");
          setUnidentified(
            refusal.kind === "ambiguous"
              ? { kind: "ambiguous", at, code, candidates: refusal.candidates }
              : { kind: "refused", mode: how.mode },
          );
          return;
        }
        if (!isStale(error)) {
          setHeard(fieldErrors(problem)["scans/0/unregistered"] ?? problem.detail);
          return;
        }
        // Someone scanned it first: now it replaces theirs, and is asked about.
        setUnidentified(null);
        setHeard(`${at} changed while you were scanning it. Check what’s recorded now.`);
        const required = affectedIds(error);
        void refetch(count.plan_revision, required ?? reviewed.map((x) => x.line_id)).then(
          ({ fresh, changes }) => {
            scan.reset();
            setAsking({
              do: "scan",
              at,
              code,
              ...how,
              // Asked again, with what changed: the person chooses again.
              ...(offer ? { unregistered: undefined } : {}),
              offer,
              required: offer ? withoutOffered(required, offer) : required,
              seen: fresh.plan_revision,
              changes,
            });
          },
        );
      },
    );
  };
  /** The person's answer for an unregistered label: another tube now, or a move, asking why. */
  const chooseUnregistered = (
    pending: Extract<Unidentified, { kind: "unregistered" }>,
    intent: UnregisteredIntent,
  ) => {
    record(pending.code, { ...pending.how, unregistered: intent }, pending.sources);
  };
  /** A decision: the first sent as it is; one replacing a saved decision asks why. */
  const decide = (line: ReconciliationLine, resolution: Resolution | null, text: string) => {
    setLineStale(null);
    // A saved decision or note is replaced: the server keeps both, and asks why.
    if (decisionNeedsReason(line, resolution, text)) {
      resolve.reset();
      setAsking({
        do: "resolve",
        line: line.id,
        resolution,
        note: text,
        seen: current().plan_revision,
        changes: null,
      });
      return;
    }
    const seen = current().plan_revision;
    const decision = decisionOf(line, resolution, text);
    void saveChange(
      () => resolve.mutateAsync({ line: line.id, decision }),
      undefined,
      (error) => {
        if (!isStale(error)) return;
        const related = [line.id, line.observation?.line_id].filter((x): x is string => !!x);
        void refetch(seen, related).then(({ fresh, changes }) => {
          resolve.reset();
          const now = fresh.lines.find((l) => l.id === line.id);
          if (now && decisionNeedsReason(now, resolution, text))
            setAsking({
              do: "resolve",
              line: line.id,
              resolution,
              note: text,
              seen: fresh.plan_revision,
              changes,
            });
          else setLineStale({ line: line.id, changes });
        });
      },
    );
  };
  /** The asked-about change, with its reason, at the versions shown now. */
  const confirm = (reason: string) => {
    if (!asking) return;
    const count = current();
    const retry = (lines: readonly string[]) => (error: unknown) => {
      if (!isStale(error)) return;
      // Never sent again by itself: the person reviews what's recorded now.
      const required = affectedIds(error);
      void refetch(asking.seen, required ?? lines).then(({ fresh, changes }) => {
        setAsking(
          asking.do === "scan"
            ? asking.offer
              ? {
                  // Which tube it is was asked about evidence that's changed: asked again.
                  ...asking,
                  unregistered: undefined,
                  required: withoutOffered(required ?? asking.required, asking.offer),
                  seen: fresh.plan_revision,
                  changes,
                  about: aboutOf(count, asking),
                }
              : {
                  ...asking,
                  required: required ?? asking.required,
                  seen: fresh.plan_revision,
                  changes,
                }
            : { ...asking, seen: fresh.plan_revision, changes },
        );
      });
    };
    switch (asking.do) {
      case "scan": {
        const { at, code, required } = asking;
        const reviewed = reviewedFor(count, at, code, required, asking);
        const said = intentText(count, asking);
        void saveChange(
          () =>
            scan.mutateAsync({
              scans: [scanItem(at, code, asking)],
              ...(reviewed.length ? { reviewed_lines: reviewed } : {}),
              reason,
            }),
          (next) => {
            setAsking(null);
            scanned(at, code, said)(next);
          },
          (error) => {
            const choice = unregisteredChoice(problemOf(error));
            if (!choice || code === null) {
              retry(reviewed.map((x) => x.line_id))(error);
              return;
            }
            setUnidentified(null);
            const moving = movingLine(asking);
            if (!moving) {
              // Asked here first: nothing changed, and the reason stays as typed.
              scan.reset();
              setAsking({ ...asking, offer: choice.sources, about: undefined });
              return;
            }
            // The tube chosen can't have moved here any more: the count as it is now, what
            // changed, and the choice again.
            void refetch(asking.seen, [moving]).then(({ fresh, changes }) => {
              scan.reset();
              setAsking({
                ...asking,
                unregistered: undefined,
                offer: choice.sources,
                required: withoutOffered(asking.required, asking.offer ?? []),
                seen: fresh.plan_revision,
                changes,
                about: aboutOf(count, asking),
              });
            });
          },
        );
        return;
      }
      case "resolve": {
        const line = count.lines.find((l) => l.id === asking.line);
        if (!line) return;
        const decision = decisionOf(line, asking.resolution, asking.note, reason);
        void saveChange(
          () => resolve.mutateAsync({ line: line.id, decision }),
          () => {
            setAsking(null);
            say("Saved, with your reason.");
          },
          retry([line.id, line.observation?.line_id].filter((x): x is string => !!x)),
        );
        return;
      }
      case "cancel":
        void saveChange(
          () => cancel.mutateAsync(reason),
          () => {
            setAsking(null);
            say(`Cancelled reconciling ${r.container.hrid}.`);
          },
          retry([]),
        );
    }
  };
  const errors = fieldErrors(problem);
  const findings = findingsAt(r);
  const contents = contentsOf(r);
  const toResolve = r.lines.filter((line) => line.resolution_stale || needsResolving(line));
  const destinations = toResolve.filter(
    (line) => confirmed(line) && (line.resolution === "moved" || line.resolution === "placed"),
  );
  const ready =
    !locked && !mustReview && (unchecked.length === 0 || missingRest) && toResolve.every(confirmed);
  const counts = (["matched", "wrong_slot", "missing", "unexpected", "unchecked"] as const).map(
    (k) => ({ k, n: r.lines.filter((l) => l.finding === k).length }),
  );
  const resolveProblem =
    resolve.error && !asking && !isStale(resolve.error) ? problemOf(resolve.error) : undefined;
  const evidence = here ? supersededBy(r, here.label, null).filter((s) => s.here) : [];
  return (
    <>
      <PageHeader
        crumbs={[
          <Link to="/storage/">Storage</Link>,
          <Link to="/storage/$ref/" params={{ ref: r.container.hrid }}>
            {stepName(r.container)}
          </Link>,
          "Reconcile",
        ]}
        title={`Reconcile ${stepName(r.container)}`}
        description={`Started by ${personName(r.started_by)}. ${
          r.status === "open" ? "Open." : r.status === "completed" ? "Completed." : "Cancelled."
        }`}
      />
      <ul className="finding-counts" aria-label="Found so far">
        {counts.map(({ k, n }) => (
          <li key={k} className={`finding-count box-map-${k}`}>
            <span className="finding-n">{n}</span> {FINDINGS[k]}
          </li>
        ))}
      </ul>
      <div className="reconcile">
        <section className="card box-card" aria-labelledby="scan-title">
          <h2 className="section-title" id="scan-title">
            {open && here ? `Scanning ${here.label}` : "Positions"}
          </h2>
          {open ? (
            <>
              <ScanBar
                label={`What’s in ${here?.label ?? ""}`}
                help="Scan the tube in this position; then the next one is asked for."
                busy={scan.isPending}
                disabled={locked || !!asking}
                onScan={(code) => {
                  record(code.trim());
                }}
              />
              {unidentified?.kind === "refused" ? (
                <ScanRefusedNote mode={unidentified.mode} />
              ) : null}
              {unidentified?.kind === "unregistered" && unidentified.at === here?.label ? (
                <UnregisteredChooser
                  code={unidentified.code}
                  at={unidentified.at}
                  lines={offeredLines(r, unidentified.sources, unidentified.code)}
                  busy={locked}
                  onChoose={(intent) => {
                    chooseUnregistered(unidentified, intent);
                  }}
                  onCancel={() => {
                    setUnidentified(null);
                    setHeard(`Nothing was recorded at ${unidentified.at}.`);
                  }}
                />
              ) : null}
              {unidentified?.kind === "ambiguous" && unidentified.at === here?.label ? (
                <ScanChooser
                  code={unidentified.code}
                  candidates={unidentified.candidates}
                  busy={locked}
                  onChoose={(choice) => {
                    record(unidentified.code, { choice });
                  }}
                  onCancel={() => {
                    setUnidentified(null);
                    setHeard(`Nothing was recorded at ${unidentified.at}.`);
                  }}
                />
              ) : null}
              <IkiIdEntry
                open={unidentified !== null && unidentified.kind !== "unregistered"}
                disabled={locked || !!asking}
                onEnter={(hrid) => {
                  record(hrid.trim(), { mode: "iki_id" });
                }}
              />
              <div className="actions">
                <Button
                  disabled={locked}
                  onClick={() => {
                    record(null);
                  }}
                >
                  {here ? `${here.label} is empty` : "Empty"}
                </Button>
                <Button
                  variant="quiet"
                  disabled={locked}
                  onClick={() => {
                    setCursor(Math.min(cursor + 1, order.length - 1));
                  }}
                >
                  Skip
                </Button>
              </div>
              <p className="scan-heard" role="status" aria-live="polite">
                {heard}
              </p>
              {here && evidence.length ? (
                <div className="slot-evidence">
                  {evidence.map(({ line }) => (
                    <div key={line.id} className="slot-evidence-line">
                      <p className="muted">
                        Recorded at {here.label}: {evidenceText(line)}
                        <ScannedBy line={line} />. A new scan here replaces it, and asks why.
                      </p>
                      <LineHistory
                        parent="reconciliations"
                        id={r.id}
                        line={line.id}
                        name={`${here.label} (${historyName(line)})`}
                      />
                    </div>
                  ))}
                </div>
              ) : null}
            </>
          ) : null}
          <BoxMap
            label={`${stepName(r.container)}’s positions, with what was found`}
            shape={shape}
            contents={contents}
            findings={findings}
            mode="pick"
            {...(here && open ? { marked: { row: here.row, column: here.column } } : {})}
            onActivate={(slot) => {
              if (!open || locked) return;
              const i = order.findIndex((o) => o.row === slot.row && o.column === slot.column);
              if (i >= 0) setCursor(i);
            }}
          />
          <ul className="box-map-legend" aria-label="Key">
            {(["matched", "wrong_slot", "missing", "unexpected", "empty"] as const).map((k) => (
              <li key={k}>
                <span className={`box-map-swatch box-map-${k}`} aria-hidden="true" /> {FINDINGS[k]}
              </li>
            ))}
          </ul>
          {open ? (
            <p className="field-help">Choose a position on the map to scan it next.</p>
          ) : null}
          <HistoryDisclosure
            parent="reconciliations"
            id={r.id}
            names={names}
            label="this count"
            summary="This count’s history: who scanned and decided what, and each change"
          />
        </section>
        <section className="card" aria-labelledby="resolve-title">
          <h2 className="section-title" id="resolve-title">
            To resolve
          </h2>
          {!toResolve.length ? (
            <p className="muted">
              {unchecked.length
                ? `Nothing yet: ${String(unchecked.length)} recorded positions aren’t scanned.`
                : "Nothing: everything scanned is where it’s recorded."}
            </p>
          ) : null}
          <ProblemAlert problem={resolveProblem} />
          <ul className="resolve-list">
            {toResolve.map((line) => (
              <ResolveLine
                key={decisionKey(line)}
                count={r.id}
                line={line}
                name={lineNames.get(line.id) ?? lineName(line)}
                names={names}
                open={open}
                locked={locked}
                busy={resolve.isPending && resolve.variables.line === line.id}
                error={errors[`lines/${line.id}`]}
                stale={
                  lineStale?.line === line.id ? (
                    <StaleNote
                      changes={eventItems(lineStale.changes, names)}
                      what={lineName(line)}
                    />
                  ) : null
                }
                onResolve={(resolution, text) => {
                  decide(line, resolution, text);
                }}
              />
            ))}
          </ul>
        </section>
      </div>
      {open ? (
        <section className="card" aria-labelledby="finish-title">
          <h2 className="section-title" id="finish-title">
            Complete
          </h2>
          {mustReview ? (
            <>
              {/* Shown with what changed, so the button doesn't move as that arrives. */}
              {reviewChanges.isPending ? null : (
                <StaleNote changes={eventItems(reviewChanges.data ?? [], names)} what="The count" />
              )}
              <p>Review the latest count and its planned destinations before completing it.</p>
              <Button
                disabled={reviewChanges.isPending}
                onClick={() => {
                  complete.reset();
                  setNote("");
                  setAcknowledgedMissing(null);
                  setReviewedRevision(r.plan_revision);
                }}
              >
                Review latest count
              </Button>
            </>
          ) : (
            <>
              <ProblemAlert
                problem={
                  problem && !Object.keys(errors).some((k) => k.startsWith("lines/"))
                    ? problem
                    : undefined
                }
              />
              {problem && Object.keys(errors).some((k) => k.startsWith("lines/")) ? (
                <div className="alert alert-error" role="alert">
                  <p>Some positions aren’t scanned or resolved yet: they’re marked above.</p>
                </div>
              ) : null}
              <p>Completing applies each resolution as one change, and keeps what was found.</p>
              {destinations.length ? (
                <>
                  <h3 id="planned-destinations">Planned destinations</h3>
                  <ul aria-labelledby="planned-destinations">
                    {destinations.map((line) => (
                      <li key={line.id}>
                        {lineName(line)} → {r.container.hrid}, {line.found}
                      </li>
                    ))}
                  </ul>
                </>
              ) : null}
              {unchecked.length ? (
                <CheckboxField
                  label={`Count the ${String(unchecked.length)} not scanned as missing, and take them out of storage`}
                  checked={missingRest}
                  onChange={(checked) => {
                    setAcknowledgedMissing(checked ? uncheckedKey : null);
                  }}
                />
              ) : null}
              <TextField
                label="Note"
                value={note}
                onChange={setNote}
                optional
                multiline
                maxLength={2000}
              />
              <div className="actions">
                <Button
                  key={ready ? "ready" : "waiting"}
                  variant="primary"
                  disabled={!ready}
                  busy={complete.isPending}
                  busyText="Completing…"
                  onClick={() => {
                    if (!ready) return;
                    const approval = {
                      plan_revision: reviewedRevision,
                      missing_rest: missingRest,
                      note: note.trim(),
                    };
                    void saveChange(
                      () => complete.mutateAsync(approval),
                      () => {
                        say(`Reconciled ${r.container.hrid}.`);
                      },
                      (error) => {
                        if (isStale(error)) void refetch(reviewedRevision);
                      },
                    );
                  }}
                >
                  Complete the reconciliation
                </Button>
                <Button
                  variant="quiet"
                  disabled={locked}
                  onClick={() => {
                    cancel.reset();
                    setAsking({ do: "cancel", seen: current().plan_revision, changes: null });
                  }}
                >
                  Cancel it
                </Button>
              </div>
            </>
          )}
        </section>
      ) : (
        <section className="card" aria-labelledby="done-title">
          <h2 className="section-title" id="done-title">
            {r.status === "completed" ? "Completed" : "Cancelled"}
          </h2>
          {r.status === "cancelled" ? (
            <p>
              {r.cancelled_by ? `By ${personName(r.cancelled_by)} ` : ""}
              {r.completed_at ? <Time value={r.completed_at} /> : null}
              {r.cancel_reason ? `: “${r.cancel_reason}”` : "."}
            </p>
          ) : (
            <p>
              {r.completed_by ? `By ${personName(r.completed_by)} ` : ""}
              {r.completed_at ? <Time value={r.completed_at} /> : null}. {r.note}
            </p>
          )}
        </section>
      )}
      {asking ? (
        <CountDialog
          r={r}
          asking={asking}
          names={names}
          busy={saving}
          onUnregistered={(intent) => {
            scan.reset();
            if (asking.do === "scan") setAsking({ ...asking, unregistered: intent });
          }}
          problem={
            asking.do === "scan"
              ? scan.error
              : asking.do === "resolve"
                ? resolve.error
                : cancel.error
          }
          onConfirm={confirm}
          onChoose={(choice) => {
            scan.reset();
            if (asking.do === "scan") setAsking({ ...asking, choice });
          }}
          onClose={() => {
            if (asking.do === "scan" && asking.offer && !unidentified)
              setHeard(`Nothing was recorded at ${asking.at}.`);
            setAsking(null);
          }}
        />
      ) : null}
    </>
  );
}

/** Asks why, for a scan replacing what's recorded, a changed decision, or cancelling. */
function CountDialog({
  r,
  asking,
  names,
  busy,
  problem,
  onConfirm,
  onChoose,
  onUnregistered,
  onClose,
}: {
  r: Reconciliation;
  asking: Asking;
  names: ReadonlyMap<string, string>;
  busy: boolean;
  problem: unknown;
  onConfirm: (reason: string) => void;
  /** The tube chosen for a label several share, kept with the reason being typed. */
  onChoose: (choice: ScanCandidate) => void;
  /** What an unregistered label seen elsewhere is, or undefined to choose again. */
  onUnregistered: (intent: UnregisteredIntent | undefined) => void;
  onClose: () => void;
}) {
  // Chosen here, the choice is said where focus goes; chosen before, the reason is asked.
  const [picked, setPicked] = useState(false);
  const shownProblem = problem ? problemOf(problem) : undefined;
  const refusal = scanRefusal(shownProblem);
  const common = { busy, problem: shownProblem, onConfirm, onClose };
  switch (asking.do) {
    case "scan": {
      const { at, code, required, offer, unregistered: intent } = asking;
      const replaced = replacedBy(r, at, code, required, asking);
      const now = code ?? "Empty";
      const choosing = !!offer && !intent && code !== null;
      const movingId = movingLine(asking);
      const moving = movingId ? r.lines.find((l) => l.id === movingId) : undefined;
      const title = moving?.found
        ? `Move the tube from ${moving.found} to ${at}?`
        : offer && code !== null && !replaced.some((x) => x.here)
          ? `Record ‘${code}’ at ${at}?`
          : `Replace what’s recorded at ${at}?`;
      return (
        <ReasonDialog
          {...common}
          stale={
            asking.changes ? (
              <StaleNote changes={eventItems(asking.changes, names)} what={asking.about ?? at} />
            ) : null
          }
          title={title}
          confirmLabel={moving ? "Record the move" : "Record the new scan"}
          confirmDisabled={
            (refusal !== null && !asking.choice) || choosing || (!!movingId && !moving)
          }
          focusReason={!choosing}
        >
          {choosing ? (
            <UnregisteredChooser
              code={code}
              at={at}
              lines={offeredLines(r, offer, code)}
              busy={busy}
              onChoose={(intent) => {
                setPicked(true);
                onUnregistered(intent);
              }}
            />
          ) : null}
          {offer && intent && code !== null ? (
            <UnregisteredChosen
              code={code}
              at={at}
              from={moving?.found ?? null}
              focus={picked}
              busy={busy}
              onAgain={() => {
                onUnregistered(undefined);
              }}
            />
          ) : null}
          {refusal?.kind === "refused" ? <ScanRefusedNote mode={asking.mode} /> : null}
          {refusal?.kind === "ambiguous" && code !== null ? (
            <ScanChooser
              code={code}
              candidates={refusal.candidates}
              busy={busy}
              onChoose={onChoose}
            />
          ) : null}
          {asking.choice ? (
            <p className="muted">Recorded as {asking.choice.hrid}, chosen for this label.</p>
          ) : null}
          <ul className="observation-changes">
            {replaced.map(({ line, here }) => (
              <li key={line.id}>
                {here ? (
                  <>
                    {at}: <span className="diff-before">{evidenceText(line)}</span>
                    <ScannedBy line={line} />
                    <span aria-hidden="true"> → </span>
                    <span className="visually-hidden">, replaced by </span>
                    <span className="diff-after">{now}</span>
                  </>
                ) : (
                  <>
                    {historyName(line)} was scanned at{" "}
                    <span className="diff-before">{seenAt(line)}</span>
                    <ScannedBy line={line} />
                    <span aria-hidden="true"> → </span>
                    <span className="visually-hidden">, now at </span>
                    <span className="diff-after">{at}</span>
                  </>
                )}
              </li>
            ))}
          </ul>
          {moving ? (
            <p className="muted">
              Any other tube with this label stays where it was scanned. What was recorded before
              stays in the history.
            </p>
          ) : (
            <p className="muted">What was recorded before stays in the history.</p>
          )}
        </ReasonDialog>
      );
    }
    case "resolve": {
      const line = r.lines.find((l) => l.id === asking.line);
      const name = line ? lineName(line) : "";
      const before = line?.resolution ? RESOLUTIONS[line.resolution] : "No decision";
      const after = asking.resolution ? RESOLUTIONS[asking.resolution] : "No decision";
      return (
        <ReasonDialog
          {...common}
          stale={
            asking.changes ? (
              <StaleNote changes={eventItems(asking.changes, names)} what={name} />
            ) : null
          }
          title={
            asking.resolution ? `Change the decision on ${name}?` : `Clear the decision on ${name}?`
          }
          confirmLabel={asking.resolution ? "Save the decision" : "Clear it"}
          confirmDisabled={!line}
        >
          <p className="observation-change">
            <span className="visually-hidden">From </span>
            <span className="diff-before">
              {before}
              {line?.note ? `: “${line.note}”` : ""}
            </span>
            <span aria-hidden="true"> → </span>
            <span className="visually-hidden"> to </span>
            <span className="diff-after">
              {after}
              {asking.note ? `: “${asking.note}”` : ""}
            </span>
          </p>
          {line?.resolved_by ? (
            <p className="muted">
              Decided by {personName(line.resolved_by)}. What was decided stays in the history.
            </p>
          ) : null}
        </ReasonDialog>
      );
    }
    case "cancel":
      return (
        <ReasonDialog
          {...common}
          stale={
            asking.changes ? (
              <StaleNote changes={eventItems(asking.changes, names)} what="The count" />
            ) : null
          }
          title="Cancel this reconciliation?"
          question="Why are you cancelling it?"
          confirmLabel="Cancel it"
          cancelLabel="Keep going"
        >
          <p>What was scanned is kept as cancelled; nothing is changed.</p>
        </ReasonDialog>
      );
  }
}

/** What the person chose for an unregistered label, said where focus goes when chosen here. */
function UnregisteredChosen({
  code,
  at,
  from,
  focus,
  busy,
  onAgain,
}: {
  code: string;
  at: string;
  /** Where the tube moved from; null, another tube. */
  from: string | null;
  focus: boolean;
  busy: boolean;
  onAgain: () => void;
}) {
  const said = useRef<HTMLParagraphElement>(null);
  useEffect(() => {
    if (focus) said.current?.focus();
  }, [focus, from]);
  return (
    <div className="unregistered-chosen">
      <p ref={said} tabIndex={-1}>
        ‘{code.trim()}’ at {at}: {from ? `the tube from ${from}, moved here.` : "another tube."}
      </p>
      <Button variant="quiet" disabled={busy} onClick={onAgain}>
        Choose again
      </Button>
    </div>
  );
}

/**
 * An unregistered label scanned here was scanned in another slot too: is it another tube
 * with the same label, or one of those, moved here? Each earlier slot is a line of its own,
 * with who scanned it and when. Nothing is recorded until the person says.
 */
function UnregisteredChooser({
  code,
  at,
  lines,
  busy,
  onChoose,
  onCancel,
}: {
  code: string;
  at: string;
  /** The earlier slots that can still be chosen. */
  lines: readonly ReconciliationLine[];
  busy: boolean;
  onChoose: (intent: UnregisteredIntent) => void;
  onCancel?: () => void;
}) {
  const id = useId();
  const heading = useRef<HTMLHeadingElement>(null);
  const offered = lines.map((line) => `${line.id}@${String(line.line_revision)}`).join(",");
  useEffect(() => {
    // Said, and where the keyboard goes next: the scanner still works from here.
    heading.current?.focus();
  }, [code, at, offered]);
  const label = code.trim();
  return (
    <section className="scan-chooser" aria-labelledby={`${id}-title`}>
      <h3 className="scan-chooser-title" id={`${id}-title`} ref={heading} tabIndex={-1}>
        ‘{label}’ at {at} is…
      </h3>
      <p className="scan-chooser-help">
        {lines.length
          ? "The same label was scanned in another slot. Say which is true; nothing is recorded until you do."
          : "No earlier slot with this label can be chosen now. If it’s a separate tube, record it as another tube; nothing is recorded until you do."}
      </p>
      <ul className="scan-choices" aria-label={`What ‘${label}’ at ${at} is`}>
        <li>
          <button
            type="button"
            className="scan-choice"
            disabled={busy}
            onClick={() => {
              onChoose({ action: "additional" });
            }}
          >
            <span className="scan-choice-name">
              <strong>Another tube</strong>
            </span>
            <span className="scan-choice-meta">
              <span>A separate tube with the same label</span>
            </span>
          </button>
        </li>
        {lines.map((line) => (
          <li key={line.id}>
            <button
              type="button"
              className="scan-choice"
              disabled={busy}
              onClick={() => {
                onChoose({ action: "move", line_id: line.id });
              }}
            >
              <span className="scan-choice-name">
                <strong>The tube from {line.found}, moved here</strong>
              </span>
              {line.observation?.scanned_by ? (
                <span className="scan-choice-meta">
                  <span>
                    Scanned at {line.found} by {personName(line.observation.scanned_by)}
                    {line.observation.scanned_at ? (
                      <>
                        {" "}
                        <UtcTime value={line.observation.scanned_at} />
                      </>
                    ) : null}
                  </span>
                </span>
              ) : null}
            </button>
          </li>
        ))}
      </ul>
      {onCancel ? (
        <div className="actions">
          <Button variant="quiet" onClick={onCancel} disabled={busy}>
            Cancel
          </Button>
        </div>
      ) : null}
    </section>
  );
}

function ResolveLine({
  count,
  line,
  name,
  names,
  open,
  locked,
  busy,
  error,
  stale,
  onResolve,
}: {
  count: string;
  line: ReconciliationLine;
  /** Its name, and where it was scanned when another line has the same. */
  name: string;
  names: ReadonlyMap<string, string>;
  open: boolean;
  locked: boolean;
  busy: boolean;
  error: string | undefined;
  stale: ReactNode;
  onResolve: (resolution: Resolution | null, note: string) => void;
}) {
  const [choice, setChoice] = useState<Resolution | "">(
    line.resolution_stale ? "" : (line.resolution ?? ""),
  );
  const [note, setNote] = useState(line.resolution_stale ? "" : line.note);
  const options = resolutionsFor(line);
  const unchanged =
    !line.resolution_stale && choice === line.resolution && note.trim() === line.note;
  const canSave =
    !locked &&
    !!choice &&
    options.includes(choice) &&
    (choice !== "kept" || !!note.trim()) &&
    !unchanged;
  // A missing tube's evidence can be another line's scan: its history is that line's.
  const evidence = line.observation;
  const elsewhere = evidence && evidence.line_id !== line.id ? evidence : null;
  return (
    <li className={`resolve-line box-map-${line.finding}${error ? " resolve-invalid" : ""}`}>
      <p>
        <strong>
          {line.ref ? (
            <Link to="/entities/$ref/" params={{ ref: line.ref.hrid }}>
              {line.ref.hrid}
            </Link>
          ) : (
            <span className="storage-code">{line.code}</span>
          )}
        </strong>{" "}
        <span className="badge">{FINDINGS[line.finding]}</span>
      </p>
      <p className="muted">
        {describeFinding(line)}
        {evidence?.scanned_by ? (
          <>
            {" "}
            Scanned by {personName(evidence.scanned_by)}
            {evidence.scanned_at ? (
              <>
                {" "}
                <UtcTime value={evidence.scanned_at} />
              </>
            ) : null}
            .
          </>
        ) : null}
      </p>
      {error ? (
        <p className="field-error" role="alert">
          {error}
        </p>
      ) : null}
      {stale}
      {line.resolution_stale ? (
        <p className="alert alert-warning" role="alert">
          The scan changed. Review this finding and choose again.
        </p>
      ) : null}
      {confirmed(line) && line.resolution && line.resolved_by ? (
        <p className="muted">
          {RESOLUTIONS[line.resolution]}: {personName(line.resolved_by)}
        </p>
      ) : null}
      {open && options.length ? (
        <form
          className="resolve-form"
          onSubmit={(e) => {
            e.preventDefault();
            if (canSave) onResolve(choice, note.trim());
          }}
        >
          <SelectField
            label={`What to do about ${name}`}
            value={choice}
            options={[
              { value: "", label: "Choose…" },
              ...options.map((o) => ({ value: o, label: RESOLUTIONS[o] })),
            ]}
            onChange={setChoice}
          />
          <TextField
            label="Note"
            value={note}
            onChange={setNote}
            optional={choice !== "kept"}
            maxLength={2000}
            {...(choice === "kept" ? { help: "Say why it’s left as recorded." } : {})}
          />
          <Button type="submit" busy={busy} busyText="Saving…" disabled={!canSave}>
            Resolve
          </Button>
        </form>
      ) : open && line.resolution_stale ? (
        <Button
          disabled={locked}
          busy={busy}
          onClick={() => {
            onResolve(null, "");
          }}
        >
          Clear previous decision
        </Button>
      ) : null}
      <div className="resolve-history">
        <LineHistory parent="reconciliations" id={count} line={line.id} name={name} />
        {elsewhere ? (
          <LineHistory
            parent="reconciliations"
            id={count}
            line={elsewhere.line_id}
            name={names.get(elsewhere.line_id) ?? elsewhere.position}
            shown={`History of the scan at ${elsewhere.position}`}
          />
        ) : null}
      </div>
    </li>
  );
}
