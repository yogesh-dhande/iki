/**
 * Reconciling a container by scanning it (ADR 0180): each position scanned, in order or
 * chosen on the map, says what's there or that it's empty. What's found is set against
 * what's recorded: matched, missing, unexpected, or in the wrong position. Each that isn't
 * matched is resolved, and completing applies every resolution as one change.
 */

import { unwrap } from "@ikisci/sdk";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { useMemo, useState } from "react";

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
  return <ReconcileView r={r} />;
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

function ReconcileView({ r: initial }: { r: Reconciliation }) {
  const client = useQueryClient();
  const { say } = useMessages();
  const [r, setR] = useState(initial);
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
  const [missingRest, setMissingRest] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const here = order[cursor];
  const open = r.status === "open" && r.can_change;
  const update = (next: Reconciliation) => {
    setR(next);
    client.setQueryData(reconciliationQuery(next.id).queryKey, next);
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
    async ({
      line,
      resolution,
      note,
    }: {
      line: string;
      resolution: Resolution | null;
      note: string;
    }) =>
      unwrap(
        await iki.PATCH("/api/v1/reconciliations/{reconciliation_id}/lines/{line_id}", {
          params: { path: { reconciliation_id: r.id, line_id: line } },
          body: { resolution, note },
        }),
      ),
    [],
  );
  const complete = useApiMutation(
    async () =>
      unwrap(
        await iki.POST("/api/v1/reconciliations/{reconciliation_id}/complete", {
          params: { path: { reconciliation_id: r.id } },
          body: { missing_rest: missingRest, note: note.trim() },
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
  const record = (code: string | null) => {
    if (!here) return;
    const at = here.label;
    scan.mutate(code, {
      onSuccess: (next) => {
        update(next);
        const line = next.lines.find((l) => l.found === at);
        setHeard(
          code === null
            ? `${at}: empty.`
            : `${at}: ${line ? `${lineName(line)}, ${FINDINGS[line.finding].toLowerCase()}` : code}.`,
        );
        setCursor(Math.min(cursor + 1, order.length - 1));
      },
      onError: (error) => {
        setHeard(problemOf(error).detail);
      },
    });
  };
  const problem = complete.error ? problemOf(complete.error) : undefined;
  const errors = fieldErrors(problem);
  const findings = findingsAt(r);
  const contents = contentsOf(r);
  const toResolve = r.lines.filter(needsResolving);
  const unchecked = r.lines.filter((l) => l.finding === "unchecked").length;
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
                onScan={(code) => {
                  record(code);
                }}
              />
              <div className="actions">
                <Button
                  disabled={scan.isPending}
                  onClick={() => {
                    record(null);
                  }}
                >
                  {here ? `${here.label} is empty` : "Empty"}
                </Button>
                <Button
                  variant="quiet"
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
              if (!open) return;
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
              {unchecked
                ? `Nothing yet: ${String(unchecked)} recorded positions aren’t scanned.`
                : "Nothing: everything scanned is where it’s recorded."}
            </p>
          ) : null}
          <ProblemAlert problem={resolve.error ? problemOf(resolve.error) : undefined} />
          <ul className="resolve-list">
            {toResolve.map((line) => (
              <ResolveLine
                key={line.id}
                line={line}
                disabled={!open}
                busy={resolve.isPending && resolve.variables.line === line.id}
                error={errors[`lines/${line.id}`]}
                onResolve={(resolution, text) => {
                  resolve.mutate(
                    { line: line.id, resolution, note: text },
                    {
                      onSuccess: update,
                    },
                  );
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
          {unchecked ? (
            <CheckboxField
              label={`Count the ${String(unchecked)} not scanned as missing, and take them out of storage`}
              checked={missingRest}
              onChange={setMissingRest}
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
              variant="primary"
              busy={complete.isPending}
              busyText="Completing…"
              onClick={() => {
                complete.mutate(undefined, {
                  onSuccess: (next) => {
                    update(next);
                    say(`Reconciled ${r.container.hrid}.`);
                  },
                });
              }}
            >
              Complete the reconciliation
            </Button>
            <Button
              variant="quiet"
              onClick={() => {
                setCancelling(true);
              }}
            >
              Cancel it
            </Button>
          </div>
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
          cancel.mutate(undefined, {
            onSuccess: (next) => {
              setCancelling(false);
              update(next);
            },
          });
        }}
      >
        <p>What was scanned is kept as cancelled; nothing is changed.</p>
      </ConfirmDialog>
    </>
  );
}

function ResolveLine({
  line,
  disabled,
  busy,
  error,
  onResolve,
}: {
  line: ReconciliationLine;
  disabled: boolean;
  busy: boolean;
  error: string | undefined;
  onResolve: (resolution: Resolution | null, note: string) => void;
}) {
  const [choice, setChoice] = useState<string>(line.resolution ?? "");
  const [note, setNote] = useState(line.note);
  const options = resolutionsFor(line);
  const where =
    line.finding === "wrong_slot"
      ? `Recorded at ${line.expected ?? ""}, found at ${line.found ?? ""}.`
      : line.finding === "missing" || line.finding === "unchecked"
        ? `Recorded at ${line.expected ?? ""}${line.finding === "missing" ? ", not found there" : ", not scanned yet"}.`
        : `Found at ${line.found ?? ""}${line.ref ? ", recorded elsewhere or nowhere" : ": a code no record you can read has"}.`;
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
      <p className="muted">{where}</p>
      {error ? (
        <p className="field-error" role="alert">
          {error}
        </p>
      ) : null}
      {line.resolution && line.resolved_by ? (
        <p className="muted">
          {RESOLUTIONS[line.resolution]}: {personName(line.resolved_by)}
        </p>
      ) : null}
      {!disabled ? (
        <form
          className="resolve-form"
          onSubmit={(e) => {
            e.preventDefault();
            onResolve((choice || null) as Resolution | null, note.trim());
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
          <Button
            type="submit"
            busy={busy}
            busyText="Saving…"
            disabled={!choice || (choice === "kept" && !note.trim())}
          >
            Resolve
          </Button>
        </form>
      ) : null}
    </li>
  );
}
