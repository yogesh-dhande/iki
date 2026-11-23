/**
 * Reconciling a container by scanning it (ADR 0180): each position scanned, in order or
 * chosen on the map, says what's there or that it's empty. What's found is set against
 * what's recorded: matched, missing, unexpected, or in the wrong position. Each that isn't
 * matched is resolved, and completing applies every resolution as one change.
 */

import { unwrap, type Schemas } from "@ikisci/sdk";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { useMemo, useRef, useState } from "react";

import { fieldErrors, iki, problemOf, useApiMutation } from "../api";
import { BoxMap, slotKey } from "../box-map";
import { personName } from "../entities";
import { usePageTitle } from "../layout";
import {
  FINDINGS,
  indexOf,
  needsResolving,
  positionLabel,
  reconciliationQuery,
  RESOLUTIONS,
  resolutionsFor,
  STORAGE,
  stepName,
  type Occupant,
  type Reconciliation,
  type ReconciliationLine,
  type Resolution,
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
  const [cancelling, setCancelling] = useState(false);
  const here = order[cursor];
  const open = r.status === "open" && r.can_change;
  const update = (next: Reconciliation) => {
    const queryKey = reconciliationQuery(next.id).queryKey;
    const latest = client.getQueryData<Reconciliation>(queryKey);
    if (latest && latest.plan_revision > next.plan_revision) return;
    if (next.plan_revision !== reviewedRevision) {
      setNote("");
      setAcknowledgedMissing(null);
      setReviewedRevision(next.plan_revision);
    }
    void client.cancelQueries({ queryKey, exact: true });
    client.setQueryData(queryKey, next);
  };
  const scan = useApiMutation(
    async (code: string | null) =>
      unwrap(
        await iki.POST("/api/v1/reconciliations/{reconciliation_id}/scans", {
          params: { path: { reconciliation_id: r.id } },
          body: { scans: [{ position: here?.label ?? "", code }] },
        }),
      ),
    [],
  );
  const resolve = useApiMutation(
    async ({ line, decision }: { line: string; decision: Schemas["ResolveIn"] }) =>
      unwrap(
        await iki.PATCH("/api/v1/reconciliations/{reconciliation_id}/lines/{line_id}", {
          params: { path: { reconciliation_id: r.id, line_id: line } },
          body: decision,
        }),
      ),
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
    async () =>
      unwrap(
        await iki.POST("/api/v1/reconciliations/{reconciliation_id}/cancel", {
          params: { path: { reconciliation_id: r.id } },
        }),
      ),
    [STORAGE],
  );
  const writing = useRef(false);
  const [saving, setSaving] = useState(false);
  const problem = complete.error ? problemOf(complete.error) : undefined;
  // A background read can reveal a different plan while the approval form is open.
  const resolveProblem = resolve.error ? problemOf(resolve.error) : undefined;
  const mustReview =
    r.plan_revision > reviewedRevision || problem?.status === 409 || resolveProblem?.status === 409;
  const locked = saving || mustReview || r.plan_revision !== reviewedRevision;
  async function saveChange(
    send: () => Promise<Reconciliation>,
    saved?: (next: Reconciliation) => void,
    failed?: (error: unknown) => void,
  ) {
    // Keyboard scans and a second click can arrive before a mutation observer renders.
    if (writing.current || locked) return;
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
  const record = (code: string | null) => {
    if (!here) return;
    const at = here.label;
    void saveChange(
      () => scan.mutateAsync(code),
      (next) => {
        const line = next.lines.find((l) => l.found === at);
        setHeard(
          code === null
            ? `${at}: empty.`
            : `${at}: ${line ? `${lineName(line)}, ${FINDINGS[line.finding].toLowerCase()}` : code}.`,
        );
        setCursor(Math.min(cursor + 1, order.length - 1));
      },
      (error) => {
        setHeard(problemOf(error).detail);
      },
    );
  };
  const errors = fieldErrors(problem);
  const findings = findingsAt(r);
  const contents = contentsOf(r);
  const toResolve = r.lines.filter((line) => line.resolution_stale || needsResolving(line));
  const destinations = toResolve.filter(
    (line) => confirmed(line) && (line.resolution === "moved" || line.resolution === "placed"),
  );
  const ready = !locked && (unchecked.length === 0 || missingRest) && toResolve.every(confirmed);
  const counts = (["matched", "wrong_slot", "missing", "unexpected", "unchecked"] as const).map(
    (k) => ({ k, n: r.lines.filter((l) => l.finding === k).length }),
  );
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
                disabled={locked}
                onScan={(code) => {
                  record(code);
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
                line={line}
                open={open && !mustReview}
                locked={locked}
                busy={resolve.isPending && resolve.variables.line === line.id}
                error={errors[`lines/${line.id}`]}
                onResolve={(resolution, text) => {
                  const decision = { plan_revision: r.plan_revision, resolution, note: text };
                  void saveChange(() => resolve.mutateAsync({ line: line.id, decision }));
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
              <p className="alert alert-warning" role="alert">
                The reconciliation changed. Review the latest count and its planned destinations
                before completing it.
              </p>
              <Button
                onClick={() => {
                  window.location.reload();
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
                    );
                  }}
                >
                  Complete the reconciliation
                </Button>
                <Button
                  variant="quiet"
                  disabled={locked}
                  onClick={() => {
                    setCancelling(true);
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
          <p>
            {r.completed_by ? `By ${personName(r.completed_by)} ` : ""}
            {r.completed_at ? <Time value={r.completed_at} /> : null}. {r.note}
          </p>
        </section>
      )}
      <ConfirmDialog
        isOpen={cancelling}
        title="Cancel this reconciliation?"
        confirmLabel="Cancel it"
        cancelLabel="Keep going"
        busy={cancel.isPending}
        onCancel={() => {
          setCancelling(false);
        }}
        onConfirm={() => {
          void saveChange(
            () => cancel.mutateAsync(undefined),
            () => {
              setCancelling(false);
            },
          );
        }}
      >
        <p>What was scanned is kept as cancelled; nothing is changed.</p>
      </ConfirmDialog>
    </>
  );
}

function ResolveLine({
  line,
  open,
  locked,
  busy,
  error,
  onResolve,
}: {
  line: ReconciliationLine;
  open: boolean;
  locked: boolean;
  busy: boolean;
  error: string | undefined;
  onResolve: (resolution: Resolution | null, note: string) => void;
}) {
  const [choice, setChoice] = useState<Resolution | "">(
    line.resolution_stale ? "" : (line.resolution ?? ""),
  );
  const [note, setNote] = useState(line.resolution_stale ? "" : line.note);
  const options = resolutionsFor(line);
  const canSave =
    !locked && !!choice && options.includes(choice) && (choice !== "kept" || !!note.trim());
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
      <p className="muted">{describeFinding(line)}</p>
      {error ? (
        <p className="field-error" role="alert">
          {error}
        </p>
      ) : null}
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
            label={`What to do about ${lineName(line)}`}
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
    </li>
  );
}
