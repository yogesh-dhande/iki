/**
 * Runs: a project's list of them, and a run's page. The page follows a run
 * while it's going: its status, each attempt's progress, and what its command
 * prints, as it prints it.
 */

import { unwrap } from "@ikisci/sdk";
import {
  useInfiniteQuery,
  useQuery,
  useQueryClient,
  useSuspenseQuery,
} from "@tanstack/react-query";
import { Link, useNavigate, useParams, useSearch } from "@tanstack/react-router";
import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";

import { AgentRows, Through } from "../agent";
import {
  fieldErrors,
  iki,
  meQuery,
  problemOf,
  projectByIdQuery,
  useApiMutation,
  type Project,
} from "../api";
import { capacityText, slurmRequestText, type AttemptSlurm } from "../compute";
import { machineTime, money } from "../costs";
import { ENTITIES, formatSize, typeLabel, typesQuery } from "../entities";
import { usePageTitle } from "../layout";
import { useReadableProjects } from "../lists";
import { useMedia } from "../media";
import { PreviewButton } from "../previews/preview-places";
import { consentFor, destinations, SendsOutside, type Statement } from "../outside-apis";
import {
  actionLabel,
  actionsQuery,
  artifactDownloadUrl,
  artifactsQuery,
  ATTEMPT_STATE,
  attemptsQuery,
  finished,
  LOG_KEPT_BYTES,
  logDownloadUrl,
  moreEffectsQuery,
  projectRunsQuery,
  readLog,
  RUNS,
  runQuery,
  STATUS,
  tone,
  useRunUpdates,
  type Artifact,
  type Attempt,
  type Run,
  type RunStatus,
} from "../runs";
import {
  Badge,
  Button,
  buttonClass,
  ConfirmDialog,
  Loading,
  PageHeader,
  ProblemAlert,
  RelativeTime,
  TextField,
  Time,
  useMessages,
  useNow,
} from "../ui";
import { ToolbarSelect } from "./entity-filters";
import { useProject } from "./project-overview";
import { ParkedNote, RunOutsideApis } from "./run-outside-apis";

export const VIA: Record<string, string> = {
  web: "in the web app",
  api: "through the API",
  mcp: "by an AI assistant (MCP)",
  cli: "from the command line",
  trigger: "by a trigger",
  system: "by Iki",
};

const EFFECT: Record<Run["effects"][number]["kind"], string> = {
  created: "Created",
  updated: "Updated",
  reused: "Already existed",
};

type Effect = Run["effects"][number];

function counted(n: number, one: string, many: string) {
  return `${n.toLocaleString()} ${n === 1 ? one : many}`;
}

/** What the run created, updated and reused: its answer lists the first 100, and the
 * rest come a page at a time (an import may make millions). */
function RunEffects({ run }: { run: Run }) {
  const types = useQuery(typesQuery).data;
  const [more, setMore] = useState(false);
  const rest = useInfiniteQuery({
    ...moreEffectsQuery(run.hrid, run.effects_next_cursor ?? ""),
    enabled: more && run.effects_next_cursor !== null,
  });
  const { created, updated, reused } = run.effect_counts;
  const total = created + updated + reused;
  if (run.effects.length === 0) {
    return <p className="muted">It changed no entities.</p>;
  }
  const listed: Effect[] = [...run.effects, ...(rest.data?.pages.flatMap((p) => p.items) ?? [])];
  const parts = [
    created ? `created ${counted(created, "entity", "entities")}` : "",
    updated ? `updated ${counted(updated, "entity", "entities")}` : "",
    reused ? `found ${counted(reused, "that already existed", "that already existed")}` : "",
  ].filter(Boolean);
  const hasMore = run.effects_next_cursor !== null && (!more || rest.hasNextPage);
  return (
    <>
      {total > run.effects.length ? (
        <p>
          It {parts.join(", ").replace(/, ([^,]*)$/, " and $1")}. Showing{" "}
          {listed.length.toLocaleString()}.
        </p>
      ) : null}
      <ul className="value-list" aria-label="Entities it changed">
        {listed.map((effect) => (
          <li key={effect.entity}>
            {EFFECT[effect.kind]}{" "}
            <Link to="/entities/$ref/" params={{ ref: effect.hrid }}>
              {effect.hrid}
            </Link>{" "}
            <span className="muted">
              {typeLabel(types, effect.type)}
              {effect.revision !== null ? `, revision ${String(effect.revision)}` : ""}
            </span>
          </li>
        ))}
      </ul>
      {rest.error ? <ProblemAlert problem={problemOf(rest.error)} /> : null}
      {hasMore ? (
        <div className="actions">
          <Button
            busy={rest.isFetching}
            busyText="Loading…"
            onClick={() => {
              if (more) void rest.fetchNextPage();
              else setMore(true);
            }}
          >
            Show more
          </Button>
        </div>
      ) : null}
    </>
  );
}

export function StatusBadge({ status }: { status: RunStatus | Attempt["state"] }) {
  const label =
    status in STATUS ? STATUS[status as RunStatus] : ATTEMPT_STATE[status as Attempt["state"]];
  return <span className={`badge badge-${tone(status)}`}>{label}</span>;
}

// --- A project's runs ------------------------------------------------------------------

/** A metric's number, rounded for reading: the run keeps it unrounded, so a workflow's
 * threshold compares its true value. */
const metricNumber = new Intl.NumberFormat(undefined, { maximumFractionDigits: 3 });

const STATUS_CHOICES = [
  { value: "all", label: "Any" },
  ...Object.entries(STATUS).map(([value, label]) => ({ value, label })),
] as const;

/** Why a self-hosted run has no machine to take it (ADR 0112), and who can fix that. */
function NeedsWorker({ why, labels }: { why: NonNullable<Run["needs_worker"]>; labels: string[] }) {
  const me = useSuspenseQuery(meQuery).data;
  const said = {
    unregistered: labels.length
      ? `Waiting for a self-hosted worker: no machine registered for this project has the labels ${labels.join(", ")}. A Compute admin registers one for its team, or gives one those labels.`
      : "Waiting for a self-hosted worker: no machine is registered for this project. A Compute admin registers one for its team.",
    offline:
      "Waiting for a self-hosted worker: the machines registered for this project are offline. It starts when one of them is back.",
    busy: "Waiting for a self-hosted worker: the machines registered for this project are busy with other runs. It starts when one is free.",
  }[why];
  return (
    <div className="alert alert-info" role="status">
      <p>{said}</p>
      {me.can.manage_compute && why === "unregistered" ? (
        <p>
          <Link to="/admin/compute/workers/">Make a self-hosted registration token</Link> for this
          project’s team, and register a machine with it.
        </p>
      ) : null}
    </div>
  );
}

/** While some run listed is going, the list reads itself again this often. */
export const LIST_REFRESH_MS = 10_000;

export function ProjectRuns() {
  const project = useProject();
  usePageTitle(`Runs · ${project.name}`);
  const { status } = useSearch({ from: "/projects/$slug/runs" });
  const navigate = useNavigate();
  return (
    <RunList
      project={project}
      status={status}
      onStatus={(value) => {
        void navigate({
          to: "/projects/$slug/runs/",
          params: { slug: project.slug },
          search: value ? { status: value } : {},
        });
      }}
      actions={
        project.can.run.length ? (
          <Link
            to="/projects/$slug/runs/new/"
            params={{ slug: project.slug }}
            className={buttonClass("primary")}
          >
            New run
          </Link>
        ) : null
      }
    />
  );
}

/** Every run its reader can read, across their projects (ADR 0176). */
export function AllRuns() {
  usePageTitle("Runs");
  const search = useSearch({ from: "/runs" });
  const navigate = useNavigate({ from: "/runs" });
  const { projects } = useReadableProjects();
  const chosen = projects.find((p) => p.slug === search.project);
  return (
    <>
      <PageHeader
        title="Runs"
        description="Every run in the projects you belong to, newest first. Runs in projects you aren’t in never appear."
      />
      <RunList
        project={chosen}
        across={{
          projects,
          slug: search.project,
          onProject: (slug) => {
            void navigate({ search: (prev) => ({ ...prev, project: slug || undefined }) });
          },
        }}
        status={search.status}
        onStatus={(value) => {
          void navigate({ search: (prev) => ({ ...prev, status: value }) });
        }}
      />
    </>
  );
}

/** A list of runs, newest first: a project's, or across projects every one its reader can
 * read, with a Project filter and column. One table for both (ADR 0176). */
function RunList({
  project,
  across,
  status,
  onStatus,
  actions,
}: {
  project: Project | undefined;
  /** Across projects: those the reader can read, the one picked, and picking another. */
  across?: { projects: Project[]; slug: string | undefined; onProject: (slug: string) => void };
  status: RunStatus | undefined;
  onStatus: (status: RunStatus | undefined) => void;
  actions?: React.ReactNode;
}) {
  const actionsList = useQuery(actionsQuery).data;
  const unknown = Boolean(across?.slug && !project);
  const runs = useInfiniteQuery({
    ...projectRunsQuery(project?.id, status),
    enabled: !unknown,
    refetchInterval: (query) =>
      query.state.data?.pages.some((p) => p.items.some((r) => !finished(r.status)))
        ? LIST_REFRESH_MS
        : false,
  });
  const items = runs.data?.pages.flatMap((p) => p.items) ?? [];
  const names = new Map((across?.projects ?? []).map((p) => [p.id, p]));
  const captionId = useId();
  return (
    <>
      <div className="list-toolbar">
        {across ? (
          <ToolbarSelect
            label="Project"
            value={across.slug ?? ""}
            options={[
              { value: "", label: "Any" },
              ...(across.slug && !project ? [{ value: across.slug, label: across.slug }] : []),
              ...across.projects.map((p) => ({ value: p.slug, label: p.name })),
            ]}
            onChange={across.onProject}
          />
        ) : null}
        <ToolbarSelect
          label="Status"
          value={status ?? "all"}
          options={STATUS_CHOICES}
          onChange={(value) => {
            onStatus(value === "all" ? undefined : (value as RunStatus));
          }}
        />
        {actions ? <div className="list-toolbar-end">{actions}</div> : null}
      </div>
      {unknown ? <p className="empty">There’s no project {across?.slug} you can read.</p> : null}
      {runs.isPending && !unknown ? <Loading label="Loading runs…" /> : null}
      {runs.error ? <ProblemAlert problem={problemOf(runs.error)} /> : null}
      {runs.isSuccess && items.length === 0 ? (
        <p className="muted empty">
          {status
            ? `No runs are ${STATUS[status].toLowerCase()}.`
            : project
              ? `No runs yet. When someone runs an action in ${across ? project.name : "this project"}, it’s listed here.`
              : "No runs you can read yet. When someone runs an action in one of your projects, it’s listed here."}
        </p>
      ) : null}
      {items.length ? (
        <div className="list-table-box">
          <table className="list-table" aria-labelledby={captionId}>
            <caption id={captionId} className="visually-hidden">
              {project ? `Runs in ${project.name}` : "Runs you can read"}
            </caption>
            <thead>
              <tr>
                <th scope="col">ID</th>
                <th scope="col" className="list-fill">
                  Action
                </th>
                {across && !project ? <th scope="col">Project</th> : null}
                <th scope="col">Status</th>
                <th scope="col">Asked by</th>
                <th scope="col" className="list-table-end">
                  Started
                </th>
              </tr>
            </thead>
            <tbody>
              {items.map((run) => (
                <tr key={run.id}>
                  <td className="list-id">
                    <span className="grid-id">{run.hrid}</span>
                    {run.batch ? (
                      <span className="cell-meta">
                        in{" "}
                        <Link to="/batches/$ref/" params={{ ref: run.batch.hrid }}>
                          {run.batch.hrid}
                        </Link>
                      </span>
                    ) : null}
                    {run.workflow ? (
                      <span className="cell-meta">
                        in{" "}
                        <Link to="/workflow-runs/$ref/" params={{ ref: run.workflow.hrid }}>
                          {run.workflow.hrid}
                        </Link>
                      </span>
                    ) : null}
                  </td>
                  {/* The room that's left: a long reason is cut short, and whole when
                      pointed at. */}
                  <td
                    className="list-fill"
                    title={
                      run.reason
                        ? `${actionLabel(actionsList, run.action)}: “${run.reason}”`
                        : undefined
                    }
                  >
                    <span className="list-fill-text">
                      <Link className="list-name" to="/runs/$ref/" params={{ ref: run.hrid }}>
                        {actionLabel(actionsList, run.action)}
                      </Link>
                      {run.reason ? <span className="cell-meta">“{run.reason}”</span> : null}
                    </span>
                  </td>
                  {across && !project ? (
                    <td className="muted">{names.get(run.project)?.name ?? ""}</td>
                  ) : null}
                  <td>
                    <StatusBadge status={run.status} />
                  </td>
                  <td>{run.requested_by.name}</td>
                  <td className="list-table-end muted">
                    <RelativeTime value={run.created_at} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
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

// --- A run's page --------------------------------------------------------------------

function useRun(): Run {
  const { ref } = useParams({ from: "/runs/$ref" });
  return useSuspenseQuery(runQuery(ref)).data;
}

function CancelRun({ run }: { run: Run }) {
  const [asking, setAsking] = useState(false);
  const [reason, setReason] = useState("");
  const { say } = useMessages();
  const client = useQueryClient();
  const cancel = useApiMutation(
    async () =>
      unwrap(
        await iki.POST("/api/v1/runs/{ref}/cancel", {
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
  const running = run.status === "running";
  return (
    <div className="actions">
      <Button
        variant="danger"
        onClick={() => {
          setAsking(true);
        }}
      >
        Cancel run
      </Button>
      <ConfirmDialog
        isOpen={asking}
        title={`Cancel ${run.hrid}?`}
        confirmLabel="Cancel run"
        cancelLabel="Keep it"
        confirmVariant="danger"
        busy={cancel.isPending}
        problem={cancel.error ? problemOf(cancel.error) : undefined}
        onCancel={close}
        onConfirm={() => {
          cancel.mutate(undefined, {
            onSuccess: (saved) => {
              client.setQueryData(runQuery(saved.hrid).queryKey, saved);
              close();
              say(
                saved.status === "cancelled"
                  ? `Cancelled ${saved.hrid}.`
                  : `Asked ${saved.hrid} to stop. Its worker ends it.`,
              );
            },
          });
        }}
      >
        <p>
          {running
            ? "Its worker stops the command. Whatever it hasn’t saved yet is lost, and it changes no entities."
            : "It hasn’t started, so it ends now and changes nothing."}
        </p>
        <TextField
          label="Reason"
          value={reason}
          onChange={setReason}
          maxLength={2000}
          optional
          multiline
          help="Why, for the record. It’s kept with the run."
        />
      </ConfirmDialog>
    </div>
  );
}

/** The run a reproduction of `run` reproduces: a run that reused or reproduced another
 * stands for it. */
function reproducible(run: Run): { id: string; hrid: string } {
  return run.reused_from ?? run.reproduces ?? { id: run.id, hrid: run.hrid };
}

/** What reproducing `run` sends outside Iki: what was agreed to for it, else where it went. */
function sends(run: Run): Statement[] {
  if (run.consent?.statements.length) return run.consent.statements;
  return run.apis.map((a) => ({
    ...a,
    inputs: {},
    params: [],
    statement: `This sends your data to ${a.label} (${a.host}). ${a.purpose}`,
  }));
}

function ReproduceRun({ run }: { run: Run }) {
  const [asking, setAsking] = useState(false);
  const [reason, setReason] = useState("");
  const [agreed, setAgreed] = useState(false);
  const [unagreed, setUnagreed] = useState(false);
  const { say } = useMessages();
  const navigate = useNavigate();
  const original = reproducible(run);
  const statements = sends(run);
  const reproduce = useApiMutation(
    async () =>
      unwrap(
        await iki.POST("/api/v1/runs/{ref}/reproduce", {
          params: { path: { ref: original.id } },
          body: { reason: reason.trim(), consent: agreed ? consentFor(statements) : [] },
        }),
      ),
    [RUNS],
  );
  if (!run.can.reproduce) return null;
  const close = () => {
    setAsking(false);
    setReason("");
    setAgreed(false);
    setUnagreed(false);
    reproduce.reset();
  };
  return (
    <div className="actions">
      <Button
        onClick={() => {
          setAsking(true);
        }}
      >
        Reproduce exactly
      </Button>
      <ConfirmDialog
        isOpen={asking}
        title={`Reproduce ${original.hrid} exactly?`}
        confirmLabel="Reproduce"
        cancelLabel="Don’t"
        busy={reproduce.isPending}
        problem={reproduce.error ? problemOf(reproduce.error) : undefined}
        onCancel={close}
        onConfirm={() => {
          if (statements.length && !agreed) {
            setUnagreed(true);
            return;
          }
          reproduce.mutate(undefined, {
            onSuccess: (made) => {
              close();
              void navigate({ to: "/runs/$ref/", params: { ref: made.hrid } }).then(() => {
                say(`Started ${made.hrid}, reproducing ${original.hrid}.`);
              });
            },
          });
        }}
      >
        <p>
          It runs {original.hrid} again with the same plugin commit, params, settings and seed, on
          its inputs as they were then. It changes no entities: when it ends, it says whether its
          results are the same.
        </p>
        <TextField
          label="Reason"
          value={reason}
          onChange={setReason}
          maxLength={2000}
          optional
          multiline
          help="Why, for the record. It’s kept with the run."
        />
        <SendsOutside
          projectName="its project"
          statements={statements}
          missing={[]}
          agreed={agreed}
          onAgree={(value) => {
            setAgreed(value);
            setUnagreed(false);
          }}
          error={unagreed ? "Agree to send this data again, or don’t reproduce it." : undefined}
        />
      </ConfirmDialog>
    </div>
  );
}

/** A run waiting for the files it reads to be scanned for malware (ADR 0234): which, since
 * when, and what happens once they are. */
function ScanWaitNote({ run, wait }: { run: Run; wait: NonNullable<Run["waiting_for_scan"]> }) {
  const count = wait.files.length + wait.others;
  const one = count === 1;
  return (
    <div className="alert alert-info" role="status">
      <p className="alert-title">Waiting for a malware scan</p>
      <p>
        {one ? "A file it reads" : `${String(count)} files it reads`}
        {wait.files.length ? (
          <>
            {" ("}
            {wait.files.map((file, index) => (
              <span key={file.id}>
                {index ? ", " : null}
                <Link to="/files/$id/" params={{ id: file.id }}>
                  {file.name}
                </Link>
              </span>
            ))}
            {wait.others ? `, and ${String(wait.others)} you can’t read` : null})
          </>
        ) : null}{" "}
        {one ? "is" : "are"} being scanned, since <RelativeTime value={wait.since} />. No worker
        takes the run until {one ? "it’s" : "they’re"} found clean; then it starts by itself. If{" "}
        {one ? "it’s" : "one is"} quarantined, the run fails, saying why.
      </p>
      <p>
        <Link to="/files/" search={{ run: run.hrid }}>
          Files it reads or made
        </Link>
      </p>
    </div>
  );
}

/** For a run that reused another's results: which, and a way to run it after all. */
function ReusedNote({ run }: { run: Run }) {
  const { say } = useMessages();
  const navigate = useNavigate();
  const again = useApiMutation(async () => {
    const inputs: Record<string, string[]> = {};
    for (const input of run.inputs) {
      if (!input.entity) throw new Error("Every input must be readable to run this action again.");
      (inputs[input.name] ??= []).push(input.entity);
    }
    return unwrap(
      await iki.POST("/api/v1/actions/{name}/runs", {
        params: { path: { name: run.action } },
        body: {
          project: run.project,
          inputs,
          params: run.params,
          reason: run.reason,
          reuse: false,
          // Its inputs were in the project; one taken out since is refused, saying so.
          add_to_project: false,
        },
      }),
    );
  }, [RUNS]);
  return (
    <div className="alert alert-info" role="status">
      <p>
        Reused the results of{" "}
        {run.reused_from ? (
          <Link to="/runs/$ref/" params={{ ref: run.reused_from.hrid }}>
            {run.reused_from.hrid}
          </Link>
        ) : (
          "an earlier run"
        )}
        , an identical run: the same plugin commit, params and settings, on the same inputs at the
        same revisions. Nothing ran.
      </p>
      {again.error ? <ProblemAlert problem={problemOf(again.error)} /> : null}
      {run.can.run_again ? (
        <div className="actions">
          <Button
            busy={again.isPending}
            onClick={() => {
              again.mutate(undefined, {
                onSuccess: (made) => {
                  void navigate({ to: "/runs/$ref/", params: { ref: made.hrid } }).then(() => {
                    say(`Started ${made.hrid}.`);
                  });
                },
              });
            }}
          >
            Run again
          </Button>
        </div>
      ) : null}
    </div>
  );
}

/** For a reproduction: what it reproduces, and once it has results, whether they match. */
function ReproductionNote({ run }: { run: Run }) {
  const original = run.reproduces ? (
    <Link to="/runs/$ref/" params={{ ref: run.reproduces.hrid }}>
      {run.reproduces.hrid}
    </Link>
  ) : (
    "the run it reproduces"
  );
  const result = run.reproduction;
  if (!result) {
    return (
      <div className="alert alert-info" role="status">
        <p>
          Reproduces {original} exactly: the same plugin commit, params, settings and seed, on its
          inputs as they were then. It changes no entities.
          {finished(run.status) ? null : " When it ends, it compares its results with those."}
        </p>
      </div>
    );
  }
  const builds = result.environment_build;
  const otherBuild = builds.original && builds.reproduced && builds.original !== builds.reproduced;
  if (result.matches) {
    return (
      <div className="alert alert-success" role="status">
        <p className="alert-title">Reproduced</p>
        <p>Its results are the same as {original}’s, in the same environment build.</p>
      </div>
    );
  }
  const count = result.differences.length + result.more_differences;
  return (
    <div className="alert alert-warning" role="status">
      <p className="alert-title">Didn’t reproduce</p>
      <p>
        {count
          ? `Its results differ from ${run.reproduces?.hrid ?? "the original"}’s in ${counted(count, "place", "places")}`
          : `Its results are the same as ${run.reproduces?.hrid ?? "the original"}’s`}
        {otherBuild ? `, and it ran in another environment build.` : "."}
      </p>
      {result.differences.length ? (
        <ul className="run-differences">
          {result.differences.map((d) => (
            <li key={d.pointer}>
              <code>{d.pointer}</code>: {shown(d.original)} → {shown(d.reproduced)}
            </li>
          ))}
          {result.more_differences ? (
            <li className="muted">
              and {counted(result.more_differences, "more place", "more places")}
            </li>
          ) : null}
        </ul>
      ) : null}
      {otherBuild ? (
        <p className="muted">
          Environment build <code>{builds.original}</code> then, <code>{builds.reproduced}</code>{" "}
          now.
        </p>
      ) : null}
    </div>
  );
}

/** A value from an output, as a difference shows it. */
function shown(value: unknown): string {
  if (value === null || value === undefined) return "nothing";
  return typeof value === "string" ? value : JSON.stringify(value);
}

type Approval = NonNullable<Run["approval"]>;

/** Whoever asked: the trigger, or the app, for whom. */
function asker(approval: Approval): string {
  if (approval.trigger) return `The trigger “${approval.trigger.label}”`;
  return approval.agent?.name ?? (approval.client || "An app");
}

/** A run an agent asked for that waits for the person's approval: theirs to decide. */
function ApprovalRequest({ run, approval }: { run: Run; approval: Approval }) {
  const [declining, setDeclining] = useState(false);
  const [note, setNote] = useState("");
  const [missing, setMissing] = useState(false);
  const reasonField = useRef<HTMLDivElement>(null);
  const { say } = useMessages();
  const others = approval.runs.length - 1;
  const approve = useApiMutation(
    async () =>
      unwrap(
        await iki.POST("/api/v1/run-approvals/{approval_id}/approve", {
          params: { path: { approval_id: approval.id } },
        }),
      ),
    [RUNS],
  );
  const decline = useApiMutation(
    async () =>
      unwrap(
        await iki.POST("/api/v1/run-approvals/{approval_id}/decline", {
          params: { path: { approval_id: approval.id } },
          body: { note: note.trim() },
        }),
      ),
    [RUNS],
  );
  const closeDecline = () => {
    setDeclining(false);
    setNote("");
    setMissing(false);
    decline.reset();
  };
  const declineProblem = decline.error ? problemOf(decline.error) : undefined;
  const noteError = missing ? "Say why it’s declined." : fieldErrors(declineProblem)["note"];
  const count = approval.runs.length;
  const these = count > 1 ? `these ${String(count)} runs` : "this run";
  const consent = approval.consent;
  const who = approval.trigger ? "a trigger" : "an AI agent";
  const why = consent.length
    ? `${count > 1 ? "They send" : "It sends"} data outside Iki, to ${destinations(consent)}, which ${who} can’t agree to for anyone`
    : `${count > 1 ? "They run" : "It runs"} on the ${run.tier} tier, which ${who} can’t start by itself`;
  return (
    <section className="alert alert-warning" aria-labelledby="approval-title">
      <h2 className="alert-title" id="approval-title">
        {approval.can.decide ? "Waiting for your approval" : "Waiting for approval"}
      </h2>
      <p>
        {asker(approval)} asked to start {these} for {approval.requested_by.name}{" "}
        <RelativeTime value={approval.created_at} />. {why}, so{" "}
        {approval.can.decide
          ? `${count > 1 ? "they wait" : "it waits"} for you.`
          : `${count > 1 ? "they wait" : "it waits"} for ${approval.decider.name} to approve ${count > 1 ? "them" : "it"} in Iki.`}{" "}
        If nobody decides, the request expires <RelativeTime value={approval.expires_at} />.
      </p>
      {others > 0 ? (
        <p>
          It’s one of {count} runs asked for together, and they’re decided together:{" "}
          {approval.runs
            .filter((r) => r.id !== run.id)
            .slice(0, 5)
            .map((r, i) => (
              <span key={r.id}>
                {i ? ", " : ""}
                <Link to="/runs/$ref/" params={{ ref: r.hrid }}>
                  {r.hrid}
                </Link>
              </span>
            ))}
          {others > 5 ? ` and ${String(others - 5)} more` : ""}.
        </p>
      ) : null}
      {consent.length ? (
        <>
          <p>
            {approval.can.decide ? "Approving agrees, for you," : "Approving agrees"} to what{" "}
            {count > 1 ? "they send" : "it sends"}:
          </p>
          <ul>
            {consent.map((s) => (
              <li key={`${s.step ?? ""}:${s.api}`}>{s.statement}</li>
            ))}
          </ul>
        </>
      ) : null}
      {approve.error ? <ProblemAlert problem={problemOf(approve.error)} /> : null}
      {approval.can.decide ? (
        <div className="actions">
          <Button
            variant="primary"
            busy={approve.isPending}
            onClick={() => {
              approve.mutate(undefined, {
                onSuccess: () => {
                  say(count > 1 ? `Approved ${String(count)} runs.` : `Approved ${run.hrid}.`);
                },
              });
            }}
          >
            {count > 1 ? `Approve ${String(count)} runs` : "Approve"}
          </Button>
          <Button
            variant="danger"
            onClick={() => {
              approve.reset();
              setDeclining(true);
            }}
          >
            Decline
          </Button>
        </div>
      ) : null}
      <ConfirmDialog
        isOpen={declining}
        title={count > 1 ? `Decline ${String(count)} runs?` : `Decline ${run.hrid}?`}
        confirmLabel="Decline"
        cancelLabel="Keep waiting"
        confirmVariant="danger"
        busy={decline.isPending}
        problem={declineProblem}
        onCancel={closeDecline}
        onConfirm={() => {
          // A decline says why: the server refuses a blank one, so it isn't sent.
          if (!note.trim()) {
            setMissing(true);
            reasonField.current?.querySelector("textarea")?.focus();
            return;
          }
          decline.mutate(undefined, {
            onSuccess: () => {
              closeDecline();
              say(count > 1 ? `Declined ${String(count)} runs.` : `Declined ${run.hrid}.`);
            },
          });
        }}
      >
        <p>
          {count > 1 ? "They end" : "It ends"} without running, and {asker(approval)} is told they
          were declined. To run {count > 1 ? "them" : "it"} later, start {count > 1 ? "them" : "it"}{" "}
          again.
        </p>
        <div ref={reasonField}>
          <TextField
            label="Reason"
            value={note}
            onChange={(value) => {
              setNote(value);
              setMissing(false);
              if (decline.isError) decline.reset();
            }}
            error={noteError}
            maxLength={2000}
            multiline
            help="Why, for the record. The app that asked can read it."
          />
        </div>
      </ConfirmDialog>
    </section>
  );
}

/** The trigger that launched a run, and the change or time that fired it (ADR 0080). */
/** A run imported from another deployment (ADR 0096): where it ran, and what it was tied
 * to there that didn't come with it. */
function ImportedRows({ imported }: { imported: NonNullable<Run["imported"]> }) {
  const approval = imported.approval;
  const workers = [...new Set(Object.values(imported.workers).filter(Boolean))];
  return (
    <>
      <dt>Imported from</dt>
      <dd>
        {imported.deployment}, where it’s <code>{imported.hrid}</code>
        {imported.bundle_import ? (
          <>
            {" "}
            (
            <Link
              to="/projects/$slug/imports/$importId/"
              params={{ slug: imported.project_slug, importId: imported.bundle_import }}
            >
              the import
            </Link>
            )
          </>
        ) : null}
        {imported.batch ? `, launched in ${imported.batch} there` : ""}
        {imported.workflow ? `, a step of ${imported.workflow} there` : ""}
        {workers.length ? `. It ran on ${workers.join(", ")}.` : ""}
      </dd>
      {approval ? (
        <>
          <dt>Approval there</dt>
          <dd>
            {approval.state === "approved" ? "Approved" : "Not approved"}
            {approval.decided_by ? ` by ${approval.decided_by}` : ""}
            {approval.decided_at ? (
              <>
                {" "}
                <RelativeTime value={approval.decided_at} />
              </>
            ) : null}
            {approval.requested_by ? `, for ${approval.requested_by}` : ""}
            {approval.note ? `: ${approval.note}` : ""}
          </dd>
        </>
      ) : null}
    </>
  );
}

function TriggerRow({ run, trigger }: { run: Run; trigger: NonNullable<Run["trigger"]> }) {
  if (trigger.event === "schedule") {
    return (
      <>
        <dt>Trigger</dt>
        <dd>
          {trigger.label}, on its schedule, for{" "}
          <RelativeTime value={trigger.scheduled_for ?? run.created_at} />
          {trigger.since ? (
            <>
              ; it carries on from the run for <RelativeTime value={trigger.since} />
            </>
          ) : (
            "; its first"
          )}
          . It runs as {run.requested_by.name} on {trigger.enabled_by?.name}’s authority, who turned
          the trigger on.
        </dd>
      </>
    );
  }
  const fired = run.inputs.find((i) => i.entity === trigger.entity);
  return (
    <>
      <dt>Trigger</dt>
      <dd>
        {trigger.label}, when{" "}
        {trigger.entity ? (
          <Link to="/entities/$ref/" params={{ ref: fired?.hrid ?? trigger.entity }}>
            {fired?.hrid ?? "its entity"}
          </Link>
        ) : (
          "an entity you can’t read"
        )}{" "}
        {trigger.event === "entity.created"
          ? "was created"
          : trigger.event === "request.received"
            ? "arrived from the project that asked"
            : "changed"}{" "}
        {trigger.revision !== null ? (
          <span className="muted">(revision {trigger.revision})</span>
        ) : null}
        .{" "}
        {trigger.event === "request.received"
          ? // Nobody here sent it (ADR 0232): its run is a service account's.
            `It runs as ${run.requested_by.name} on ${trigger.enabled_by?.name ?? "an Admin"}’s authority, who turned the trigger on.`
          : `It runs as ${run.requested_by.name}${trigger.editor ? ", whose edit prompted the recheck." : ", who turned the trigger on."}`}
      </dd>
    </>
  );
}

/**
 * A reference's name as a label: `heavy_chain` is "Heavy chain", and a list of rows'
 * `components.part` is "Components · Part" (ADR 0161).
 */
function referenceLabel(field: string) {
  return field
    .split(".")
    .map((name) => {
      const words = name.replaceAll("_", " ");
      return words.charAt(0).toUpperCase() + words.slice(1);
    })
    .join(" · ");
}

function approvalText(approval: Approval) {
  switch (approval.state) {
    case "approved":
    case "declined":
      return (
        <>
          {approval.state === "approved" ? "Approved" : "Declined"}
          {approval.decided_by ? ` by ${approval.decided_by.name}` : ""}{" "}
          {approval.decided_at ? <RelativeTime value={approval.decided_at} /> : null}
          {approval.note ? <span className="preserve-lines">: {approval.note}</span> : null}
        </>
      );
    case "expired":
      return <>Expired: nobody approved it in time</>;
    default:
      return <>Waiting</>;
  }
}

/** When an attempt waited for its files' scans (ADR 0234), for its history. */
function scanWaitNote(wait: NonNullable<Attempt["scan_wait"]>, finished: string | null) {
  const what = wait.files === 1 ? "a file it reads" : `${String(wait.files)} files it reads`;
  // One cancelled as it waited stopped waiting when it ended.
  const ended = wait.ended_at ?? finished;
  if (!ended) {
    return (
      <>
        Waiting for {what} to be scanned for malware, since <RelativeTime value={wait.started_at} />
      </>
    );
  }
  return (
    <>
      Waited {duration(wait.started_at, ended)} for {what} to be scanned for malware, until{" "}
      <Time value={ended} />
    </>
  );
}

function duration(from: string, to: string): string {
  const seconds = Math.max(0, Math.round((Date.parse(to) - Date.parse(from)) / 1000));
  if (seconds < 60) return `${String(seconds)} s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${String(minutes)} min ${String(seconds % 60)} s`;
  return `${String(Math.floor(minutes / 60))} h ${String(minutes % 60)} min`;
}

function paramText(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value);
}

/**
 * A param's value: a file param's file by name, downloadable, when the reader can read it,
 * and a list of files as a list of them.
 */
function ParamValue({
  run,
  type,
  items,
  value,
}: {
  run: Run;
  type: string | undefined;
  items?: string | null | undefined;
  value: unknown;
}) {
  const media = useMedia(type === "file");
  if (type === "list" && items === "file" && Array.isArray(value)) {
    return (
      <ul className="param-files">
        {(value as unknown[]).map((each, index) => (
          // Files are listed in the order chosen, which never changes.
          <li key={index}>
            <ParamValue run={run} type="file" value={each} />
          </li>
        ))}
      </ul>
    );
  }
  const file = typeof value === "string" ? run.files[value] : undefined;
  if (file && typeof value === "string") {
    return (
      <span>
        <a href={`/api/v1/files/${value}/content`} download={file.name}>
          {file.name}
        </a>{" "}
        <span className="muted">
          {formatSize(file.size)} · {media.named(file.media_type)}
        </span>{" "}
        <PreviewButton id={value} name={file.name} mediaType={file.media_type} />
      </span>
    );
  }
  if (type === "file") return <span className="muted">A file you can’t read</span>;
  return <>{paramText(value)}</>;
}

/**
 * What an attempt's command printed. It reads the log from where it got to
 * whenever it's known to be longer (live updates say so), and keeps to the
 * bottom while the reader is there.
 */
function AttemptLog({ runRef, attempt, size }: { runRef: string; attempt: Attempt; size: number }) {
  const [text, setText] = useState("");
  const [next, setNext] = useState(0);
  const [read, setRead] = useState(false);
  const [problem, setProblem] = useState<ReturnType<typeof problemOf>>();
  const box = useRef<HTMLPreElement>(null);
  const atBottom = useRef(true);
  const ended = attempt.finished_at !== null;
  // Past what's kept, the size keeps counting but there's nothing more to read.
  const behind = !read || (next < size && next < LOG_KEPT_BYTES);

  useEffect(() => {
    if (!behind || problem) return;
    // A newer read replaces this one (or the log closed): drop what this one gets.
    const stale = { now: false };
    void (async () => {
      try {
        let offset = next;
        let added = "";
        for (;;) {
          const page = await readLog(runRef, attempt.attempt, offset);
          added += page.text;
          if (!page.more || page.next_offset === offset) {
            offset = page.next_offset;
            break;
          }
          offset = page.next_offset;
        }
        if (stale.now) return;
        setText((t) => t + added);
        setNext(offset);
        setRead(true);
      } catch (error) {
        if (!stale.now) setProblem(problemOf(error));
      }
    })();
    return () => {
      stale.now = true;
    };
  }, [behind, next, problem, runRef, attempt.attempt]);

  useLayoutEffect(() => {
    const el = box.current;
    if (el && atBottom.current) el.scrollTop = el.scrollHeight;
  }, [text]);

  return (
    <div className="log">
      <div className="log-bar">
        <span className="muted">
          {formatSize(size)}
          {size > LOG_KEPT_BYTES ? ", of which the first 16 MB is kept" : ""}
          {!ended ? " so far" : ""}
        </span>
        {size > 0 ? (
          <a href={logDownloadUrl(runRef, attempt.attempt)} download>
            Download log
          </a>
        ) : null}
      </div>
      <ProblemAlert problem={problem} />
      {!read ? <Loading label="Loading the log…" /> : null}
      {read && !text ? (
        <p className="muted">{ended ? "It printed nothing." : "Nothing printed yet."}</p>
      ) : null}
      {text ? (
        <pre
          // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- it scrolls
          tabIndex={0}
          ref={box}
          // A log, which screen readers don't read out line by line as it grows. (Not a
          // region: Playwright 1.65's Firefox crashed checking one added as it loads.)
          role="log"
          aria-live="off"
          className="log-text"
          aria-label={`Log of attempt ${String(attempt.attempt)}`}
          onScroll={(event) => {
            const el = event.currentTarget;
            atBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 8;
          }}
        >
          {text}
        </pre>
      ) : null}
    </div>
  );
}

function others(runs: number): string {
  return runs === 2 ? "1 other run" : `${String(runs - 1)} other runs`;
}

/** How far importing a creates file has got (ADR 0056). */
function importNote(found: NonNullable<Attempt["bulk_import"]>) {
  const lines = counted(found.lines, "line", "lines");
  if (found.phase === "checking") {
    return `Checking its creates file: ${lines} so far, ${formatSize(found.read)} of ${formatSize(found.size)}. Nothing is written until all of it is checked.`;
  }
  if (found.phase === "writing") {
    return `Writing ${counted(found.staged, "entity", "entities")} from its creates file, all at once.`;
  }
  return `Gave its creates in a file of ${formatSize(found.size)}, ${lines}.`;
}

/** Which worker an imported attempt ran on, on the deployment it came from. */
function workedThere(imported: Run["imported"], attempt: number): string | null {
  const worker = imported?.workers[String(attempt)];
  return imported && worker ? `${worker} on ${imported.deployment}` : null;
}

/** "Ran for 1 h 5 min on 8 CPUs, 32 GiB, 7 h 40 min of CPU time, at most 12 GiB of memory". */
export function slurmUsageText(usage: AttemptSlurm["usage"]): string | null {
  const given = usage.alloc ? capacityText(usage.alloc) : "—";
  const parts: string[] = [];
  if (typeof usage.elapsed === "number") {
    parts.push(`Ran for ${machineTime(usage.elapsed)}${given === "—" ? "" : ` on ${given}`}`);
  } else if (given !== "—") {
    parts.push(`Given ${given}`);
  }
  if (typeof usage.cpu_seconds === "number") {
    parts.push(`${machineTime(usage.cpu_seconds)} of CPU time`);
  }
  if (typeof usage.max_rss === "number") {
    parts.push(`at most ${formatSize(usage.max_rss)} of memory in use`);
  }
  return parts.length ? parts.join(", ") : null;
}

/** The Slurm job an attempt ran as (ADR 0133): where it went, how it waited, what it used. */
function SlurmJob({ attempt, slurm }: { attempt: number; slurm: AttemptSlurm }) {
  const used = slurmUsageText(slurm.usage);
  return (
    <div role="group" aria-label={`Slurm job of attempt ${String(attempt)}`}>
      <dl className="summary">
        <dt>Slurm job</dt>
        <dd>
          {slurm.slurm_id ? (
            <>
              <code>{slurm.slurm_id}</code> on {slurm.cluster}
            </>
          ) : (
            `Not submitted to ${slurm.cluster} yet`
          )}
        </dd>
        {slurm.state ? (
          <>
            <dt>Slurm state</dt>
            <dd>
              <code>{slurm.state}</code>
              {slurm.reason ? ` (${slurm.reason})` : ""}
            </dd>
          </>
        ) : null}
        <dt>Partition</dt>
        <dd>
          <code>{slurm.partition}</code>
          {slurm.qos ? (
            <>
              , QOS <code>{slurm.qos}</code>
            </>
          ) : null}
        </dd>
        <dt>Account</dt>
        <dd>{slurm.account ? <code>{slurm.account}</code> : "Slurm’s default for its user"}</dd>
        {slurm.nodes ? (
          <>
            <dt>Nodes</dt>
            <dd>
              <code>{slurm.nodes}</code>
            </dd>
          </>
        ) : null}
        <dt>Asked for</dt>
        <dd>{slurmRequestText(slurm.requested)}</dd>
        {used ? (
          <>
            <dt>Used, as sacct says</dt>
            <dd>{used}</dd>
          </>
        ) : null}
        {slurm.exit_code ? (
          <>
            <dt>Exit code</dt>
            <dd>
              <code>{slurm.exit_code}</code>
            </dd>
          </>
        ) : null}
        {slurm.image_signer ? (
          <>
            <dt>Image signed by</dt>
            <dd>
              <code>{slurm.image_signer}</code>, as its worker checked
            </dd>
          </>
        ) : null}
        {slurm.submitted_at ? (
          <>
            <dt>Submitted</dt>
            <dd>
              <RelativeTime value={slurm.submitted_at} />
            </dd>
          </>
        ) : null}
        {slurm.started_at ? (
          <>
            <dt>Started</dt>
            <dd>
              <RelativeTime value={slurm.started_at} />
              {slurm.submitted_at
                ? `, after ${duration(slurm.submitted_at, slurm.started_at)} in the queue`
                : ""}
            </dd>
          </>
        ) : null}
        {slurm.ended_at ? (
          <>
            <dt>Ended</dt>
            <dd>
              <RelativeTime value={slurm.ended_at} />
            </dd>
          </>
        ) : null}
      </dl>
    </div>
  );
}

function AttemptRow({
  runRef,
  attempt,
  size,
  latest,
  runError,
  there,
}: {
  runRef: string;
  attempt: Attempt;
  size: number;
  /** Where an imported attempt ran, on the deployment it came from. */
  there: string | null;
  latest: boolean;
  /** The run shows this attempt's error already. */
  runError: boolean;
}) {
  const [open, setOpen] = useState(latest);
  const going = attempt.finished_at === null && attempt.started_at !== null;
  return (
    <li className="row row-stacked">
      <div className="row-main">
        <span className="row-title">
          Attempt {attempt.attempt} <StatusBadge status={attempt.state} />
        </span>
        <span className="row-meta">
          {attempt.worker
            ? `On ${attempt.worker}${attempt.worker_labels.length ? ` (${attempt.worker_labels.join(", ")})` : ""}`
            : there
              ? `On ${there}`
              : attempt.started_at
                ? null
                : "Not picked up yet"}
          {attempt.started_at ? (
            <>
              {attempt.worker || there ? " · started " : "Started "}
              <RelativeTime value={attempt.started_at} />
            </>
          ) : null}
          {attempt.started_at && attempt.finished_at
            ? ` · took ${duration(attempt.started_at, attempt.finished_at)}`
            : ""}
          {attempt.environment_build ? (
            <>
              {" · environment "}
              <code title={attempt.environment_build}>
                {attempt.environment_build.replace(/^sha256:/, "").slice(0, 12)}
              </code>
            </>
          ) : null}
        </span>
        {going && attempt.progress !== null ? (
          <div className="run-progress">
            <progress
              value={attempt.progress}
              max={1}
              aria-label={`Attempt ${String(attempt.attempt)} progress`}
            />
            <span>
              {Math.round(attempt.progress * 100)}%
              {attempt.progress_message ? ` · ${attempt.progress_message}` : ""}
            </span>
          </div>
        ) : null}
        {attempt.scan_wait ? (
          <p className="row-meta">{scanWaitNote(attempt.scan_wait, attempt.finished_at)}</p>
        ) : null}
        {attempt.error && !(latest && runError) ? (
          <p className="row-reason">{attempt.error.message}</p>
        ) : null}
        {attempt.slurm ? <SlurmJob attempt={attempt.attempt} slurm={attempt.slurm} /> : null}
        {attempt.bulk_import ? <p className="row-meta">{importNote(attempt.bulk_import)}</p> : null}
        {attempt.resumed_from ? (
          <p className="row-meta">
            Resumed from the checkpoint attempt {attempt.resumed_from.attempt} saved{" "}
            <RelativeTime value={attempt.resumed_from.saved_at} />
          </p>
        ) : null}
        {attempt.checkpoint ? (
          <p className="row-meta">
            Saved a checkpoint of {formatSize(attempt.checkpoint.size)}{" "}
            <RelativeTime value={attempt.checkpoint.saved_at} />
            {attempt.checkpoint.kept ? ", for a later attempt to resume from" : ", since deleted"}
          </p>
        ) : null}
        {attempt.pack ? (
          <p className="row-meta">
            Ran in one job with {others(attempt.pack.runs)} of its batch
            {attempt.pack.reported_as === runRef ? null : (
              <>
                , led by{" "}
                <Link to="/runs/$ref/" params={{ ref: attempt.pack.reported_as }}>
                  {attempt.pack.reported_as}
                </Link>
              </>
            )}
            . They share its log, progress and network use.
          </p>
        ) : null}
        {Object.keys(attempt.volumes).length ? (
          <p className="row-meta">
            Read{" "}
            {Object.entries(attempt.volumes).map(([name, read], index) => (
              <span key={name}>
                {index ? ", " : ""}
                <code>{name}</code> version {read.version} ({read.hrid}
                {read.data_version ? `, ${read.data_version}` : ""})
              </span>
            ))}
          </p>
        ) : null}
        {attempt.secrets.length ? (
          <p className="row-meta">
            Given{" "}
            {attempt.secrets.map((name, index) => (
              <span key={name}>
                {index ? ", " : ""}
                <code>{name}</code>
              </span>
            ))}{" "}
            from this deployment’s secrets
          </p>
        ) : null}
        {attempt.network.length ? (
          <ul
            className="value-list"
            aria-label={`Network use of attempt ${String(attempt.attempt)}`}
          >
            {attempt.network.map((reached) => (
              <li key={`${reached.host}:${String(reached.port)}:${reached.refused ?? ""}`}>
                <code>{reached.host}</code>
                {reached.port === 443 ? "" : `:${String(reached.port)}`}
                {reached.refused === null
                  ? ` · ${String(reached.connections)} connection${reached.connections === 1 ? "" : "s"}, ${formatSize(reached.bytes_received)} received, ${formatSize(reached.bytes_sent)} sent`
                  : ` · refused: ${reached.refused}`}
              </li>
            ))}
          </ul>
        ) : null}
        {open ? <AttemptLog runRef={runRef} attempt={attempt} size={size} /> : null}
      </div>
      <div className="row-actions">
        <Button
          variant="quiet"
          label={`${open ? "Hide" : "Show"} log of attempt ${String(attempt.attempt)}`}
          onClick={() => {
            setOpen(!open);
          }}
        >
          {open ? "Hide log" : "Show log"}
        </Button>
      </div>
    </li>
  );
}

export function RunPage() {
  const run = useRun();
  const actions = useQuery(actionsQuery).data;
  const action = actions?.find((a) => a.name === run.action);
  const label = actionLabel(actions, run.action);
  usePageTitle(`${label} (${run.hrid})`);
  const project = useQuery(projectByIdQuery(run.project)).data;
  const attempts = useQuery(attemptsQuery(run.hrid));
  const artifacts = useQuery(artifactsQuery(run.hrid)).data ?? [];
  const client = useQueryClient();
  const [sizes, setSizes] = useState<Record<number, number>>({});
  const done = finished(run.status);
  useRunUpdates(run.hrid, !done, (attempt, size) => {
    setSizes((known) => ({ ...known, [attempt]: Math.max(known[attempt] ?? 0, size) }));
  });

  // When it ends, what it changed is read again wherever it shows.
  const wasDone = useRef(done);
  useEffect(() => {
    if (done && !wasDone.current) {
      void client.invalidateQueries({ queryKey: ENTITIES });
      void client.invalidateQueries({ queryKey: attemptsQuery(run.hrid).queryKey });
      void client.invalidateQueries({ queryKey: artifactsQuery(run.hrid).queryKey });
      void client.invalidateQueries({ queryKey: [...RUNS, "list"] });
    }
    wasDone.current = done;
  }, [done, client, run.hrid]);

  const tense = tenseOf(run);
  const paramLabels = new Map(action?.params.map((p) => [p.name, p.label]) ?? []);
  const paramNotes = new Map(action?.params.map((p) => [p.name, p.description]) ?? []);
  const paramTypes = new Map(action?.params.map((p) => [p.name, p.type]) ?? []);
  const paramItems = new Map(action?.params.map((p) => [p.name, p.items]) ?? []);
  const inputLabels = new Map(action?.inputs.map((i) => [i.name, i.label]) ?? []);
  const metricLabels = new Map(action?.metrics.map((m) => [m.name, m.label]) ?? []);
  const params = Object.entries(run.params);
  const metrics = Object.entries(run.metrics);
  const items = attempts.data ?? [];
  return (
    <>
      <PageHeader
        crumbs={[
          <Link to="/runs/">Runs</Link>,
          ...(project
            ? [
                <Link to="/projects/$slug/runs/" params={{ slug: project.slug }}>
                  {project.name}
                </Link>,
              ]
            : []),
          run.hrid,
        ]}
        title={label}
        badge={
          <>
            <Badge>{run.hrid}</Badge> <StatusBadge status={run.status} />
          </>
        }
        actions={
          <>
            <ReproduceRun run={run} />
            <CancelRun run={run} />
          </>
        }
      />
      {run.cancel_requested_at && !done ? (
        <div className="alert alert-warning" role="status">
          <p>
            Cancelling: asked to stop <RelativeTime value={run.cancel_requested_at} />. Its worker
            ends it.
          </p>
        </div>
      ) : null}
      {run.approval?.state === "pending" ? (
        <ApprovalRequest run={run} approval={run.approval} />
      ) : null}
      {run.waiting_for_scan ? (
        <ScanWaitNote run={run} wait={run.waiting_for_scan} />
      ) : run.waiting ? (
        <div className="alert alert-info" role="status">
          <p>Waiting to start: {run.waiting}</p>
        </div>
      ) : null}
      {run.needs_worker ? <NeedsWorker why={run.needs_worker} labels={run.labels} /> : null}
      <ParkedNote run={run} />
      {run.reused ? <ReusedNote run={run} /> : null}
      {run.reproduces || run.reproduction ? <ReproductionNote run={run} /> : null}
      {run.error ? (
        <div className="alert alert-error" role="alert">
          <p className="alert-title">{STATUS[run.status]}</p>
          <p className="preserve-lines">{run.error.message}</p>
          {run.error.errors?.length ? (
            <ul>
              {run.error.errors.map((e) => (
                <li key={e.pointer}>
                  <code>{e.pointer}</code>: {e.detail}
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}

      {run.status === "succeeded" || run.effects.length ? (
        <section className="card" aria-labelledby="results-title">
          <h2 className="section-title" id="results-title">
            Results
          </h2>
          <RunEffects run={run} />
          {metrics.length ? (
            <dl className="summary">
              {metrics.map(([name, value]) => (
                <div key={name} className="value-row">
                  <dt>{metricLabels.get(name) ?? name}</dt>
                  <dd>
                    {typeof value === "number" && Number.isFinite(value)
                      ? metricNumber.format(value)
                      : value}
                  </dd>
                </div>
              ))}
            </dl>
          ) : null}
        </section>
      ) : null}

      {run.messages.length ? (
        <section className="card" aria-labelledby="messages-title">
          <h2 className="section-title" id="messages-title">
            Messages
          </h2>
          <ul className="run-messages">
            {run.messages.map((m, index) => (
              // Messages can repeat; their position tells them apart.
              <li key={index} className={`run-message run-message-${m.level}`}>
                <span className="run-message-level">
                  {m.level === "info" ? "Info" : m.level === "warning" ? "Warning" : "Error"}
                </span>{" "}
                {m.entity ? (
                  <span className="muted">
                    {run.inputs.find((i) => i.entity === m.entity)?.hrid ?? "An input"}:{" "}
                  </span>
                ) : null}
                <span className="preserve-lines">{m.text}</span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {artifacts.length ? <RunArtifacts runRef={run.hrid} artifacts={artifacts} /> : null}

      <RunOutsideApis run={run} />

      <section aria-labelledby="attempts-title">
        <h2 className="section-title" id="attempts-title">
          Attempts
        </h2>
        {attempts.isPending ? <Loading label="Loading attempts…" /> : null}
        {attempts.error ? <ProblemAlert problem={problemOf(attempts.error)} /> : null}
        {items.length ? (
          <ul className="rows">
            {items.map((attempt, index) => (
              <AttemptRow
                key={attempt.attempt}
                runRef={run.hrid}
                attempt={attempt}
                size={Math.max(attempt.log_size, sizes[attempt.attempt] ?? 0)}
                latest={index === items.length - 1}
                runError={run.error !== null}
                there={workedThere(run.imported, attempt.attempt)}
              />
            ))}
          </ul>
        ) : null}
      </section>

      <section className="card" aria-labelledby="inputs-title">
        <h2 className="section-title" id="inputs-title">
          Inputs
        </h2>
        {run.inputs.length === 0 &&
        params.length === 0 &&
        !run.imported?.inputs_elsewhere.length ? (
          <p className="muted">It took no inputs.</p>
        ) : null}
        <dl className="summary">
          {run.inputs.map((input, index) => (
            <div key={`${input.name}-${String(index)}`} className="value-row">
              <dt>{inputLabels.get(input.name) ?? input.name}</dt>
              <dd>
                {input.hrid ? (
                  <>
                    <Link to="/entities/$ref/" params={{ ref: input.hrid }}>
                      {input.hrid}
                    </Link>{" "}
                    <span className="muted">as of revision {input.revision}</span>
                  </>
                ) : (
                  "An entity you can’t read"
                )}
              </dd>
            </div>
          ))}
          {run.included.map((read) => (
            <div
              key={[read.input, read.position, read.field, read.index].join("-")}
              className="value-row"
            >
              <dt>{referenceLabel(read.field)}</dt>
              <dd>
                {read.hrid ? (
                  <>
                    <Link to="/entities/$ref/" params={{ ref: read.hrid }}>
                      {read.hrid}
                    </Link>{" "}
                    <span className="muted">
                      as of revision {read.revision}, which{" "}
                      {run.inputs.filter((i) => i.name === read.input)[read.position]?.hrid ??
                        "an input"}{" "}
                      names
                    </span>
                  </>
                ) : (
                  "An entity you can’t read"
                )}
              </dd>
            </div>
          ))}
          {(run.imported?.inputs_elsewhere ?? []).map((input) => (
            <div key={`${input.name}-${String(input.position)}`} className="value-row">
              <dt>{inputLabels.get(input.name) ?? input.name}</dt>
              <dd>
                <span className="muted">
                  {input.entity ? (
                    <>
                      An entity on {run.imported?.deployment} that didn’t come with it (
                      <code>{input.entity}</code>, revision {input.revision})
                    </>
                  ) : (
                    "An entity you can’t read"
                  )}
                </span>
              </dd>
            </div>
          ))}
          {(run.imported?.included_elsewhere ?? []).map((read) => (
            <div
              key={[read.input, read.position, read.field, read.index].join("-")}
              className="value-row"
            >
              <dt>{referenceLabel(read.field)}</dt>
              <dd>
                <span className="muted">
                  {read.entity ? (
                    <>
                      An entity on {run.imported?.deployment} that didn’t come with it (
                      <code>{read.entity}</code>, revision {read.revision})
                    </>
                  ) : (
                    "An entity you can’t read"
                  )}
                </span>
              </dd>
            </div>
          ))}
          {params.map(([name, value]) => {
            // What the form said it's for, such as "For FASTA records": without it, a
            // value can read as more than it was.
            const note = paramNotes.get(name);
            return (
              <div key={name} className="value-row">
                <dt>{paramLabels.get(name) ?? name}</dt>
                <dd>
                  <ParamValue
                    run={run}
                    type={paramTypes.get(name)}
                    items={paramItems.get(name)}
                    value={value}
                  />
                  {note ? <p className="field-help">{note}</p> : null}
                </dd>
              </div>
            );
          })}
        </dl>
        {/* The files it was given and made, in the list that finds files (ADR 0177). */}
        <p className="run-files-link">
          <Link to="/files/" search={{ run: run.hrid }}>
            Files it read or made
          </Link>
        </p>
      </section>

      <section className="card" aria-labelledby="details-title">
        <h2 className="section-title" id="details-title">
          Details
        </h2>
        <dl className="summary">
          <dt>ID</dt>
          <dd>
            <code>{run.hrid}</code>
          </dd>
          <dt>Requested</dt>
          <dd>
            By {run.requested_by.name} <RelativeTime value={run.created_at} />{" "}
            {VIA[run.via] ?? run.via}
            <Through agent={run.agent} />
          </dd>
          <AgentRows agent={run.agent} />
          {run.trigger ? <TriggerRow run={run} trigger={run.trigger} /> : null}
          {run.approval && run.approval.state !== "pending" ? (
            <>
              <dt>Approval</dt>
              <dd>{approvalText(run.approval)}</dd>
            </>
          ) : null}
          {run.imported ? <ImportedRows imported={run.imported} /> : null}
          {run.reason ? (
            <>
              <dt>Reason</dt>
              <dd className="preserve-lines">{run.reason}</dd>
            </>
          ) : null}
          {run.batch ? (
            <>
              <dt>Batch</dt>
              <dd>
                Launched with others in{" "}
                <Link to="/batches/$ref/" params={{ ref: run.batch.hrid }}>
                  {run.batch.hrid}
                </Link>
              </dd>
            </>
          ) : null}
          {run.workflow ? (
            <>
              <dt>Workflow</dt>
              <dd>
                Step <code>{run.workflow.step}</code> of{" "}
                <Link to="/workflow-runs/$ref/" params={{ ref: run.workflow.hrid }}>
                  {run.workflow.hrid}
                </Link>
              </dd>
            </>
          ) : null}
          {run.started_at ? (
            <>
              <dt>{tense === "now" ? "Running" : "Ran"}</dt>
              <dd>
                Started <RelativeTime value={run.started_at} />
                {run.finished_at ? `, took ${duration(run.started_at, run.finished_at)}` : ""}
              </dd>
            </>
          ) : null}
          <dt>Action</dt>
          <dd>
            <code>{run.action}</code> from plugin version {run.plugin_version}
            {run.plugin_sha ? (
              <>
                {" "}
                (<code title={run.plugin_sha}>{run.plugin_sha.slice(0, 12)}</code>)
              </>
            ) : null}
          </dd>
          {run.seed !== null ? (
            <>
              <dt>Seed</dt>
              <dd>
                <code>{run.seed}</code>{" "}
                <span className="muted">given to its command as IKI_SEED</span>
              </dd>
            </>
          ) : null}
          {Object.keys(run.settings).length ? (
            <>
              <dt>Plugin settings</dt>
              <dd>
                {Object.entries(run.settings).map(([name, value], index) => (
                  <span key={name}>
                    {index ? "; " : ""}
                    <code>{name}</code>: {paramText(value)}
                  </span>
                ))}
              </dd>
            </>
          ) : null}
          <dt>Network</dt>
          <dd>
            {run.network.length ? (
              <>
                {TENSES[tense].reach}{" "}
                {run.network.map((host, index) => (
                  <span key={host}>
                    {index ? ", " : ""}
                    <code>{host}</code>
                  </span>
                ))}
                , through Iki’s egress proxy
              </>
            ) : (
              TENSES[tense].offline
            )}
          </dd>
          <dt>{TENSES[tense].on}</dt>
          <dd>
            {run.tier === "self_hosted" ? "A self-hosted worker" : `A ${run.tier} worker`}
            {run.labels.length ? (
              <>
                {" "}
                labelled{" "}
                {run.labels.map((label, index) => (
                  <span key={label}>
                    {index ? ", " : null}
                    <code>{label}</code>
                  </span>
                ))}
              </>
            ) : null}
            {run.environment ? (
              <>
                , in the <code>{run.environment}</code> environment
              </>
            ) : null}
          </dd>
          {run.cost ? (
            <>
              <dt>Cost</dt>
              <dd>
                <RunCost cost={run.cost} />
              </dd>
            </>
          ) : null}
        </dl>
      </section>
    </>
  );
}

/** How far a run has got, as its details speak of where it runs and what it reaches:
 * before a worker takes it, while it runs, once it ran, or ended before it ever ran. */
type Tense = "ahead" | "now" | "past" | "never";

const TENSES: Record<Tense, { on: string; reach: string; offline: string }> = {
  ahead: {
    on: "Will run on",
    reach: "May reach",
    offline: "None: it won’t be able to reach the internet",
  },
  now: { on: "Running on", reach: "May reach", offline: "None: it can’t reach the internet" },
  past: { on: "Ran on", reach: "Could reach", offline: "None: it couldn’t reach the internet" },
  never: {
    on: "Was to run on",
    reach: "Could have reached",
    offline: "None: it couldn’t have reached the internet",
  },
};

function tenseOf(run: Run): Tense {
  if (finished(run.status)) return run.started_at ? "past" : "never";
  return run.status === "running" || run.status === "parked" ? "now" : "ahead";
}

/** What the run's machines cost (ADR 0070): so far while one is still its, then all. */
function RunCost({ cost }: { cost: NonNullable<Run["cost"]> }) {
  const time = machineTime(cost.seconds);
  if (!cost.currency) {
    return (
      <>
        {time} of machine time <span className="muted">(its machine’s template has no price)</span>
      </>
    );
  }
  const apis = cost.apis ?? 0;
  return (
    <>
      {money(cost.amount, cost.currency)}
      {cost.final ? "" : " so far"}{" "}
      <span className="muted">
        {apis
          ? `: ${money(apis, cost.currency)} for outside APIs’ calls, and the rest for ${time} of machine time`
          : `for ${time} of machine time`}
      </span>
    </>
  );
}

/** Files the run's command wrote that its action keeps (ADR 0068), each attempt's, with
 * when each is deleted. A deleted one keeps its record: its size and SHA-256. */
function RunArtifacts({ runRef, artifacts }: { runRef: string; artifacts: Artifact[] }) {
  const several = new Set(artifacts.map((a) => a.attempt)).size > 1;
  return (
    <section className="card" aria-labelledby="artifacts-title">
      <h2 className="section-title" id="artifacts-title">
        Artifacts
      </h2>
      <p className="muted">
        Files the run wrote besides its results, which its action keeps for a while. Each is deleted
        when its retention is over; what it was stays recorded here.
      </p>
      <div className="table-scroll">
        <table className="table">
          <thead>
            <tr>
              <th scope="col">File</th>
              {several ? <th scope="col">Attempt</th> : null}
              <th scope="col">Size</th>
              <th scope="col">Kept</th>
            </tr>
          </thead>
          <tbody>
            {artifacts.map((artifact) => (
              <tr key={artifact.id}>
                <td>
                  {artifact.state === "kept" ? (
                    <a href={artifactDownloadUrl(runRef, artifact.id)} download={artifact.name}>
                      {artifact.path}
                    </a>
                  ) : (
                    artifact.path
                  )}
                  <div className="muted">
                    SHA-256 <code title={artifact.sha256}>{artifact.sha256.slice(0, 12)}…</code>
                  </div>
                  {artifact.scan?.usable && artifact.scan.state === "not_scanned" ? (
                    <div className="muted">Not scanned: too large</div>
                  ) : null}
                  {artifact.scan?.released ? (
                    <div className="muted">Released from quarantine</div>
                  ) : null}
                </td>
                {several ? <td>{artifact.attempt}</td> : null}
                <td>{formatSize(artifact.size)}</td>
                <td>
                  <ArtifactKept artifact={artifact} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function ArtifactKept({ artifact }: { artifact: Artifact }) {
  const now = useNow();
  if (artifact.state === "deleted" && artifact.deleted_at) {
    return (
      <span className="muted">
        Deleted <Time value={artifact.deleted_at} />, its retention over
      </span>
    );
  }
  if (artifact.state === "scanning") return <span className="muted">Scanning…</span>;
  if (artifact.state === "quarantined") return <>Quarantined</>;
  if (artifact.state === "not_scanned") return <>Not scanned</>;
  if (artifact.state === "uploading") return <span className="muted">Uploading…</span>;
  if (artifact.state === "failed") return <span className="muted">Didn’t finish uploading</span>;
  // The organization may keep it longer, as a legal hold does (ADR 0092).
  if (Date.parse(artifact.expires_at) <= now) return <>Kept past its retention</>;
  return (
    <>
      Until <Time value={artifact.expires_at} />
    </>
  );
}
