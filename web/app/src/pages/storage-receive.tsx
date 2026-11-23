/**
 * Receiving a request's material (ADR 0180): what it expects as a checklist, each scanned
 * or marked matched, missing, damaged or rejected, and anything unexpected as extra; who
 * handed it over; where each goes, in the free positions of a box; then one change that
 * takes custody, puts it away, and tells the requester what didn't arrive as expected.
 */

import { unwrap } from "@ikisci/sdk";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { useId, useMemo, useState } from "react";

import { fieldErrors, iki, problemOf, useApiMutation } from "../api";
import { BoxMap, MapLegend, slotKey } from "../box-map";
import { entityQuery, personName } from "../entities";
import { usePageTitle } from "../layout";
import {
  containerQuery,
  freeSlots,
  OUTCOMES,
  receiptsQuery,
  STORAGE,
  stepName,
  type Outcome,
  type Receipt,
  type ReceiptLine,
} from "../storage";
import { ScanBar } from "../storage-ui";
import {
  Button,
  CheckboxField,
  ConfirmDialog,
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

export function ReceivePage() {
  const { ref } = useParams({ from: "/storage/receive/$ref" });
  const request = useQuery(entityQuery(ref));
  const receipts = useQuery(receiptsQuery(ref));
  const hrid = request.data?.hrid ?? ref;
  usePageTitle(`Receive ${hrid}`);
  const data = receipts.data;
  const open = data?.receipts.find((r) => r.status === "open");
  const done = data?.receipts.filter((r) => r.status === "completed") ?? [];
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
    return unwrap(
      await iki.PATCH("/api/v1/receipts/{receipt_id}", {
        params: { path: { receipt_id: started.id } },
        body: { handed_over_by: by.trim(), note: null },
      }),
    );
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

function OpenReceipt({ receipt: initial, request }: { receipt: Receipt; request: string }) {
  const client = useQueryClient();
  const [receipt, setReceipt] = useState(initial);
  const [heard, setHeard] = useState("");
  const [lastLine, setLastLine] = useState<string | null>(null);
  const [by, setBy] = useState(initial.handed_over_by);
  const [note, setNote] = useState(initial.note);
  const [box, setBox] = useState("");
  const [boxShown, setBoxShown] = useState("");
  const [where, setWhere] = useState<Record<string, string>>({});
  const [missingRest, setMissingRest] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const { say } = useMessages();
  const id = receipt.id;
  const refresh = (next: Receipt) => {
    setReceipt(next);
    void client.invalidateQueries({ queryKey: [...STORAGE, "receipts"] });
  };
  const scan = useApiMutation(
    async (code: string) =>
      unwrap(
        await iki.POST("/api/v1/receipts/{receipt_id}/scan", {
          params: { path: { receipt_id: id } },
          body: { code },
        }),
      ),
    [],
  );
  const check = useApiMutation(
    async ({ line, outcome, note }: { line: string; outcome: Outcome | null; note: string }) =>
      unwrap(
        await iki.PATCH("/api/v1/receipts/{receipt_id}/lines/{line_id}", {
          params: { path: { receipt_id: id, line_id: line } },
          body: { outcome, note },
        }),
      ),
    [],
  );
  const drop = useApiMutation(
    async (line: string) =>
      unwrap(
        await iki.DELETE("/api/v1/receipts/{receipt_id}/lines/{line_id}", {
          params: { path: { receipt_id: id, line_id: line } },
        }),
      ),
    [],
  );
  const describe = useApiMutation(
    async () =>
      unwrap(
        await iki.PATCH("/api/v1/receipts/{receipt_id}", {
          params: { path: { receipt_id: id } },
          body: { handed_over_by: by.trim(), note: note.trim() },
        }),
      ),
    [],
  );
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
    async () =>
      unwrap(
        await iki.POST("/api/v1/receipts/{receipt_id}/complete", {
          params: { path: { receipt_id: id } },
          body: {
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
          },
        }),
      ),
    [STORAGE],
  );
  const cancel = useApiMutation(
    async () =>
      unwrap(
        await iki.POST("/api/v1/receipts/{receipt_id}/cancel", {
          params: { path: { receipt_id: id } },
        }),
      ),
    [STORAGE],
  );
  const problem = complete.error ? problemOf(complete.error) : undefined;
  const errors = fieldErrors(problem);
  const lineProblem = check.error ? problemOf(check.error) : undefined;
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
          busy={scan.isPending}
          onScan={(code) => {
            scan.mutate(code, {
              onSuccess: (done) => {
                refresh(done.receipt);
                setLastLine(done.line);
                const line = done.receipt.lines.find((l) => l.id === done.line);
                const name = line ? lineName(line) : code;
                setHeard(
                  done.found === "matched"
                    ? `${name}: matched.`
                    : done.found === "again"
                      ? `${name} was already checked.`
                      : `${code} wasn’t expected: added as extra. Say what it is in its note.`,
                );
              },
              onError: (error) => {
                setHeard(problemOf(error).detail);
              },
            });
          }}
        />
        <p className="scan-heard" role="status" aria-live="polite">
          {heard}
        </p>
        <ProblemAlert problem={lineProblem} />
        <ol className="receive-list" aria-label="Checklist">
          {receipt.lines.map((line) => (
            <ReceiveLine
              key={line.id}
              line={line}
              last={line.id === lastLine}
              error={errors[`lines/${line.id}`]}
              busy={check.isPending && check.variables.line === line.id}
              onCheck={(outcome, note) => {
                check.mutate(
                  { line: line.id, outcome, note },
                  {
                    onSuccess: (next) => {
                      refresh(next);
                    },
                  },
                );
              }}
              onDrop={() => {
                drop.mutate(line.id, {
                  onSuccess: (next) => {
                    refresh(next);
                  },
                });
              }}
            />
          ))}
        </ol>
      </section>
      <section className="card" aria-labelledby="handover-title">
        <h2 className="section-title" id="handover-title">
          Handed over
        </h2>
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
            busy={describe.isPending}
            busyText="Saving…"
            disabled={by.trim() === receipt.handed_over_by && note.trim() === receipt.note}
            onClick={() => {
              describe.mutate(undefined, {
                onSuccess: (next) => {
                  refresh(next);
                  say("Saved.");
                },
              });
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
        <ProblemAlert problem={problem && !Object.keys(errors).length ? problem : undefined} />
        {problem && Object.keys(errors).length ? (
          <div className="alert alert-error" role="alert">
            <p>Check the highlighted lines and fields.</p>
          </div>
        ) : null}
        <p>
          Completing takes custody of what’s received and puts it away, as one change.
          {discrepancies
            ? ` The requester is told of ${String(discrepancies)} that ${discrepancies === 1 ? "didn’t" : "didn’t"} arrive as expected.`
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
              complete.mutate(undefined, {
                onSuccess: () => {
                  say(`Received ${receipt.request.hrid}.`);
                  void client.invalidateQueries({ queryKey: STORAGE });
                },
              });
            }}
          >
            Complete receiving
          </Button>
          <Button
            variant="quiet"
            onClick={() => {
              setCancelling(true);
            }}
          >
            Cancel this receipt
          </Button>
        </div>
      </section>
      <ConfirmDialog
        isOpen={cancelling}
        title="Cancel this receipt?"
        confirmLabel="Cancel the receipt"
        cancelLabel="Keep it"
        busy={cancel.isPending}
        problem={cancel.error ? problemOf(cancel.error) : undefined}
        onCancel={() => {
          setCancelling(false);
        }}
        onConfirm={() => {
          cancel.mutate(undefined, {
            onSuccess: () => {
              setCancelling(false);
              say(`Cancelled receiving ${request}.`);
            },
          });
        }}
      >
        <p>What’s been checked is kept as cancelled; nothing is received or put away.</p>
      </ConfirmDialog>
    </>
  );
}

function ReceiveLine({
  line,
  last,
  error,
  busy,
  onCheck,
  onDrop,
}: {
  line: ReceiptLine;
  last: boolean;
  error: string | undefined;
  busy: boolean;
  onCheck: (outcome: Outcome | null, note: string) => void;
  onDrop: () => void;
}) {
  const id = useId();
  const [note, setNote] = useState(line.note);
  const [wanting, setWanting] = useState<Outcome | null>(null);
  const outcome = line.outcome ?? null;
  const choices = line.expected
    ? OUTCOMES.filter((o) => o.value !== "extra")
    : OUTCOMES.filter((o) => o.value === "extra");
  const shown = wanting ?? outcome;
  const needsNote = !!shown && NOTED.includes(shown);
  const name = lineName(line);
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
              onCheck(null, note.trim());
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
            onClick={onDrop}
          >
            Remove
          </button>
        ) : null}
      </div>
      {needsNote || note || shown ? (
        <form
          className="receive-note"
          onSubmit={(e) => {
            e.preventDefault();
            onCheck(wanting ?? outcome, note.trim());
            setWanting(null);
          }}
        >
          <TextField
            label={`Note on ${line.ref?.hrid ?? line.code}`}
            value={note}
            onChange={setNote}
            optional={!needsNote}
            maxLength={2000}
            {...(needsNote
              ? { help: `Say why it’s ${shown}: what’s wrong with it, or what it is.` }
              : {})}
          />
          <Button type="submit" busy={busy} busyText="Saving…" disabled={needsNote && !note.trim()}>
            {wanting
              ? `Mark ${OUTCOMES.find((o) => o.value === wanting)?.label.toLowerCase() ?? ""}`
              : "Save note"}
          </Button>
        </form>
      ) : null}
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
          </details>
        );
      })}
    </section>
  );
}
