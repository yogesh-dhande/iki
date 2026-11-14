/**
 * A workflow run's page: its steps in the order they start, each an ordinary run, followed
 * while they go (ADR 0079).
 */

import { unwrap } from "@ikisci/sdk";
import { useQuery, useQueryClient, useSuspenseQuery } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { useEffect, useRef, useState } from "react";

import { AgentRows, Through } from "../agent";
import { iki, problemOf, projectsQuery, useApiMutation } from "../api";
import { ENTITIES } from "../entities";
import { usePageTitle } from "../layout";
import {
  finished,
  RUNS,
  STATUS,
  STEP_STATE,
  tone,
  useWorkflowUpdates,
  workflowRunQuery,
  type RunStatus,
  type StepState,
  type WorkflowRun,
} from "../runs";
import {
  Badge,
  Button,
  ConfirmDialog,
  PageHeader,
  RelativeTime,
  TextField,
  useMessages,
} from "../ui";
import { StatusBadge, VIA } from "./runs";
import { StepGraph, type GraphStep } from "./step-graph";

export const WORKFLOW_STATUS: Record<WorkflowRun["status"], string> = {
  running: "Running",
  succeeded: "Succeeded",
  failed: "Failed",
  cancelled: "Cancelled",
};

export function stepTone(state: StepState): string {
  switch (state) {
    case "succeeded":
      return "good";
    case "failed":
    case "declined":
      return "bad";
    case "cancelled":
    case "skipped":
    case "waiting":
      return "quiet";
    default:
      return "busy";
  }
}

export function StepBadge({ state }: { state: StepState }) {
  return <span className={`badge badge-${stepTone(state)}`}>{STEP_STATE[state]}</span>;
}

type Step = WorkflowRun["steps"][number];
type StepBatch = NonNullable<Step["batch"]>;

/** Who approves a step, as "an Editor". */
export const APPROVER: Record<NonNullable<Step["approval"]>["role"], string> = {
  viewer: "a Viewer",
  editor: "an Editor",
  runner: "a Runner",
  admin: "an Admin",
};

/** A step waiting for a person to approve it: theirs to decide, if they may. */
function StepApproval({ run, step }: { run: WorkflowRun; step: Step }) {
  const [asking, setAsking] = useState<"approve" | "decline" | null>(null);
  const [note, setNote] = useState("");
  const { say } = useMessages();
  const client = useQueryClient();
  const decide = useApiMutation(
    async (verb: "approve" | "decline") => {
      const given = {
        params: { path: { ref: run.id, step: step.id } },
        body: { note: note.trim() },
      };
      return unwrap(
        verb === "approve"
          ? await iki.POST("/api/v1/workflow-runs/{ref}/steps/{step}/approve", given)
          : await iki.POST("/api/v1/workflow-runs/{ref}/steps/{step}/decline", given),
      );
    },
    [RUNS],
  );
  const close = () => {
    setAsking(null);
    setNote("");
    decide.reset();
  };
  const approver = step.approval ? APPROVER[step.approval.role] : "";
  const titleId = `approval-${step.id}`;
  return (
    <section className="alert alert-warning" aria-labelledby={titleId}>
      <h2 className="alert-title" id={titleId}>
        {step.can.decide
          ? `“${step.label}” waits for your approval`
          : `“${step.label}” waits for approval`}
      </h2>
      {step.approval?.instructions ? (
        <p className="preserve-lines">{step.approval.instructions}</p>
      ) : null}
      <p>
        {step.can.decide
          ? `It starts once you approve it, as ${run.requested_by.name}, who started the workflow.`
          : `It needs ${approver} or above in the project, other than ${run.requested_by.name}, who started the workflow.`}{" "}
        Declining it fails the workflow.
        {step.approval_expires_at ? (
          <>
            {" "}
            If nobody decides, it’s declined <RelativeTime value={step.approval_expires_at} />.
          </>
        ) : null}
      </p>
      {step.can.decide ? (
        <div className="actions">
          <Button
            variant="primary"
            onClick={() => {
              setAsking("approve");
            }}
          >
            Approve
          </Button>
          <Button
            variant="danger"
            onClick={() => {
              setAsking("decline");
            }}
          >
            Decline
          </Button>
        </div>
      ) : null}
      <ConfirmDialog
        isOpen={asking !== null}
        title={asking === "approve" ? `Approve “${step.label}”?` : `Decline “${step.label}”?`}
        confirmLabel={asking === "approve" ? "Approve" : "Decline"}
        cancelLabel="Keep waiting"
        confirmVariant={asking === "approve" ? "primary" : "danger"}
        busy={decide.isPending}
        problem={decide.error ? problemOf(decide.error) : undefined}
        onCancel={close}
        onConfirm={() => {
          if (!asking) return;
          decide.mutate(asking, {
            onSuccess: (saved) => {
              client.setQueryData(workflowRunQuery(saved.hrid).queryKey, saved);
              say(
                asking === "approve"
                  ? `Approved “${step.label}”. It starts now.`
                  : `Declined “${step.label}”. ${saved.hrid} failed.`,
              );
              close();
            },
          });
        }}
      >
        <p>
          {asking === "approve"
            ? `It starts now, as ${run.requested_by.name}. Your approval is recorded with your name and the time.`
            : `It doesn’t run, and ${run.hrid} fails. ${run.requested_by.name} is told why.`}
        </p>
        <TextField
          label={asking === "approve" ? "Note" : "Reason"}
          value={note}
          onChange={setNote}
          maxLength={2000}
          optional={asking === "approve"}
          multiline
          help={
            asking === "approve"
              ? "What you checked, for the record."
              : "Why, for the record and for whoever started the workflow."
          }
        />
      </ConfirmDialog>
    </section>
  );
}

/** "Approved by Runner 3 minutes ago: looks right", for a step someone decided. */
function Decision({ decision }: { decision: NonNullable<Step["decision"]> }) {
  return (
    <span className="row-meta">
      {decision.approved ? "Approved" : "Declined"} by {decision.by.name}{" "}
      <RelativeTime value={decision.at} />
      {decision.note ? <span className="preserve-lines">: {decision.note}</span> : null}
    </span>
  );
}
type Condition = WorkflowRun["steps"][number]["when"][number];

const COMPARED: Record<Condition["operator"], string> = {
  is: "is",
  is_not: "isn’t",
  in: "is one of",
  at_least: "is at least",
  at_most: "is at most",
  above: "is above",
  below: "is below",
};

/** `$steps.align.metrics.score` as `align.score`, `$params.min` as `min`. */
function named(ref: string): string {
  const parts = ref.split(".");
  return parts[0] === "$steps" ? `${parts[1] ?? ""}.${parts[3] ?? ""}` : (parts[1] ?? ref);
}

function Operand({ value }: { value: unknown }) {
  if (Array.isArray(value)) {
    return (
      <>
        {(value as unknown[]).map((v, i) => (
          <span key={i}>
            {i ? ", " : ""}
            <Operand value={v} />
          </span>
        ))}
      </>
    );
  }
  if (typeof value === "string" && value.startsWith("$params.")) {
    return <code>{named(value)}</code>;
  }
  const literal = typeof value === "string" && value.startsWith("$$") ? value.slice(1) : value;
  return <code>{JSON.stringify(literal)}</code>;
}

/** "Runs if align.score is at least min_score": what must hold for a step to run. */
export function When({ conditions }: { conditions: Condition[] }) {
  return (
    <span className="row-meta">
      {"Runs if "}
      {conditions.map((c, i) => (
        <span key={i}>
          {i ? " and " : ""}
          <code>{named(c.value)}</code> {COMPARED[c.operator]} <Operand value={c.operand} />
        </span>
      ))}
    </span>
  );
}

/** Its steps for the graph: how far each has got, linked to its run or batch. */
function graphSteps(run: WorkflowRun): GraphStep[] {
  return run.steps.map((step) => ({
    id: step.id,
    label: step.label,
    after: step.after,
    tone: step.run && step.state === "running" ? tone(step.run.status) : stepTone(step.state),
    meta:
      (step.run && step.state === "running" ? STATUS[step.run.status] : STEP_STATE[step.state]) +
      (step.run ? ` · ${step.run.hrid}` : "") +
      (step.batch ? ` · ${countRuns(step.batch)}` : ""),
    link: step.run
      ? { to: "/runs/$ref/" as const, ref: step.run.hrid }
      : step.batch
        ? { to: "/batches/$ref/" as const, ref: step.batch.hrid }
        : undefined,
  }));
}

/** "3 of 12 runs finished", or "12 runs" once they all have. */
function countRuns(batch: StepBatch): string {
  const ended = Object.entries(batch.statuses)
    .filter(([status]) => finished(status as RunStatus))
    .reduce((sum, [, n]) => sum + n, 0);
  const runs = `${String(batch.size)} run${batch.size === 1 ? "" : "s"}`;
  return ended < batch.size ? `${String(ended)} of ${runs} finished` : runs;
}

function useWorkflowRun(): WorkflowRun {
  const { ref } = useParams({ from: "/workflow-runs/$ref" });
  return useSuspenseQuery(workflowRunQuery(ref)).data;
}

function CancelWorkflow({ run }: { run: WorkflowRun }) {
  const [asking, setAsking] = useState(false);
  const [reason, setReason] = useState("");
  const { say } = useMessages();
  const client = useQueryClient();
  const cancel = useApiMutation(
    async () =>
      unwrap(
        await iki.POST("/api/v1/workflow-runs/{ref}/cancel", {
          params: { path: { ref: run.id } },
          body: { reason: reason.trim() },
        }),
      ),
    [RUNS],
  );
  if (!run.can.cancel) return null;
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
        Cancel
      </Button>
      <ConfirmDialog
        isOpen={asking}
        title={`Cancel ${run.hrid}?`}
        confirmLabel="Cancel it"
        cancelLabel="Keep it"
        confirmVariant="danger"
        busy={cancel.isPending}
        problem={cancel.error ? problemOf(cancel.error) : undefined}
        onCancel={close}
        onConfirm={() => {
          cancel.mutate(undefined, {
            onSuccess: (saved) => {
              client.setQueryData(workflowRunQuery(saved.hrid).queryKey, saved);
              close();
              say(
                saved.status === "running"
                  ? `Cancelling ${saved.hrid}. Workers stop the steps they have.`
                  : `Cancelled ${saved.hrid}.`,
              );
            },
          });
        }}
      >
        <p>
          No further step starts. Steps that haven’t started end now and change nothing; workers
          stop those that are going. Steps that have finished keep their results.
        </p>
        <TextField
          label="Reason"
          value={reason}
          onChange={setReason}
          maxLength={2000}
          optional
          multiline
          help="Why, for the record. It’s kept with the workflow and its runs."
        />
      </ConfirmDialog>
    </div>
  );
}

export function WorkflowRunPage() {
  const run = useWorkflowRun();
  usePageTitle(`${run.label} (${run.hrid})`);
  const project = useQuery(projectsQuery).data?.find((p) => p.id === run.project);
  const client = useQueryClient();
  const going = run.status === "running";
  useWorkflowUpdates(run.hrid, going);

  // When it ends, what its steps changed is read again wherever it shows.
  const wasGoing = useRef(going);
  useEffect(() => {
    if (!going && wasGoing.current) {
      void client.invalidateQueries({ queryKey: ENTITIES });
      void client.invalidateQueries({ queryKey: [...RUNS, "list"] });
    }
    wasGoing.current = going;
  }, [going, client]);

  const ended = run.steps.filter((s) => !["waiting", "running"].includes(s.state)).length;
  const params = Object.entries(run.params);
  return (
    <>
      <PageHeader
        eyebrow={
          project ? (
            <>
              <Link to="/projects/$slug/runs/" params={{ slug: project.slug }}>
                {project.name}
              </Link>
              {" · Workflow"}
            </>
          ) : (
            "Workflow"
          )
        }
        title={run.label}
        badge={<Badge>{run.hrid}</Badge>}
        actions={<CancelWorkflow run={run} />}
      />

      <section className="card" aria-labelledby="workflow-summary-title">
        <h2 className="section-title" id="workflow-summary-title">
          {WORKFLOW_STATUS[run.status]}
          {run.cancel_requested_at && going ? ": cancelling" : ""}
        </h2>
        <div className="run-progress">
          <progress value={ended} max={run.steps.length} aria-label={`${run.hrid} progress`} />
          <span>
            {going ? `${String(ended)} of ${String(run.steps.length)} steps finished` : "Finished"}
            {!going && run.finished_at ? (
              <>
                {" "}
                <RelativeTime value={run.finished_at} />
              </>
            ) : null}
          </span>
        </div>
        {run.error ? (
          <div className="alert alert-error" role="alert">
            <p>{run.error.message}</p>
          </div>
        ) : null}
        <dl className="summary">
          <dt>Started</dt>
          <dd>
            By {run.requested_by.name} <RelativeTime value={run.created_at} />{" "}
            {VIA[run.via] ?? run.via}
            <Through agent={run.agent} />
          </dd>
          <AgentRows agent={run.agent} />
          {run.reason ? (
            <>
              <dt>Reason</dt>
              <dd className="preserve-lines">{run.reason}</dd>
            </>
          ) : null}
          <dt>Workflow</dt>
          <dd>
            <code>{run.workflow}</code> {run.plugin_version}
          </dd>
          {run.inputs.length ? (
            <>
              <dt>Inputs</dt>
              <dd>
                <ul className="value-list" aria-label="Inputs">
                  {run.inputs.map((i) => (
                    <li key={`${i.name}-${i.entity}`}>
                      {i.name}:{" "}
                      <Link to="/entities/$ref/" params={{ ref: i.hrid }}>
                        {i.hrid}
                      </Link>
                    </li>
                  ))}
                </ul>
              </dd>
            </>
          ) : null}
          {params.length ? (
            <>
              <dt>Params</dt>
              <dd>
                <ul className="value-list" aria-label="Params">
                  {params.map(([name, value]) => (
                    <li key={name}>
                      {name}: <code>{JSON.stringify(value)}</code>
                    </li>
                  ))}
                </ul>
              </dd>
            </>
          ) : null}
        </dl>
      </section>

      {run.steps
        .filter((s) => s.state === "awaiting_approval")
        .map((s) => (
          <StepApproval key={s.id} run={run} step={s} />
        ))}

      <section aria-labelledby="workflow-steps-title">
        <h2 className="section-title" id="workflow-steps-title">
          Steps
        </h2>
        {run.steps.length > 1 ? (
          <StepGraph
            steps={graphSteps(run)}
            caption={`${String(run.steps.length)} steps, each right of the steps it waits for. Point at one to see what it waits for and what waits for it.`}
          />
        ) : null}
        <ol className="rows" aria-label="Steps in order">
          {run.steps.map((step) => (
            <li className="row" key={step.id}>
              <div className="row-main">
                {step.run ? (
                  <Link className="row-title" to="/runs/$ref/" params={{ ref: step.run.hrid }}>
                    {step.label}
                  </Link>
                ) : step.batch ? (
                  <Link className="row-title" to="/batches/$ref/" params={{ ref: step.batch.hrid }}>
                    {step.label}
                  </Link>
                ) : (
                  <span className="row-title">{step.label}</span>
                )}
                <span className="row-meta">
                  <code>{step.action}</code>
                  {step.each ? ` for each ${step.each}` : ""}
                  {step.run ? ` · ${step.run.hrid}` : ""}
                  {step.batch ? ` · ${step.batch.hrid}, ${countRuns(step.batch)}` : ""}
                  {step.after.length ? ` · after ${step.after.join(", ")}` : ""}
                  {step.finished_at ? (
                    <>
                      {" · ended "}
                      <RelativeTime value={step.finished_at} />
                    </>
                  ) : null}
                </span>
                {step.when.length ? <When conditions={step.when} /> : null}
                {step.approval && !step.decision ? (
                  <span className="row-meta">
                    Needs approval by {APPROVER[step.approval.role]} or above
                  </span>
                ) : null}
                {step.decision ? <Decision decision={step.decision} /> : null}
                {step.error ? <p className="row-reason">{step.error.message}</p> : null}
              </div>
              <div className="row-tags">
                {step.run && step.state === "running" ? (
                  <StatusBadge status={step.run.status} />
                ) : (
                  <StepBadge state={step.state} />
                )}
              </div>
            </li>
          ))}
        </ol>
      </section>
    </>
  );
}
