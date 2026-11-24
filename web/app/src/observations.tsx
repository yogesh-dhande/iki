/**
 * What receiving and counting share for observations (ADR 0180): the dialog that asks why
 * someone is changing what was recorded, the history of who recorded what and each change,
 * and what to say when someone else changed it first.
 */

import { useInfiniteQuery } from "@tanstack/react-query";
import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { Dialog, Heading, Modal, ModalOverlay } from "react-aria-components";

import { fieldErrors, problemOf } from "./api";
import { personName } from "./entities";
import { UtcTime } from "./pages/revision-text";
import {
  historyQuery,
  IDENTITY_BASES,
  OUTCOMES,
  RECEIPT_BASES,
  REASON_MAX,
  RESOLUTIONS,
  type ObservationEvent,
  type Observed,
  type Outcome,
  type Resolution,
} from "./storage";
import { Button, buttonClass, Loading, ProblemAlert } from "./ui";

import type { Problem } from "@ikisci/sdk";

// --- What an observation says -------------------------------------------------------------------

const KINDS: Record<ObservationEvent["kind"], string> = {
  outcome: "Outcome",
  handoff: "Handover",
  scan: "Scan",
  resolution: "Decision",
  status: "Status",
};

const STATUSES: Record<string, string> = {
  open: "Started",
  completed: "Completed",
  cancelled: "Cancelled",
};

type Snapshot = Record<string, unknown> | null;

function text(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value);
}

/** A code as recorded, or that its record is no longer one the reader can open. */
function codeText(value: unknown): string {
  if (value === "") return "a record you can’t open";
  return text(value);
}

/** What a snapshot keeps for the record, but a reader needn't be told twice. */
const UNSAID = new Set([
  "line_revision",
  "checked_by",
  "checked_at",
  "scanned_by",
  "scanned_at",
  "resolved_by",
  "resolved_at",
  "observation",
  "observed",
  "input_unknown",
  "finding",
  "expected",
  // A receipt scan's evidence, said on a line of its own.
  "scan",
  // A count's decision about an unregistered label: said in the event's title.
  "unregistered_action",
]);

/**
 * One side of a change, in words: “Damaged · note “Cap cracked””, “LB-0012 at A2”, “Empty
 * at A3”. Names the facts it knows, and any other as it's given.
 */
export function factText(kind: ObservationEvent["kind"], snapshot: Snapshot): string {
  if (!snapshot) return "Nothing recorded";
  const parts: string[] = [];
  const rest = { ...snapshot };
  if (kind === "scan") {
    const at = typeof rest["position"] === "string" ? rest["position"] : null;
    if (!at) return "Not scanned";
    const code = rest["code"];
    parts.push(
      code === null
        ? `Empty at ${at}`
        : code === undefined
          ? `Scanned at ${at}`
          : `${codeText(code)} at ${at}`,
    );
    delete rest["code"];
    delete rest["position"];
  }
  // A receipt line's code is its name, said beside it.
  if (kind === "outcome") delete rest["code"];
  // What was recorded, then the note on it, whatever order the snapshot keeps.
  const entries = Object.entries(rest).sort(([a], [b]) => +(a === "note") - +(b === "note"));
  for (const [key, value] of entries) {
    if (value === undefined || UNSAID.has(key) || key.endsWith("_id")) continue;
    switch (key) {
      case "outcome":
        parts.push(
          value === null
            ? "Not checked"
            : (OUTCOMES.find((o) => o.value === (value as Outcome))?.label ?? text(value)),
        );
        break;
      case "resolution":
        parts.push(
          value === null
            ? "No decision"
            : Object.hasOwn(RESOLUTIONS, text(value))
              ? RESOLUTIONS[value as Resolution]
              : text(value),
        );
        break;
      case "status":
        parts.push(STATUSES[text(value)] ?? text(value));
        break;
      case "note":
        if (value) parts.push(`note “${text(value)}”`);
        break;
      case "handed_over_by":
        if (value) parts.push(`handed over by ${text(value)}`);
        break;
      case "code":
        parts.push(codeText(value));
        break;
      case "position":
        if (value) parts.push(`at ${text(value)}`);
        break;
      case "removed":
        if (value === true) parts.push("removed from the checklist");
        break;
      case "identity_basis":
        if (typeof value === "string" && Object.hasOwn(IDENTITY_BASES, value))
          parts.push(`identified ${IDENTITY_BASES[value as keyof typeof IDENTITY_BASES]}`);
        break;
      default:
        if (value !== null && value !== "")
          parts.push(`${key.replaceAll("_", " ")} ${text(value)}`);
    }
  }
  const joined = parts.join(" · ");
  return joined ? joined.charAt(0).toUpperCase() + joined.slice(1) : "Nothing recorded";
}

/** What an event did: “Outcome recorded”, “Scan replaced”, “Cancelled”. */
export function eventTitle(event: Pick<ObservationEvent, "kind" | "before" | "after">): string {
  const { kind, before, after } = event;
  if (kind === "status") return STATUSES[text(after?.["status"])] ?? "Status changed";
  const name = KINDS[kind];
  // An unregistered label seen in another slot before: the person said which tube it is.
  if (kind === "scan" && after?.["unregistered_action"] === "additional")
    return "Recorded as another tube";
  if (kind === "scan" && after?.["unregistered_action"] === "move") {
    const from = before?.["position"];
    return typeof from === "string" && from ? `Moved from ${from}` : "Moved here";
  }
  if (after?.["removed"] === true && before?.["removed"] !== true)
    return "Removed from the checklist";
  if (before?.["removed"] === true && after?.["removed"] !== true)
    return "Restored to the checklist";
  if (!before) return `${name} recorded`;
  // A count's scan goes only when its position is scanned again.
  if (!after) return kind === "scan" ? "Scan replaced" : `${name} cleared`;
  if (kind === "outcome" && before["outcome"] != null && after["outcome"] == null)
    return "Outcome cleared";
  // An expected tube's first check.
  if (kind === "outcome" && before["outcome"] == null && after["outcome"] != null)
    return "Outcome recorded";
  if (kind === "scan" && before["observed"] === true && after["observed"] === false)
    return "Scan replaced";
  // An expected tube's first scan.
  if (kind === "scan" && before["observed"] === false && after["observed"] === true)
    return "Scan recorded";
  return `${name} changed`;
}

/** A receipt scan's evidence, as the server keeps it with the observation. */
interface ScanEvidence {
  code: string;
  identity_basis?: string;
  scope?: string;
  chosen?: boolean;
}

function evidenceOf(snapshot: Snapshot): ScanEvidence | null {
  const scan = snapshot?.["scan"];
  if (!scan || typeof scan !== "object") return null;
  const found = scan as Record<string, unknown>;
  return typeof found["code"] === "string" ? (found as unknown as ScanEvidence) : null;
}

/**
 * What a scan read, exactly as the scanner sent it, and how it identified the tube: “Scanned
 * “RC001” · its vendor barcode · on this checklist”. Only what the server returns is said;
 * a manual or earlier observation has none.
 */
function ScanFacts({ scan, earlier = false }: { scan: ScanEvidence; earlier?: boolean }) {
  const basis = scan.identity_basis ? RECEIPT_BASES[scan.identity_basis] : undefined;
  const where =
    scan.scope === "receipt"
      ? "on this checklist"
      : scan.scope === "inventory"
        ? "among the records you can read"
        : undefined;
  return (
    <p className="observation-scan">
      {earlier ? "Its earlier scan read " : "Scanned "}
      <code className="scan-raw">{scan.code}</code>
      {basis ? ` · ${basis}` : null}
      {where ? ` ${where}` : null}
      {scan.chosen ? " · chosen by the receiver" : null}
    </p>
  );
}

/** Before and after, or just what was recorded. */
function Change({ event }: { event: ObservationEvent }) {
  if (event.kind === "status") return null;
  const after = factText(event.kind, event.after);
  if (!event.before) return <p className="observation-change">{after}</p>;
  return (
    <p className="observation-change">
      <span className="visually-hidden">From </span>
      <span className="diff-before">{factText(event.kind, event.before)}</span>
      <span aria-hidden="true"> → </span>
      <span className="visually-hidden"> to </span>
      <span className="diff-after">{event.after ? after : "nothing"}</span>
    </p>
  );
}

/** The scan an observation records, or that its correction replaced. */
function evidence(event: ObservationEvent): ReactNode {
  const after = evidenceOf(event.after);
  if (after) return <ScanFacts scan={after} />;
  const before = evidenceOf(event.before);
  return before ? <ScanFacts scan={before} earlier /> : null;
}

// --- History ------------------------------------------------------------------------------------

/** Each observation and correction, newest first: who, when, before and after, and why. */
export function ObservationHistory({
  parent,
  id,
  line,
  names,
  label,
}: {
  parent: Observed;
  id: string;
  /** Only this line's. */
  line?: string;
  /** Each line's name, to say which an event was about: in a whole history. */
  names?: ReadonlyMap<string, string>;
  /** Whose history, for its list's name and its “Show older”. */
  label: string;
}) {
  const query = useInfiniteQuery(historyQuery(parent, id, line));
  const items = query.data?.pages.flatMap((p) => p.items) ?? [];
  return (
    <div className="observation-history">
      {query.isPending ? <Loading label={`Loading the history of ${label}…`} /> : null}
      {query.error ? <ProblemAlert problem={problemOf(query.error)} /> : null}
      {query.isSuccess && items.length === 0 ? (
        <p className="muted">Nothing’s been recorded yet.</p>
      ) : null}
      {items.length ? (
        <ol className="trail observation-trail" aria-label={`History of ${label}, newest first`}>
          {items.map((event) => {
            const about = !line && event.line_id ? names?.get(event.line_id) : undefined;
            return (
              <li key={event.id} className="trail-rev">
                <span className="trail-node" aria-hidden="true">
                  {event.revision}
                </span>
                <div className="trail-body">
                  <p className="row-title observation-title">
                    {eventTitle(event)}
                    {about ? <span className="storage-code"> · {about}</span> : null}
                  </p>
                  <Change event={event} />
                  {evidence(event)}
                  <p className="trail-meta">
                    <UtcTime value={event.created_at} /> · {personName(event.actor)}
                  </p>
                  {event.reason ? (
                    <p className="row-reason">
                      <span className="visually-hidden">Reason: </span>“{event.reason}”
                    </p>
                  ) : null}
                </div>
              </li>
            );
          })}
        </ol>
      ) : null}
      {query.hasNextPage ? (
        <div className="actions">
          <Button
            busy={query.isFetchingNextPage}
            busyText="Loading…"
            label={`Show older history of ${label}`}
            onClick={() => void query.fetchNextPage()}
          >
            Show older
          </Button>
        </div>
      ) : null}
    </div>
  );
}

/** A line's history, shown when asked for beside it. */
export function LineHistory({
  parent,
  id,
  line,
  name,
  shown,
}: {
  parent: Observed;
  id: string;
  line: string;
  name: string;
  /** Its visible text, when “History” alone wouldn't say whose. */
  shown?: string;
}) {
  const [open, setOpen] = useState(false);
  const panel = useId();
  const text = shown ?? "History";
  const visible = open ? `Hide ${text.charAt(0).toLowerCase()}${text.slice(1)}` : text;
  return (
    <div className="line-history">
      <button
        type="button"
        className={`${buttonClass("quiet")} line-history-toggle`}
        aria-expanded={open}
        aria-controls={panel}
        aria-label={shown ? undefined : `${visible} of ${name}`}
        onClick={() => {
          setOpen(!open);
        }}
      >
        {visible}
      </button>
      <div id={panel} hidden={!open}>
        {open ? <ObservationHistory parent={parent} id={id} line={line} label={name} /> : null}
      </div>
    </div>
  );
}

/** A receipt's or count's whole history, read when it's opened. */
export function HistoryDisclosure({
  parent,
  id,
  names,
  label,
  summary = "History: who recorded what, and each change",
}: {
  parent: Observed;
  id: string;
  names: ReadonlyMap<string, string>;
  label: string;
  summary?: string;
}) {
  const [open, setOpen] = useState(false);
  return (
    <details
      className="disclosure"
      onToggle={(e) => {
        setOpen(e.currentTarget.open);
      }}
    >
      <summary>{summary}</summary>
      {open ? <ObservationHistory parent={parent} id={id} names={names} label={label} /> : null}
    </details>
  );
}

// --- Someone else changed it first --------------------------------------------------------------

/** One thing that changed: which line, or the handover, and how it reads now. */
export interface StaleItem {
  key: string;
  /** Which line it was, when it was one. */
  about?: string | undefined;
  /** What's recorded now, or what was done: a sentence without its full stop. */
  body: ReactNode;
}

/** History events as changes: what was done, by whom and when, and why. */
export function eventItems(
  changes: readonly ObservationEvent[],
  names: ReadonlyMap<string, string>,
): StaleItem[] {
  return changes.map((event) => ({
    key: event.id,
    about: event.line_id ? names.get(event.line_id) : undefined,
    body: (
      <>
        {eventTitle(event)} by {personName(event.actor)}, <UtcTime value={event.created_at} />
        {event.kind === "status" ? "" : `: ${factText(event.kind, event.after)}`}
        {event.reason ? ` (“${event.reason}”)` : ""}
      </>
    ),
  }));
}

/**
 * What changed since the person looked: said once, as an alert, when their change was
 * refused because it no longer matched what's recorded.
 */
export function StaleNote({
  changes,
  what = "This",
}: {
  changes: readonly StaleItem[];
  /** What changed, in a sentence: “RC001”, “The checklist”. */
  what?: string;
}) {
  return (
    <div className="alert alert-warning" role="alert">
      <p>
        {what} changed while you were looking at it. Check what’s recorded now: nothing has been
        saved, and anything you typed is kept.
      </p>
      {changes.length ? (
        <ul className="stale-changes">
          {changes.map((item) => (
            <li key={item.key}>
              {item.about ? <strong className="storage-code">{item.about}: </strong> : null}
              {item.body}.
            </li>
          ))}
        </ul>
      ) : null}
      <p>If your change still applies, save it again.</p>
    </div>
  );
}

// --- Why ----------------------------------------------------------------------------------------

/**
 * Asks why someone is changing what was recorded, before the change is sent. The reason is
 * required, and kept with the change in the history; it's separate from any condition or
 * decision note. A refused change leaves the dialog open, with the reason as typed.
 */
export function ReasonDialog({
  title,
  children,
  confirmLabel,
  cancelLabel = "Don’t change it",
  question = "Why are you changing this?",
  busy,
  problem,
  stale,
  confirmDisabled = false,
  focusReason = true,
  onConfirm,
  onClose,
}: {
  title: string;
  /** What changes, and any field the change itself needs. */
  children: ReactNode;
  confirmLabel: string;
  cancelLabel?: string;
  question?: string;
  busy: boolean;
  problem?: Problem | undefined;
  /** What changed since the person looked, after a refusal. */
  stale?: ReactNode;
  confirmDisabled?: boolean;
  /** False when a field in `children` should take focus first. */
  focusReason?: boolean;
  onConfirm: (reason: string) => void;
  onClose: () => void;
}) {
  const id = useId();
  const [reason, setReason] = useState("");
  const [missing, setMissing] = useState(false);
  const field = useRef<HTMLTextAreaElement>(null);
  // Focus goes back to what opened it: taken before the reason field takes focus.
  const [opener] = useState(() =>
    document.activeElement instanceof HTMLElement ? document.activeElement : null,
  );
  useEffect(
    () => () => {
      if (opener?.isConnected) opener.focus();
    },
    [opener],
  );
  const serverError = fieldErrors(problem)["reason"];
  const error = missing ? "Say why you’re making this change." : serverError;
  const shownProblem = problem && problem.status !== 409 && !serverError ? problem : undefined;
  return (
    <ModalOverlay
      className="dialog-overlay"
      isOpen
      isDismissable={!busy}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <Modal className="dialog storage-dialog">
        <Dialog className="dialog-body" aria-labelledby={`${id}-title`}>
          <form
            noValidate
            onSubmit={(e) => {
              e.preventDefault();
              const trimmed = reason.trim();
              if (!trimmed) {
                setMissing(true);
                field.current?.focus();
                return;
              }
              onConfirm(trimmed);
            }}
          >
            <Heading slot="title" className="section-title" id={`${id}-title`}>
              {title}
            </Heading>
            {stale}
            <ProblemAlert problem={shownProblem} />
            {children}
            <div className={error ? "field field-invalid" : "field"}>
              <label htmlFor={`${id}-reason`}>{question}</label>
              {error ? (
                <p className="field-error" id={`${id}-error`} role="alert">
                  {error}
                </p>
              ) : null}
              <textarea
                id={`${id}-reason`}
                ref={field}
                rows={3}
                value={reason}
                required
                maxLength={REASON_MAX}
                aria-invalid={error ? "true" : undefined}
                aria-describedby={[error ? `${id}-error` : "", `${id}-help`]
                  .filter(Boolean)
                  .join(" ")}
                // The dialog was just asked for: its first field takes focus.
                // eslint-disable-next-line jsx-a11y/no-autofocus
                autoFocus={focusReason}
                onChange={(e) => {
                  setReason(e.target.value);
                  if (e.target.value.trim()) setMissing(false);
                }}
              />
              <p className="field-help" id={`${id}-help`}>
                Kept in the history with your name, beside what was recorded before.
              </p>
            </div>
            <div className="actions">
              <Button
                type="submit"
                variant="primary"
                busy={busy}
                busyText="Saving…"
                disabled={confirmDisabled}
              >
                {confirmLabel}
              </Button>
              <Button variant="quiet" onClick={onClose} disabled={busy}>
                {cancelLabel}
              </Button>
            </div>
          </form>
        </Dialog>
      </Modal>
    </ModalOverlay>
  );
}
