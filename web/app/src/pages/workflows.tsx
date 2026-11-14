/**
 * A project's workflows (ADR 0079): its workflow runs, newest first, and a form to start
 * one, as GitHub's Actions tab lists runs and runs a workflow by hand.
 */

import { unwrap } from "@ikisci/sdk";
import { useInfiniteQuery, useQuery, useSuspenseQuery } from "@tanstack/react-query";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { useState, type SubmitEvent } from "react";

import { iki, problemOf, useApiMutation, type Project } from "../api";
import { inSentence, typesQuery, type EntityType } from "../entities";
import { FieldInput } from "../entity-inputs";
import {
  blankMessage,
  encode,
  initialDraft,
  isBlank,
  type Draft,
  type Schemaed,
} from "../entity-values";
import { useForm } from "../forms";
import { usePageTitle } from "../layout";
import {
  actionsQuery,
  projectWorkflowRunsQuery,
  RUNS,
  workflowsQuery,
  type Action,
  type Workflow,
  type WorkflowRun,
  type WorkflowStatus,
} from "../runs";
import {
  Button,
  buttonClass,
  ConfirmDialog,
  Loading,
  ProblemAlert,
  RelativeTime,
  SelectField,
  TextField,
  useMessages,
} from "../ui";
import { inputField, listed, NO_RULES } from "./new-run";
import { useProject } from "./project-overview";
import { StepGraph } from "./step-graph";
import { APPROVER, When, WORKFLOW_STATUS } from "./workflow-runs";

/** How often a list with workflow runs still going is read again. */
const LIST_REFRESH_MS = 5_000;

const STATUS_CHOICES = [
  { value: "all", label: "Every status" },
  ...Object.entries(WORKFLOW_STATUS).map(([value, label]) => ({ value, label })),
];

function statusTone(status: WorkflowStatus): string {
  switch (status) {
    case "succeeded":
      return "good";
    case "failed":
      return "bad";
    case "cancelled":
      return "quiet";
    default:
      return "busy";
  }
}

/** The role a workflow's steps need to start it: Runner for containers and machines. */
function needs(workflow: Workflow): string {
  return workflow.tiers.some((t) => t === "container" || t === "vm") ? "Runner" : "Editor";
}

function startable(project: Project, workflow: Workflow): boolean {
  return workflow.available && workflow.tiers.every((t) => project.can.run.includes(t));
}

/** "2 of 3 steps finished", and whether a step waits for someone to approve it. */
function progress(run: WorkflowRun): string {
  const ended = run.steps.filter(
    (s) => !["waiting", "awaiting_approval", "running"].includes(s.state),
  );
  const waiting = run.steps.some((s) => s.state === "awaiting_approval");
  if (run.status !== "running") return `${String(run.steps.length)} steps`;
  return (
    `${String(ended.length)} of ${String(run.steps.length)} steps finished` +
    (waiting ? " · waiting for approval" : "")
  );
}

// --- The project's workflow runs ----------------------------------------------------------

export function ProjectWorkflows() {
  const project = useProject();
  usePageTitle(`Workflows · ${project.name}`);
  const { status } = useSearch({ from: "/projects/$slug/workflows" });
  const navigate = useNavigate();
  const runs = useInfiniteQuery({
    ...projectWorkflowRunsQuery(project.id, status),
    refetchInterval: (query) =>
      query.state.data?.pages.some((p) => p.items.some((r) => r.status === "running"))
        ? LIST_REFRESH_MS
        : false,
  });
  const items = runs.data?.pages.flatMap((p) => p.items) ?? [];
  return (
    <>
      <div className="toolbar">
        <SelectField
          label="Status"
          compact
          value={status ?? "all"}
          options={STATUS_CHOICES}
          onChange={(value) => {
            void navigate({
              to: "/projects/$slug/workflows/",
              params: { slug: project.slug },
              search: value === "all" ? {} : { status: value as WorkflowStatus },
            });
          }}
        />
        {project.can.run.length ? (
          <Link
            to="/projects/$slug/workflows/new/"
            params={{ slug: project.slug }}
            className={`${buttonClass("primary")} toolbar-end`}
          >
            Run a workflow
          </Link>
        ) : null}
      </div>
      {runs.isPending ? <Loading label="Loading workflow runs…" /> : null}
      {runs.error ? <ProblemAlert problem={problemOf(runs.error)} /> : null}
      {runs.isSuccess && items.length === 0 ? (
        <p className="muted empty">
          {status
            ? `No workflow runs are ${WORKFLOW_STATUS[status].toLowerCase()}.`
            : "No workflow runs yet. A workflow runs several actions as one, each step once the steps it takes from have succeeded."}
        </p>
      ) : null}
      {items.length ? (
        <ul className="rows" aria-label="Workflow runs">
          {items.map((run) => (
            <li className="row" key={run.id}>
              <div className="row-main">
                <Link className="row-title" to="/workflow-runs/$ref/" params={{ ref: run.hrid }}>
                  {run.label}
                </Link>
                <span className="row-meta">
                  {run.hrid} · {progress(run)} · {run.requested_by.name} ·{" "}
                  <RelativeTime value={run.created_at} />
                  {run.reason ? ` · “${run.reason}”` : ""}
                </span>
              </div>
              <div className="row-tags">
                <span className={`badge badge-${statusTone(run.status)}`}>
                  {WORKFLOW_STATUS[run.status]}
                </span>
              </div>
            </li>
          ))}
        </ul>
      ) : null}
      {runs.hasNextPage ? (
        <div className="actions">
          <Button
            busy={runs.isFetchingNextPage}
            busyText="Loading…"
            onClick={() => void runs.fetchNextPage()}
          >
            Show older
          </Button>
        </div>
      ) : null}
    </>
  );
}

// --- Starting one -------------------------------------------------------------------------

interface Start {
  inputs: Record<string, string | string[]>;
  params: Record<string, unknown>;
  /** Link inputs from other projects into this one first (ADR 0066). */
  addToProject: boolean;
}

export function NewWorkflowRun() {
  const project = useProject();
  const search = useSearch({ from: "/projects/$slug/workflows/new" });
  const navigate = useNavigate({ from: "/projects/$slug/workflows/new" });
  const types = useSuspenseQuery(typesQuery).data;
  const workflows = useQuery(workflowsQuery).data;
  const actions = useQuery(actionsQuery).data;
  usePageTitle(`Run a workflow · ${project.name}`);

  if (project.can.run.length === 0) {
    return (
      <section className="card form-card" aria-labelledby="new-workflow-title">
        <h2 className="section-title" id="new-workflow-title">
          Run a workflow
        </h2>
        <p>
          {project.archived
            ? `${project.name} is archived, so nothing new can run in it.`
            : `You can’t run workflows in ${project.name}: its steps run as you, so starting one takes the role each step needs. A project Admin can change your role.`}
        </p>
      </section>
    );
  }
  if (!workflows || !actions) return <Loading label="Loading workflows…" />;
  const runnable = workflows.filter((w) => startable(project, w));
  const others = workflows.filter((w) => !startable(project, w));
  const workflow = runnable.find((w) => w.name === search.workflow);
  return (
    <section className="card form-card" aria-labelledby="new-workflow-title">
      <h2 className="section-title" id="new-workflow-title">
        Run a workflow
      </h2>
      <p>
        In {project.name}: each step is a run you start, recorded with who started it and why, and
        what it makes belongs to {project.name}.
      </p>
      {runnable.length === 0 ? (
        <p className="empty">No workflow you can run here is installed.</p>
      ) : (
        <SelectField
          label="Workflow"
          value={workflow?.name ?? ""}
          options={[
            { value: "", label: "Choose a workflow" },
            ...runnable.map((w) => ({ value: w.name, label: w.label || w.name })),
          ]}
          onChange={(value) => {
            void navigate({
              search: (prev) => ({ ...prev, workflow: value || undefined }),
              replace: true,
            });
          }}
        />
      )}
      {others.length ? (
        <details className="disclosure">
          <summary>
            {others.length} more {others.length === 1 ? "workflow" : "workflows"} you can’t run here
          </summary>
          <ul>
            {others.map((w) => (
              <li key={w.name}>
                {w.label || w.name}:{" "}
                {w.available
                  ? `its steps need the ${needs(w)} role in ${project.name}.`
                  : (w.unavailable_reason ?? "it can’t run on this deployment.")}
              </li>
            ))}
          </ul>
        </details>
      ) : null}
      {workflow ? (
        <WorkflowForm
          key={workflow.name}
          project={project}
          workflow={workflow}
          actions={actions}
          types={types}
        />
      ) : (
        <div className="actions">
          <Link
            to="/projects/$slug/workflows/"
            params={{ slug: project.slug }}
            className={buttonClass("quiet")}
          >
            Cancel
          </Link>
        </div>
      )}
    </section>
  );
}

/** Its steps in the order they start: what each runs, after what, and when. */
function Steps({ workflow, actions }: { workflow: Workflow; actions: Action[] }) {
  return (
    <ol className="rows" aria-label="Steps">
      {workflow.steps.map((step) => (
        <li className="row" key={step.id}>
          <div className="row-main">
            <span className="row-title">{step.label}</span>
            <span className="row-meta">
              {actions.find((a) => a.name === step.action)?.label ?? step.action}
              {step.each ? ` for each ${step.each}` : ""}
              {step.after.length ? ` · after ${step.after.join(", ")}` : ""}
            </span>
            {step.when.length ? <When conditions={step.when} /> : null}
            {step.approval ? (
              <span className="row-meta">
                Waits for approval by {APPROVER[step.approval.role]} or above, other than you
              </span>
            ) : null}
          </div>
        </li>
      ))}
    </ol>
  );
}

function WorkflowForm({
  project,
  workflow,
  actions,
  types,
}: {
  project: Project;
  workflow: Workflow;
  actions: Action[];
  types: EntityType[];
}) {
  const navigate = useNavigate();
  const { say } = useMessages();
  const params: Schemaed = { values_schema: workflow.params_schema };
  const [drafts, setDrafts] = useState<Record<string, Draft>>(() => ({
    ...Object.fromEntries(
      workflow.inputs.map((i) => [`inputs/${i.name}`, i.cardinality === "many" ? [] : null]),
    ),
    ...Object.fromEntries(
      workflow.params.map((p) => [`params/${p.name}`, initialDraft(p, params)]),
    ),
  }));
  const [reason, setReason] = useState("");
  const launch = useApiMutation(
    async (body: Start) =>
      unwrap(
        await iki.POST("/api/v1/workflows/{name}/runs", {
          params: { path: { name: workflow.name } },
          body: {
            project: project.id,
            inputs: body.inputs,
            params: body.params,
            reason: reason.trim(),
            add_to_project: body.addToProject,
          },
        }),
      ),
    [RUNS],
  );
  const form = useForm(launch.error);
  // A start refused for inputs from other projects, to send again adding them.
  const [outside, setOutside] = useState<{ body: Start; hrids: string[] } | null>(null);

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    const errors: Record<string, string> = {};
    const inputs: Record<string, string | string[]> = {};
    for (const input of workflow.inputs) {
      const key = `inputs/${input.name}`;
      const field = inputField(input);
      const draft = drafts[key] ?? null;
      if (isBlank(field, draft)) {
        if (!input.optional) errors[key] = `Choose ${inSentence(input.label)}.`;
        continue;
      }
      const encoded = encode(field, draft, NO_RULES);
      if (encoded.ok) inputs[input.name] = encoded.value as string | string[];
      else errors[key] = encoded.error;
    }
    const values: Record<string, unknown> = {};
    for (const param of workflow.params) {
      const key = `params/${param.name}`;
      const draft = drafts[key] ?? null;
      if (isBlank(param, draft)) {
        if (param.required && param.default === null) errors[key] = blankMessage(param);
        continue;
      }
      const encoded = encode(param, draft, params);
      if (encoded.ok) values[param.name] = encoded.value;
      else errors[key] = encoded.error;
    }
    if (!form.validate(errors)) return;
    start({ inputs, params: values, addToProject: false });
  }

  function start(body: Start) {
    launch.mutate(body, {
      onError: (error) => {
        const problem = problemOf(error);
        if (problem.code === "inputs_outside_project") {
          setOutside({ body, hrids: (problem.entities ?? []).map((e) => e.hrid) });
        }
      },
      onSuccess: (made) => {
        setOutside(null);
        void navigate({ to: "/workflow-runs/$ref/", params: { ref: made.hrid } }).then(() => {
          say(`Started ${made.hrid}.`);
        });
      },
    });
  }

  const count = outside?.hrids.length ?? 0;
  return (
    <form {...form.props} onSubmit={submit}>
      {form.alert}
      {workflow.description ? <p>{workflow.description}</p> : null}
      <fieldset className="field">
        <legend>Steps</legend>
        <p className="field-help">
          Each starts once the steps it takes from have succeeded. If one fails, no further step
          starts.
        </p>
        {workflow.steps.length > 1 ? (
          <StepGraph
            steps={workflow.steps.map((step) => ({
              id: step.id,
              label: step.label,
              after: step.after,
              meta: actions.find((a) => a.name === step.action)?.label ?? step.action,
            }))}
            caption="Each step is right of the steps it waits for."
          />
        ) : null}
        <Steps workflow={workflow} actions={actions} />
      </fieldset>
      {workflow.inputs.map((input) => (
        <FieldInput
          key={input.name}
          field={inputField(input)}
          type={NO_RULES}
          types={types}
          draft={drafts[`inputs/${input.name}`] ?? null}
          onChange={(draft) => {
            setDrafts((d) => ({ ...d, [`inputs/${input.name}`]: draft }));
          }}
          error={form.errors[`inputs/${input.name}`]}
          projectId={project.id}
        />
      ))}
      {workflow.params.map((param) => (
        <FieldInput
          key={param.name}
          field={param}
          type={params}
          types={types}
          draft={drafts[`params/${param.name}`] ?? null}
          onChange={(draft) => {
            setDrafts((d) => ({ ...d, [`params/${param.name}`]: draft }));
          }}
          error={form.errors[`params/${param.name}`]}
          projectId={project.id}
        />
      ))}
      <TextField
        label="Reason"
        value={reason}
        onChange={setReason}
        error={form.errors["reason"]}
        maxLength={2000}
        optional
        multiline
        help="Why, for the record. It’s kept with the workflow and each of its runs."
      />
      <div className="actions">
        <Button type="submit" variant="primary" busy={launch.isPending} busyText="Starting…">
          Start workflow
        </Button>
        <Link
          to="/projects/$slug/workflows/"
          params={{ slug: project.slug }}
          className={buttonClass("quiet")}
        >
          Cancel
        </Link>
      </div>
      <ConfirmDialog
        isOpen={outside !== null}
        title={`Add to ${project.name}?`}
        confirmLabel="Add and start workflow"
        confirmVariant="primary"
        busy={launch.isPending}
        problem={
          launch.error && problemOf(launch.error).code !== "inputs_outside_project"
            ? problemOf(launch.error)
            : undefined
        }
        onCancel={() => {
          setOutside(null);
        }}
        onConfirm={() => {
          if (outside) start({ ...outside.body, addToProject: true });
        }}
      >
        <p>
          {listed(outside?.hrids ?? [])} {count > 1 ? "are" : "is"} in other projects. Its steps
          read only what’s in its project, so {count > 1 ? "they’ll" : "it’ll"} be added to{" "}
          {project.name} first, and its members will be able to read {count > 1 ? "them" : "it"}.
        </p>
        <p>Adding is recorded in each project’s audit log.</p>
      </ConfirmDialog>
    </form>
  );
}
