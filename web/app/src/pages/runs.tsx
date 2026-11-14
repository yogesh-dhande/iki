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
import { useEffect, useLayoutEffect, useRef, useState } from "react";

import { AgentRows, Through } from "../agent";
import { iki, problemOf, projectsQuery, useApiMutation } from "../api";
import { machineTime, money } from "../costs";
import { ENTITIES, formatSize, typeLabel, typesQuery } from "../entities";
import { usePageTitle } from "../layout";
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
  SelectField,
  TextField,
  Time,
  useMessages,
} from "../ui";
import { useProject } from "./project-overview";

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

const STATUS_CHOICES = [
  { value: "all", label: "Every status" },
  ...Object.entries(STATUS).map(([value, label]) => ({ value, label })),
] as const;

/** A client-tier run runs on its requester's own computer, so it waits for their worker. */
function NeedsWorker({ whose, run }: { whose: "yours" | "theirs"; run: Run }) {
  return (
    <div className="alert alert-info" role="status">
      {whose === "yours" ? (
        <p>
          Waiting for a worker on your computer: this action runs on the client tier, and none of
          yours is online. Start <code>iki-worker</code>, or sign one in from{" "}
          <a href="/account/workers/">Your workers</a>.
        </p>
      ) : (
        <p>
          Waiting for a worker on {run.requested_by.name}’s computer: this action runs on the client
          tier, and none of theirs is online.
        </p>
      )}
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
  const actions = useQuery(actionsQuery).data;
  const runs = useInfiniteQuery({
    ...projectRunsQuery(project.id, status),
    refetchInterval: (query) =>
      query.state.data?.pages.some((p) => p.items.some((r) => !finished(r.status)))
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
              to: "/projects/$slug/runs/",
              params: { slug: project.slug },
              search: value === "all" ? {} : { status: value as RunStatus },
            });
          }}
        />
        {project.can.run.length ? (
          <Link
            to="/projects/$slug/runs/new/"
            params={{ slug: project.slug }}
            className={`${buttonClass("primary")} toolbar-end`}
          >
            New run
          </Link>
        ) : null}
      </div>
      {runs.isPending ? <Loading label="Loading runs…" /> : null}
      {runs.error ? <ProblemAlert problem={problemOf(runs.error)} /> : null}
      {runs.isSuccess && items.length === 0 ? (
        <p className="muted empty">
          {status
            ? `No runs are ${STATUS[status].toLowerCase()}.`
            : "No runs yet. When someone runs an action in this project, it’s listed here."}
        </p>
      ) : null}
      {items.length ? (
        <ul className="rows">
          {items.map((run) => (
            <li className="row" key={run.id}>
              <div className="row-main">
                <Link className="row-title" to="/runs/$ref/" params={{ ref: run.hrid }}>
                  {actionLabel(actions, run.action)}
                </Link>
                <span className="row-meta">
                  {run.hrid}
                  {run.batch ? (
                    <>
                      {" in "}
                      <Link to="/batches/$ref/" params={{ ref: run.batch.hrid }}>
                        {run.batch.hrid}
                      </Link>
                    </>
                  ) : null}
                  {run.workflow ? (
                    <>
                      {" in "}
                      <Link to="/workflow-runs/$ref/" params={{ ref: run.workflow.hrid }}>
                        {run.workflow.hrid}
                      </Link>
                    </>
                  ) : null}{" "}
                  · {run.requested_by.name} · <RelativeTime value={run.created_at} />
                  {run.reason ? ` · “${run.reason}”` : ""}
                </span>
              </div>
              <div className="row-tags">
                <StatusBadge status={run.status} />
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

function ReproduceRun({ run }: { run: Run }) {
  const [asking, setAsking] = useState(false);
  const [reason, setReason] = useState("");
  const { say } = useMessages();
  const navigate = useNavigate();
  const original = reproducible(run);
  const reproduce = useApiMutation(
    async () =>
      unwrap(
        await iki.POST("/api/v1/runs/{ref}/reproduce", {
          params: { path: { ref: original.id } },
          body: { reason: reason.trim() },
        }),
      ),
    [RUNS],
  );
  if (!run.can.reproduce) return null;
  const close = () => {
    setAsking(false);
    setReason("");
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
      </ConfirmDialog>
    </div>
  );
}

/** For a run that reused another's results: which, and a way to run it after all. */
function ReusedNote({ run }: { run: Run }) {
  const { say } = useMessages();
  const navigate = useNavigate();
  const again = useApiMutation(async () => {
    const inputs: Record<string, string[]> = {};
    for (const input of run.inputs) (inputs[input.name] ??= []).push(input.entity);
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
    decline.reset();
  };
  const count = approval.runs.length;
  const these = count > 1 ? `these ${String(count)} runs` : "this run";
  return (
    <section className="alert alert-warning" aria-labelledby="approval-title">
      <h2 className="alert-title" id="approval-title">
        {approval.can.decide ? "Waiting for your approval" : "Waiting for approval"}
      </h2>
      <p>
        {asker(approval)} asked to start {these} for {approval.requested_by.name}{" "}
        <RelativeTime value={approval.created_at} />. {count > 1 ? "They run" : "It runs"} on the{" "}
        {run.tier} tier, which {approval.trigger ? "a trigger" : "an AI agent"} can’t start by
        itself, so{" "}
        {approval.can.decide
          ? `${count > 1 ? "they wait" : "it waits"} for you.`
          : `${count > 1 ? "they wait" : "it waits"} for ${approval.requested_by.name} to approve ${count > 1 ? "them" : "it"} in Iki.`}{" "}
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
        problem={decline.error ? problemOf(decline.error) : undefined}
        onCancel={closeDecline}
        onConfirm={() => {
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
        <TextField
          label="Note"
          value={note}
          onChange={setNote}
          maxLength={2000}
          optional
          multiline
          help="Why, for the record. The app that asked can read it."
        />
      </ConfirmDialog>
    </section>
  );
}

/** How a request was decided, for the run's details. */
/** The trigger that launched a run, and the change that fired it (ADR 0080). */
function TriggerRow({ run, trigger }: { run: Run; trigger: NonNullable<Run["trigger"]> }) {
  const fired = run.inputs.find((i) => i.entity === trigger.entity);
  return (
    <>
      <dt>Trigger</dt>
      <dd>
        {trigger.label}, when{" "}
        <Link to="/entities/$ref/" params={{ ref: fired?.hrid ?? trigger.entity }}>
          {fired?.hrid ?? "its entity"}
        </Link>{" "}
        {trigger.event === "entity.created" ? "was created" : "changed"}{" "}
        <span className="muted">(revision {trigger.revision})</span>. It runs as{" "}
        {run.requested_by.name}, who turned the trigger on.
      </dd>
    </>
  );
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

/** A param's value: a file param's file by name, downloadable, when the reader can read it. */
function ParamValue({ run, type, value }: { run: Run; type: string | undefined; value: unknown }) {
  const file = typeof value === "string" ? run.files[value] : undefined;
  if (file && typeof value === "string") {
    return (
      <span>
        <a href={`/api/v1/files/${value}/content`} download={file.name}>
          {file.name}
        </a>{" "}
        <span className="muted">
          {formatSize(file.size)} · {file.media_type}
        </span>
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

function AttemptRow({
  runRef,
  attempt,
  size,
  latest,
  runError,
}: {
  runRef: string;
  attempt: Attempt;
  size: number;
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
          {attempt.worker ? `On ${attempt.worker}` : "Not picked up yet"}
          {attempt.started_at ? (
            <>
              {" · started "}
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
        {attempt.error && !(latest && runError) ? (
          <p className="row-reason">{attempt.error.message}</p>
        ) : null}
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
  const project = useQuery(projectsQuery).data?.find((p) => p.id === run.project);
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

  const paramLabels = new Map(action?.params.map((p) => [p.name, p.label]) ?? []);
  const paramTypes = new Map(action?.params.map((p) => [p.name, p.type]) ?? []);
  const inputLabels = new Map(action?.inputs.map((i) => [i.name, i.label]) ?? []);
  const metricLabels = new Map(action?.metrics.map((m) => [m.name, m.label]) ?? []);
  const params = Object.entries(run.params);
  const metrics = Object.entries(run.metrics);
  const items = attempts.data ?? [];
  return (
    <>
      <PageHeader
        eyebrow={
          project ? (
            <>
              <Link to="/projects/$slug/runs/" params={{ slug: project.slug }}>
                {project.name}
              </Link>
              {" · Run"}
            </>
          ) : (
            "Run"
          )
        }
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
      {run.waiting ? (
        <div className="alert alert-info" role="status">
          <p>Waiting to start: {run.waiting}</p>
        </div>
      ) : null}
      {run.needs_worker ? <NeedsWorker whose={run.needs_worker} run={run} /> : null}
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
                  <dd>{value}</dd>
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
              />
            ))}
          </ul>
        ) : null}
      </section>

      <section className="card" aria-labelledby="inputs-title">
        <h2 className="section-title" id="inputs-title">
          Inputs
        </h2>
        {run.inputs.length === 0 && params.length === 0 ? (
          <p className="muted">It took no inputs.</p>
        ) : null}
        <dl className="summary">
          {run.inputs.map((input) => (
            <div key={`${input.name}-${input.entity}`} className="value-row">
              <dt>{inputLabels.get(input.name) ?? input.name}</dt>
              <dd>
                <Link to="/entities/$ref/" params={{ ref: input.hrid }}>
                  {input.hrid}
                </Link>{" "}
                <span className="muted">as of revision {input.revision}</span>
              </dd>
            </div>
          ))}
          {params.map(([name, value]) => (
            <div key={name} className="value-row">
              <dt>{paramLabels.get(name) ?? name}</dt>
              <dd>
                <ParamValue run={run} type={paramTypes.get(name)} value={value} />
              </dd>
            </div>
          ))}
        </dl>
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
              <dt>Ran</dt>
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
                May reach{" "}
                {run.network.map((host, index) => (
                  <span key={host}>
                    {index ? ", " : ""}
                    <code>{host}</code>
                  </span>
                ))}
                , through Iki’s egress proxy
              </>
            ) : (
              "None: it couldn’t reach the internet"
            )}
          </dd>
          <dt>Ran on</dt>
          <dd>
            {run.tier === "client" ? "A client" : `A ${run.tier} worker`}
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
  return (
    <>
      {money(cost.amount, cost.currency)}
      {cost.final ? "" : " so far"} <span className="muted">for {time} of machine time</span>
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
  if (artifact.state === "deleted" && artifact.deleted_at) {
    return (
      <span className="muted">
        Deleted <Time value={artifact.deleted_at} />, its retention over
      </span>
    );
  }
  if (artifact.state === "uploading") return <span className="muted">Uploading…</span>;
  if (artifact.state === "failed") return <span className="muted">Didn’t finish uploading</span>;
  return (
    <>
      Until <Time value={artifact.expires_at} />
    </>
  );
}
