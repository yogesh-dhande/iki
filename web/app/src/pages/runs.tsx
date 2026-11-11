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

import { iki, problemOf, projectsQuery, useApiMutation } from "../api";
import { ENTITIES, formatSize, typeLabel, typesQuery } from "../entities";
import { usePageTitle } from "../layout";
import {
  actionLabel,
  actionsQuery,
  ATTEMPT_STATE,
  attemptsQuery,
  finished,
  LOG_KEPT_BYTES,
  logDownloadUrl,
  projectRunsQuery,
  readLog,
  RUNS,
  runQuery,
  STATUS,
  tone,
  useRunUpdates,
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
  useMessages,
} from "../ui";
import { useProject } from "./project-overview";

export const VIA: Record<string, string> = {
  web: "in the web app",
  api: "through the API",
  mcp: "by an AI assistant (MCP)",
  cli: "from the command line",
  system: "by Iki",
};

const EFFECT: Record<Run["effects"][number]["kind"], string> = {
  created: "Created",
  updated: "Updated",
  reused: "Already existed",
};

export function StatusBadge({ status }: { status: RunStatus | Attempt["state"] }) {
  const label = status in STATUS ? STATUS[status as RunStatus] : ATTEMPT_STATE[status];
  return <span className={`badge badge-${tone(status)}`}>{label}</span>;
}

// --- A project's runs ------------------------------------------------------------------

const STATUS_CHOICES = [
  { value: "all", label: "Every status" },
  ...Object.entries(STATUS).map(([value, label]) => ({ value, label })),
] as const;

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
                  {run.hrid} · {run.requested_by.name} · <RelativeTime value={run.created_at} />
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
  const types = useQuery(typesQuery).data;
  const project = useQuery(projectsQuery).data?.find((p) => p.id === run.project);
  const attempts = useQuery(attemptsQuery(run.hrid));
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
      void client.invalidateQueries({ queryKey: [...RUNS, "list"] });
    }
    wasDone.current = done;
  }, [done, client, run.hrid]);

  const paramLabels = new Map(action?.params.map((p) => [p.name, p.label]) ?? []);
  const inputLabels = new Map(action?.inputs.map((i) => [i.name, i.label]) ?? []);
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
        actions={<CancelRun run={run} />}
      />
      {run.cancel_requested_at && !done ? (
        <div className="alert alert-warning" role="status">
          <p>
            Cancelling: asked to stop <RelativeTime value={run.cancel_requested_at} />. Its worker
            ends it.
          </p>
        </div>
      ) : null}
      {run.waiting ? (
        <div className="alert alert-info" role="status">
          <p>Waiting to start: {run.waiting}</p>
        </div>
      ) : null}
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
          {run.effects.length === 0 ? (
            <p className="muted">It changed no entities.</p>
          ) : (
            <ul className="value-list">
              {run.effects.map((effect) => (
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
          )}
          {metrics.length ? (
            <dl className="summary">
              {metrics.map(([name, value]) => (
                <div key={name} className="value-row">
                  <dt>{name}</dt>
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
                <span className="preserve-lines">{m.text}</span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

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
              <dd>{paramText(value)}</dd>
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
          </dd>
          {run.reason ? (
            <>
              <dt>Reason</dt>
              <dd className="preserve-lines">{run.reason}</dd>
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
          <dt>Ran on</dt>
          <dd>
            {run.tier === "client" ? "A client" : `A ${run.tier} worker`}
            {run.environment ? (
              <>
                , in the <code>{run.environment}</code> environment
              </>
            ) : null}
          </dd>
        </dl>
      </section>
    </>
  );
}
