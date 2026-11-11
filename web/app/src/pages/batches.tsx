/**
 * A batch's page: the runs of one action launched together, one for each of
 * several entities, followed together while they go (ADR 0055).
 */

import { unwrap } from "@ikisci/sdk";
import {
  useInfiniteQuery,
  useQuery,
  useQueryClient,
  useSuspenseQuery,
} from "@tanstack/react-query";
import { Link, useNavigate, useParams, useSearch } from "@tanstack/react-router";
import { useEffect, useRef, useState } from "react";

import { iki, problemOf, projectsQuery, useApiMutation } from "../api";
import { ENTITIES } from "../entities";
import { usePageTitle } from "../layout";
import {
  actionLabel,
  actionsQuery,
  batchQuery,
  batchRunsQuery,
  finished,
  RUNS,
  STATUS,
  useBatchUpdates,
  type Batch,
  type RunStatus,
} from "../runs";
import {
  Badge,
  Button,
  ConfirmDialog,
  Loading,
  PageHeader,
  ProblemAlert,
  RelativeTime,
  SelectField,
  TextField,
  useMessages,
} from "../ui";
import { StatusBadge, VIA } from "./runs";

/** The order a batch's counts read in: going first, then how they ended. */
const ORDER: RunStatus[] = [
  "awaiting_approval",
  "queued",
  "running",
  "succeeded",
  "failed",
  "timed_out",
  "rejected",
  "cancelled",
  "declined",
];

function useBatch(): Batch {
  const { ref } = useParams({ from: "/batches/$ref" });
  return useSuspenseQuery(batchQuery(ref)).data;
}

function counts(batch: Batch): [RunStatus, number][] {
  return ORDER.flatMap((status) => {
    const n = batch.statuses[status] ?? 0;
    return n ? [[status, n] as [RunStatus, number]] : [];
  });
}

function done(batch: Batch): number {
  return counts(batch)
    .filter(([status]) => finished(status))
    .reduce((sum, [, n]) => sum + n, 0);
}

function CancelBatch({ batch }: { batch: Batch }) {
  const [asking, setAsking] = useState(false);
  const [reason, setReason] = useState("");
  const { say } = useMessages();
  const client = useQueryClient();
  const cancel = useApiMutation(
    async () =>
      unwrap(
        await iki.POST("/api/v1/batches/{ref}/cancel", {
          params: { path: { ref: batch.id } },
          body: { reason: reason.trim() },
        }),
      ),
    [RUNS],
  );
  if (!batch.can.cancel) return null;
  const left = batch.size - done(batch);
  const close = () => {
    setAsking(false);
    setReason("");
    cancel.reset();
  };
  return (
    <div className="actions">
      <Button
        variant="danger"
        onClick={() => {
          setAsking(true);
        }}
      >
        Cancel the rest
      </Button>
      <ConfirmDialog
        isOpen={asking}
        title={`Cancel ${String(left)} run${left === 1 ? "" : "s"} of ${batch.hrid}?`}
        confirmLabel="Cancel them"
        cancelLabel="Keep them"
        confirmVariant="danger"
        busy={cancel.isPending}
        problem={cancel.error ? problemOf(cancel.error) : undefined}
        onCancel={close}
        onConfirm={() => {
          cancel.mutate(undefined, {
            onSuccess: (saved) => {
              client.setQueryData(batchQuery(saved.hrid).queryKey, saved);
              close();
              say(
                done(saved) === saved.size
                  ? `Cancelled the rest of ${saved.hrid}.`
                  : `Cancelled the rest of ${saved.hrid}. Workers stop the runs they have.`,
              );
            },
          });
        }}
      >
        <p>
          Runs that haven’t started end now and change nothing. Workers stop those that are going,
          and they change no entities. Runs that have finished keep their results.
        </p>
        <TextField
          label="Reason"
          value={reason}
          onChange={setReason}
          maxLength={2000}
          optional
          multiline
          help="Why, for the record. It’s kept with each run."
        />
      </ConfirmDialog>
    </div>
  );
}

export function BatchPage() {
  const batch = useBatch();
  const { status } = useSearch({ from: "/batches/$ref" });
  const navigate = useNavigate();
  const actions = useQuery(actionsQuery).data;
  const action = actions?.find((a) => a.name === batch.action);
  const label = actionLabel(actions, batch.action);
  usePageTitle(`${label} (${batch.hrid})`);
  const project = useQuery(projectsQuery).data?.find((p) => p.id === batch.project);
  const client = useQueryClient();
  const runs = useInfiniteQuery(batchRunsQuery(batch.hrid, status));
  const items = runs.data?.pages.flatMap((p) => p.items) ?? [];
  const ended = done(batch);
  const going = ended < batch.size;
  useBatchUpdates(batch.hrid, going);

  // When it ends, what its runs changed is read again wherever it shows.
  const wasGoing = useRef(going);
  useEffect(() => {
    if (!going && wasGoing.current) {
      void client.invalidateQueries({ queryKey: ENTITIES });
      void client.invalidateQueries({ queryKey: [...RUNS, "list"] });
    }
    wasGoing.current = going;
  }, [going, client]);

  const each = action?.inputs.find((i) => i.name === batch.each)?.label ?? batch.each;
  const shown = counts(batch);
  const choices = [
    { value: "all", label: `Every status (${String(batch.size)})` },
    ...shown.map(([s, n]) => ({ value: s, label: `${STATUS[s]} (${String(n)})` })),
  ];
  return (
    <>
      <PageHeader
        eyebrow={
          project ? (
            <>
              <Link to="/projects/$slug/runs/" params={{ slug: project.slug }}>
                {project.name}
              </Link>
              {" · Batch"}
            </>
          ) : (
            "Batch"
          )
        }
        title={label}
        badge={<Badge>{batch.hrid}</Badge>}
        actions={<CancelBatch batch={batch} />}
      />

      <section className="card" aria-labelledby="progress-title">
        <h2 className="section-title" id="progress-title">
          {String(batch.size)} run{batch.size === 1 ? "" : "s"}, one for each {each}
        </h2>
        <div className="run-progress">
          <progress value={ended} max={batch.size} aria-label={`${batch.hrid} progress`} />
          <span>
            {going ? `${String(ended)} of ${String(batch.size)} finished` : "All finished"}
            {!going && batch.finished_at ? (
              <>
                {" "}
                <RelativeTime value={batch.finished_at} />
              </>
            ) : null}
          </span>
        </div>
        <ul className="value-list" aria-label={`Runs of ${batch.hrid} by status`}>
          {shown.map(([s, n]) => (
            <li key={s}>
              <StatusBadge status={s} /> {String(n)}
            </li>
          ))}
        </ul>
        <dl className="summary">
          <dt>Requested</dt>
          <dd>
            By {batch.requested_by.name} <RelativeTime value={batch.created_at} />{" "}
            {VIA[batch.via] ?? batch.via}
            {batch.client ? `, through ${batch.client}` : ""}
          </dd>
          {batch.reason ? (
            <>
              <dt>Reason</dt>
              <dd className="preserve-lines">{batch.reason}</dd>
            </>
          ) : null}
          <dt>Action</dt>
          <dd>
            <code>{batch.action}</code>
            {action && action.batchable > 1
              ? `: its runs go up to ${String(action.batchable)} to a job, and each keeps its own results and provenance`
              : ""}
          </dd>
        </dl>
      </section>

      <section aria-labelledby="batch-runs-title">
        <h2 className="section-title" id="batch-runs-title">
          Runs
        </h2>
        <div className="toolbar">
          <SelectField
            label="Status"
            compact
            value={status ?? "all"}
            options={choices}
            onChange={(value) => {
              void navigate({
                to: "/batches/$ref/",
                params: { ref: batch.hrid },
                search: value === "all" ? {} : { status: value as RunStatus },
              });
            }}
          />
        </div>
        {runs.isPending ? <Loading label="Loading runs…" /> : null}
        {runs.error ? <ProblemAlert problem={problemOf(runs.error)} /> : null}
        {runs.isSuccess && items.length === 0 && status ? (
          <p className="muted empty">No runs of this batch are {STATUS[status].toLowerCase()}.</p>
        ) : null}
        {items.length ? (
          <ul className="rows">
            {items.map((run) => {
              const input = run.inputs.find((i) => i.name === batch.each);
              return (
                <li className="row" key={run.id}>
                  <div className="row-main">
                    <Link className="row-title" to="/runs/$ref/" params={{ ref: run.hrid }}>
                      {input ? `On ${input.hrid}` : run.hrid}
                    </Link>
                    <span className="row-meta">
                      {run.hrid}
                      {run.finished_at ? (
                        <>
                          {" · ended "}
                          <RelativeTime value={run.finished_at} />
                        </>
                      ) : null}
                    </span>
                    {run.error ? <p className="row-reason">{run.error.message}</p> : null}
                  </div>
                  <div className="row-tags">
                    <StatusBadge status={run.status} />
                  </div>
                </li>
              );
            })}
          </ul>
        ) : null}
        {runs.hasNextPage ? (
          <div className="actions">
            <Button
              busy={runs.isFetchingNextPage}
              busyText="Loading…"
              onClick={() => void runs.fetchNextPage()}
            >
              Show more
            </Button>
          </div>
        ) : null}
      </section>
    </>
  );
}
