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
  RUNS,
  STEP_STATE,
  useWorkflowUpdates,
  workflowRunQuery,
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

const WORKFLOW_STATUS: Record<WorkflowRun["status"], string> = {
  running: "Running",
  succeeded: "Succeeded",
  failed: "Failed",
  cancelled: "Cancelled",
};

function stepTone(state: StepState): string {
  switch (state) {
    case "succeeded":
      return "good";
    case "failed":
      return "bad";
    case "cancelled":
    case "skipped":
    case "waiting":
      return "quiet";
    default:
      return "busy";
  }
}

function StepBadge({ state }: { state: StepState }) {
  return <span className={`badge badge-${stepTone(state)}`}>{STEP_STATE[state]}</span>;
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

      <section aria-labelledby="workflow-steps-title">
        <h2 className="section-title" id="workflow-steps-title">
          Steps
        </h2>
        <ol className="rows">
          {run.steps.map((step) => (
            <li className="row" key={step.id}>
              <div className="row-main">
                {step.run ? (
                  <Link className="row-title" to="/runs/$ref/" params={{ ref: step.run.hrid }}>
                    {step.label}
                  </Link>
                ) : (
                  <span className="row-title">{step.label}</span>
                )}
                <span className="row-meta">
                  <code>{step.action}</code>
                  {step.run ? ` · ${step.run.hrid}` : ""}
                  {step.after.length ? ` · after ${step.after.join(", ")}` : ""}
                  {step.finished_at ? (
                    <>
                      {" · ended "}
                      <RelativeTime value={step.finished_at} />
                    </>
                  ) : null}
                </span>
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
