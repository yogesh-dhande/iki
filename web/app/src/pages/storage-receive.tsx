/**
 * Receiving a request's material (ADR 0180): what it expects as a checklist, each scanned
 * or marked matched, missing, damaged or rejected, and anything unexpected as extra; who
 * handed it over; where each goes, in the free positions of a box; then one change that
 * takes custody, puts it away, and tells the requester what didn't arrive as expected.
 *
 * A first observation is one step: a scan, or an outcome chosen. Changing what someone
 * recorded (another outcome, Clear, a saved note, removing or restoring an extra, the
 * handover, cancelling) asks why, names the version seen, and keeps both in the history.
 */

import { ApiError, unwrap } from "@ikisci/sdk";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";

import { fieldErrors, iki, problemOf, useApiMutation } from "../api";
import { BoxMap, MapLegend, slotKey } from "../box-map";
import { entityQuery, personName } from "../entities";
import { usePageTitle } from "../layout";
import { HistoryDisclosure, LineHistory, ReasonDialog, StaleNote } from "../observations";
import {
  cancelReceipt,
  changesSince,
  checkReceiptLine,
  completeReceipt,
  containerQuery,
  describeReceipt,
  freeSlots,
  historyKey,
  OUTCOMES,
  readReceipt,
  receiptsQuery,
  removeReceiptLine,
  restoreReceiptLine,
  scanReceipt,
  STORAGE,
  stepName,
  type ObservationEvent,
  type Outcome,
  type Receipt,
  type ReceiptLine,
} from "../storage";
import { ScanBar } from "../storage-ui";
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
      note: null,
      reason: "",
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
      // As the server has it: changing a checked line, or a saved note, is a correction.
      const line = receipt.lines.find((l) => l.id === fix.line);
      if (!line) return false;
      return !!line.outcome || (!!line.note && (fix.note ?? line.note) !== line.note);
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

/** Sends a change, with the version of what it replaces as it's shown now. */
async function send(receipt: Receipt, fix: Fix, reason: string | undefined): Promise<Receipt> {
  const id = receipt.id;
  const why = reason ?? "";
  const line = lineOf(receipt, fix);
  const revision = line?.line_revision ?? 0;
  switch (fix.do) {
    case "outcome": {
      const checked = await checkReceiptLine(id, fix.line, {
        outcome: fix.outcome,
        // The note as shown, unless it's being changed.
        note: fix.note ?? line?.note ?? "",
        line_revision: revision,
        reason: why,
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
        handed_over_by: fix.by,
        note: fix.note,
        reason: why,
      });
    case "cancel":
      return cancelReceipt(id, { plan_revision: receipt.plan_revision, reason: why });
  }
}

/** A change being asked about: what was shown when it was asked, and what changed since. */
interface Asking {
  fix: Fix;
  /** The receipt's revision the person last reviewed. */
  seen: number;
  /** After a refusal: what others changed. */
  changes: ObservationEvent[] | null;
}

function isStale(error: unknown): boolean {
  return error instanceof ApiError && error.status === 409;
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
  const [where, setWhere] = useState<Record<string, string>>({});
  const [missingRest, setMissingRest] = useState(false);
  const [asking, setAsking] = useState<Asking | null>(null);
  const [handoffStale, setHandoffStale] = useState<ObservationEvent[] | null>(null);
  const [completeStale, setCompleteStale] = useState<ObservationEvent[] | null>(null);
  const [lineStale, setLineStale] = useState<{ line: string; changes: ObservationEvent[] } | null>(
    null,
  );
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
  /** After a refusal: the receipt as it is now, and what changed since `seen`. */
  const refetch = async (seen: number, line?: string) => {
    const fresh = await readReceipt(id);
    update(fresh);
    let changes: ObservationEvent[] = [];
    try {
      changes = await changesSince("receipts", id, seen);
    } catch {
      // The receipt is current; who changed it is a courtesy.
    }
    const own = line ? changes.filter((e) => e.line_id === line) : changes;
    return { fresh, changes: own.length ? own : changes };
  };
  const scan = useApiMutation(async (code: string) => scanReceipt(id, code), []);
  const check = useApiMutation(
    async ({ line, outcome, note }: { line: string; outcome: Outcome; note: string }) =>
      checkReceiptLine(id, line, { outcome, note, reason: "" }),
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
      setAsking({ fix, seen: latest.current.plan_revision, changes: null });
      return;
    }
    const seen = latest.current.plan_revision;
    fixing.mutate(
      { fix, reason: undefined },
      {
        onSuccess: (next) => {
          update(next);
          setHandoffStale(null);
          setLineStale(null);
          if (fix.do === "handoff") say("Saved.");
        },
        onError: (error) => {
          if (!isStale(error)) return;
          const line = "line" in fix ? fix.line : undefined;
          void refetch(seen, line).then(({ fresh, changes }) => {
            // Someone recorded it first: now it's a change to theirs, and needs a reason.
            if (needsReason(fix, fresh)) {
              fixing.reset();
              setAsking({ fix, seen: fresh.plan_revision, changes });
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
        onSuccess: (next) => {
          update(next);
          setAsking(null);
          if (fix.do === "restore") setRestorable(null);
          if (fix.do === "cancel") {
            say(`Cancelled receiving ${request}.`);
            void client.invalidateQueries({ queryKey: STORAGE });
          } else say("Saved, with your reason.");
        },
        onError: (error) => {
          if (!isStale(error)) return;
          // Never sent again by itself: the person reviews what's recorded now.
          void refetch(asking.seen, "line" in fix ? fix.line : undefined).then(
            ({ fresh, changes }) => {
              setAsking({ fix, seen: fresh.plan_revision, changes });
            },
          );
        },
      },
    );
  };
  const container = useQuery({ ...containerQuery(boxShown || "-"), enabled: !!boxShown });
  const putting = receipt.lines.filter(
    (l) => l.outcome && RECEIVED.includes(l.outcome) && l.entity,
  );
  const shape = container.data?.shape ?? null;
  const free = useMemo(
    () => (shape && container.data ? freeSlots(shape, container.data.contents) : []),
    [shape, container.data],
  );
  // Each received line's position: the one chosen, or the next free one not chosen.
  const plan = useMemo(() => {
    const taken = new Set(Object.values(where).filter(Boolean));
    const left = free.map((s) => s.label).filter((l) => !taken.has(l));
    const out: Record<string, string> = {};
    for (const l of putting) {
      const chosen = where[l.id];
      out[l.id] = chosen ?? left.shift() ?? "";
    }
    return out;
  }, [free, putting, where]);
  const complete = useApiMutation(
    async (reviewed: Receipt) =>
      completeReceipt(id, {
        plan_revision: reviewed.plan_revision,
        missing_rest: missingRest,
        put_away:
          boxShown && container.data
            ? putting
                .filter((l) => plan[l.id])
                .map((l) => ({
                  line: l.id,
                  to: container.data.id,
                  position: plan[l.id] ?? null,
                }))
            : [],
      }),
    [STORAGE],
  );
  const problem = complete.error ? problemOf(complete.error) : undefined;
  const errors = fieldErrors(problem);
  const lineProblem =
    check.error ?? (fixing.error && !asking && !isStale(fixing.error) ? fixing.error : null);
  const unchecked = receipt.lines.filter((l) => !l.outcome).length;
  const discrepancies = receipt.lines.filter(
    (l) => l.outcome && ["missing", "extra", "damaged", "rejected"].includes(l.outcome),
  ).length;
  const marks = new Map<string, string>();
  if (container.data && shape) {
    for (const l of putting) {
      const pos = plan[l.id];
      const slot = free.find((s) => s.label === pos);
      if (slot) marks.set(slotKey(slot.row, slot.column), "suggested");
    }
  }
  const restoring = restorable ? receipt.removed_lines.find((l) => l.id === restorable) : undefined;
  const handoffChanged = by.trim() !== receipt.handed_over_by || note.trim() !== receipt.note;
  return (
    <>
      <section className="card" aria-labelledby="receipt-title">
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
            scan.mutate(code, {
              onSuccess: (done) => {
                update(done.receipt);
                setLastLine(done.line);
                setRestorable(done.found === "removed" ? done.line : null);
                const line = [...done.receipt.lines, ...done.receipt.removed_lines].find(
                  (l) => l.id === done.line,
                );
                const name = line ? lineName(line) : code;
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
                    setHeard(
                      `${code} wasn’t expected: added as extra. Say what it is in its note.`,
                    );
                }
              },
              onError: (error) => {
                setRestorable(null);
                setHeard(problemOf(error).detail);
              },
            });
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
                  <StaleNote changes={lineStale.changes} names={names} what={shortName(line)} />
                ) : null
              }
              busy={
                (check.isPending && check.variables.line === line.id) ||
                (fixing.isPending &&
                  "line" in fixing.variables.fix &&
                  fixing.variables.fix.line === line.id)
              }
              onCheck={(outcome, text) => {
                if (line.note && text !== line.note) {
                  change({ do: "outcome", line: line.id, outcome, note: text });
                  return;
                }
                check.mutate(
                  { line: line.id, outcome, note: text },
                  {
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
      </section>
      <section className="card" aria-labelledby="handover-title">
        <h2 className="section-title" id="handover-title">
          Handed over
        </h2>
        {handoffStale ? (
          <StaleNote changes={handoffStale} names={names} what="The receipt" />
        ) : null}
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
              change({ do: "handoff", by: by.trim(), note: note.trim() });
            }}
          >
            Save
          </Button>
        </div>
      </section>
      <section className="card" aria-labelledby="putaway-title">
        <div className="section-head">
          <h2 className="section-title" id="putaway-title">
            Put away
          </h2>
          <p className="muted">
            {putting.length
              ? `${String(putting.length)} received to put away. Rejected ones stay in your custody, not stored.`
              : "Nothing received yet to put away."}
          </p>
        </div>
        <form
          className="putaway-box"
          onSubmit={(e) => {
            e.preventDefault();
            setWhere({});
            setBoxShown(box.trim());
          }}
        >
          <TextField
            label="Box"
            value={box}
            onChange={setBox}
            optional
            maxLength={64}
            help="Scan or type the box they go in, such as BOX-000014; each goes in its next free position."
          />
          <div className="actions">
            <Button type="submit" disabled={!box.trim()}>
              Show its free positions
            </Button>
          </div>
        </form>
        {container.error ? <ProblemAlert problem={problemOf(container.error)} /> : null}
        {container.data && !shape ? (
          <div className="alert alert-warning" role="note">
            <p>{container.data.hrid} has no positions: choose a box.</p>
          </div>
        ) : null}
        {container.data && shape ? (
          <div className="putaway">
            <div className="putaway-map">
              <BoxMap
                label={`${stepName(container.data)}’s positions, with where each goes`}
                shape={shape}
                contents={container.data.contents}
                findings={marks}
                showFree
                compact
              />
              <MapLegend />
            </div>
            <div className="table-scroll">
              <table className="table">
                <caption>
                  Where each goes in {stepName(container.data)}: {free.length} free
                </caption>
                <thead>
                  <tr>
                    <th scope="col">Record</th>
                    <th scope="col">Position</th>
                  </tr>
                </thead>
                <tbody>
                  {putting.map((l, i) => (
                    <tr key={l.id}>
                      <th scope="row">{lineName(l)}</th>
                      <td>
                        <SelectField
                          label={`Position for ${l.ref?.hrid ?? l.code}`}
                          compact
                          value={plan[l.id] ?? ""}
                          options={[
                            { value: "", label: "Don’t put it away" },
                            ...free.map((s) => ({ value: s.label, label: s.label })),
                          ]}
                          onChange={(v) => {
                            setWhere({ ...where, [l.id]: v });
                          }}
                          error={errors[`put_away/${String(i)}`]}
                        />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        ) : null}
      </section>
      <section className="card" aria-labelledby="complete-title">
        <h2 className="section-title" id="complete-title">
          Complete
        </h2>
        {completeStale ? (
          <StaleNote changes={completeStale} names={names} what="The checklist" />
        ) : null}
        <ProblemAlert
          problem={
            problem && problem.status !== 409 && !Object.keys(errors).length ? problem : undefined
          }
        />
        {problem && Object.keys(errors).length ? (
          <div className="alert alert-error" role="alert">
            <p>Check the highlighted lines and fields.</p>
          </div>
        ) : null}
        <p>
          Completing takes custody of what’s received and puts it away, as one change.
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
          <Button
            variant="primary"
            busy={complete.isPending}
            busyText="Completing…"
            onClick={() => {
              const reviewed = latest.current;
              complete.mutate(reviewed, {
                onSuccess: () => {
                  setCompleteStale(null);
                  say(`Received ${receipt.request.hrid}.`);
                  void client.invalidateQueries({ queryKey: STORAGE });
                },
                onError: (error) => {
                  if (!isStale(error)) return;
                  // What's to be missing may have changed: it's ticked again once reviewed.
                  setMissingRest(false);
                  void refetch(reviewed.plan_revision).then(({ changes }) => {
                    setCompleteStale(changes);
                  });
                },
              });
            }}
          >
            Complete receiving
          </Button>
          <Button
            variant="quiet"
            onClick={() => {
              change({ do: "cancel" });
            }}
          >
            Cancel this receipt
          </Button>
        </div>
      </section>
      {asking ? (
        <FixDialog
          receipt={receipt}
          asking={asking}
          names={names}
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
  names,
  busy,
  problem,
  onNote,
  onConfirm,
  onClose,
}: {
  receipt: Receipt;
  asking: Asking;
  names: ReadonlyMap<string, string>;
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
  const stale = changes ? (
    <StaleNote changes={changes} names={names} what={line ? name : "The receipt"} />
  ) : null;
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
        <span className="cell-meta">
          {line.expected ? "Expected" : "Not expected"}
          {line.checked_by && line.checked_at ? (
            <>
              {" "}
              · checked by {personName(line.checked_by)} <Time value={line.checked_at} />
            </>
          ) : null}
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
        {outcome && line.expected ? (
          <button
            type="button"
            className="button button-quiet outcome"
            disabled={busy}
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
      {needsNote || note || shown ? (
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
            {...(needsNote
              ? { help: `Say why it’s ${shown}: what’s wrong with it, or what it is.` }
              : {})}
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
      ) : null}
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
