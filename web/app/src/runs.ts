/**
 * Action runs, as the app reads them: a project's runs, one run, its
 * attempts and their logs, batches of runs, and live updates about a run or
 * a batch while it's going.
 */

import { unwrap, type Schemas } from "@ikisci/sdk";
import { infiniteQueryOptions, queryOptions, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import { iki, isSignedOut, markSignedOut } from "./api";
import { HIDDEN_GRACE_MS, REFUSED_RETRY_MS } from "./events";

export type Run = Schemas["RunOut"];
export type RunStatus = Run["status"];
export type Attempt = Schemas["RunAttemptOut"];
export type AttemptState = Attempt["state"];
export type Action = Schemas["ActionOut"];
export type Batch = Schemas["BatchOut"];
export type Workflow = Schemas["WorkflowOut"];
export type WorkflowRun = Schemas["WorkflowRunOut"];
export type WorkflowStatus = WorkflowRun["status"];
export type StepState = WorkflowRun["steps"][number]["state"];

export const RUNS = ["runs"] as const;

export const STATUS: Record<RunStatus, string> = {
  awaiting_approval: "Awaiting approval",
  queued: "Queued",
  running: "Running",
  succeeded: "Succeeded",
  failed: "Failed",
  cancelled: "Cancelled",
  timed_out: "Timed out",
  rejected: "Rejected",
  declined: "Declined",
};

export const ATTEMPT_STATE: Record<AttemptState, string> = {
  awaiting_approval: "Awaiting approval",
  queued: "Queued",
  leased: "Starting",
  staging: "Staging inputs",
  running: "Running",
  uploading: "Uploading results",
  committing: "Committing results",
  succeeded: "Succeeded",
  failed: "Failed",
  cancelled: "Cancelled",
  timed_out: "Timed out",
  rejected: "Rejected",
  interrupted: "Interrupted",
};

/** How a status reads at a glance: its badge's tone. */
export function tone(status: RunStatus | AttemptState): "good" | "bad" | "busy" | "quiet" {
  switch (status) {
    case "succeeded":
      return "good";
    case "failed":
    case "timed_out":
    case "rejected":
      return "bad";
    case "cancelled":
    case "declined":
    case "interrupted":
      return "quiet";
    default:
      return "busy";
  }
}

export function finished(status: RunStatus): boolean {
  return !(status === "awaiting_approval" || status === "queued" || status === "running");
}

/** The actions installed here, for their labels. */
export const actionsQuery = queryOptions({
  queryKey: ["actions"],
  queryFn: async () => unwrap(await iki.GET("/api/v1/actions")),
  staleTime: 5 * 60_000,
});

/** The actions with an input that takes entities of every one of `types`. */
export function actionsTakingQuery(types: string[]) {
  const sorted = [...types].sort();
  return queryOptions({
    queryKey: ["actions", "taking", ...sorted],
    queryFn: async () =>
      unwrap(await iki.GET("/api/v1/actions", { params: { query: { takes: sorted } } })),
    staleTime: 5 * 60_000,
  });
}

/**
 * The most entities one launch here takes. The API takes up to 1000 in a batch, but the
 * app names them in the new run's address, which proxies may refuse past about 8 KB.
 */
export const MAX_BATCH = 200;

/** Entities by HRID, as a new run's address names them. */
export function entitiesByHridQuery(hrids: string[]) {
  return queryOptions({
    queryKey: ["entities", "by-hrid", ...hrids],
    queryFn: async () => {
      const found: Schemas["EntityOut"][] = [];
      let cursor: string | undefined;
      do {
        const page = unwrap(
          await iki.POST("/api/v1/entities/search", {
            body: {
              where: { field: "hrid", op: "in", value: hrids },
              exact_type: false,
              limit: 100,
              ...(cursor ? { cursor } : {}),
            },
          }),
        );
        found.push(...page.items);
        cursor = page.next_cursor ?? undefined;
      } while (cursor);
      return found;
    },
  });
}

/** A label for an action's qualified name, or the name while they load (or if it's gone). */
export function actionLabel(actions: Action[] | undefined, name: string): string {
  return actions?.find((a) => a.name === name)?.label ?? name;
}

/** A project's runs, newest first, a page at a time ("Show older"). */
export function projectRunsQuery(projectId: string, status: RunStatus | undefined) {
  return infiniteQueryOptions({
    queryKey: [...RUNS, "list", projectId, status ?? "all"],
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam }) =>
      unwrap(
        await iki.GET("/api/v1/runs", {
          params: {
            query: {
              project: projectId,
              limit: 25,
              ...(status ? { status } : {}),
              ...(pageParam ? { cursor: pageParam } : {}),
            },
          },
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
}

/** The workflows installed plugins provide (ADR 0079). */
export const workflowsQuery = queryOptions({
  queryKey: ["workflows"],
  queryFn: async () => unwrap(await iki.GET("/api/v1/workflows")),
  staleTime: 5 * 60_000,
});

/** A project's workflow runs, newest first, a page at a time. */
export function projectWorkflowRunsQuery(projectId: string, status: WorkflowStatus | undefined) {
  return infiniteQueryOptions({
    queryKey: [...RUNS, "list", "workflows", projectId, status ?? "all"],
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam }) =>
      unwrap(
        await iki.GET("/api/v1/workflow-runs", {
          params: {
            query: {
              project: projectId,
              limit: 25,
              ...(status ? { status } : {}),
              ...(pageParam ? { cursor: pageParam } : {}),
            },
          },
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
}

/** By ID or HRID, as in the page's address. */
export function runQuery(ref: string) {
  return queryOptions({
    queryKey: [...RUNS, "one", ref.toUpperCase()],
    queryFn: async () => unwrap(await iki.GET("/api/v1/runs/{ref}", { params: { path: { ref } } })),
  });
}

/** A batch, by ID or HRID (BAT-000123). */
export function batchQuery(ref: string) {
  return queryOptions({
    queryKey: [...RUNS, "batch", ref.toUpperCase()],
    queryFn: async () =>
      unwrap(await iki.GET("/api/v1/batches/{ref}", { params: { path: { ref } } })),
  });
}

/** A workflow run, by ID or HRID (WF-000123) (ADR 0079). */
export function workflowRunQuery(ref: string) {
  return queryOptions({
    queryKey: [...RUNS, "workflow", ref.toUpperCase()],
    queryFn: async () =>
      unwrap(await iki.GET("/api/v1/workflow-runs/{ref}", { params: { path: { ref } } })),
  });
}

export const STEP_STATE: Record<StepState, string> = {
  waiting: "Waiting",
  awaiting_approval: "Awaiting approval",
  running: "Running",
  succeeded: "Succeeded",
  failed: "Failed",
  cancelled: "Cancelled",
  skipped: "Skipped",
  declined: "Declined",
};

/** A batch's runs, in the order it launched them, a page at a time. */
export function batchRunsQuery(ref: string, status?: RunStatus) {
  return infiniteQueryOptions({
    queryKey: [...RUNS, "list", "batch", ref.toUpperCase(), status ?? "all"],
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam }) =>
      unwrap(
        await iki.GET("/api/v1/runs", {
          params: {
            query: {
              batch: ref,
              limit: 100,
              ...(status ? { status } : {}),
              ...(pageParam ? { cursor: pageParam } : {}),
            },
          },
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
}

/** The rest of a run's effects, after those its answer lists (`effects_next_cursor`). */
export function moreEffectsQuery(ref: string, after: string) {
  return infiniteQueryOptions({
    queryKey: [...RUNS, "effects", ref.toUpperCase(), after],
    initialPageParam: after,
    queryFn: async ({ pageParam }) =>
      unwrap(
        await iki.GET("/api/v1/runs/{ref}/effects", {
          params: { path: { ref }, query: { limit: 100, cursor: pageParam } },
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
}

export function attemptsQuery(ref: string) {
  return queryOptions({
    queryKey: [...RUNS, "attempts", ref.toUpperCase()],
    queryFn: async () =>
      unwrap(await iki.GET("/api/v1/runs/{ref}/attempts", { params: { path: { ref } } })),
  });
}

export type Artifact = Schemas["RunArtifactOut"];

/** Files the run's command wrote that its action keeps (ADR 0068). */
export function artifactsQuery(ref: string) {
  return queryOptions({
    queryKey: [...RUNS, "artifacts", ref.toUpperCase()],
    queryFn: async () =>
      unwrap(await iki.GET("/api/v1/runs/{ref}/artifacts", { params: { path: { ref } } })),
  });
}

export function artifactDownloadUrl(ref: string, id: string): string {
  return `/api/v1/runs/${encodeURIComponent(ref)}/artifacts/${encodeURIComponent(id)}/content`;
}

/** Past this, the server counts a log's bytes but doesn't keep them. */
export const LOG_KEPT_BYTES = 16 * 1024 ** 2;

export async function readLog(ref: string, attempt: number, offset: number) {
  return unwrap(
    await iki.GET("/api/v1/runs/{ref}/attempts/{attempt}/log", {
      params: { path: { ref, attempt }, query: { offset } },
    }),
  );
}

export function logDownloadUrl(ref: string, attempt: number): string {
  return `/api/v1/runs/${encodeURIComponent(ref)}/attempts/${String(attempt)}/log.txt`;
}

export function runEventsUrl(ref: string): string {
  return `/api/v1/runs/${encodeURIComponent(ref)}/events`;
}

export function batchEventsUrl(ref: string): string {
  return `/api/v1/batches/${encodeURIComponent(ref)}/events`;
}

export function workflowEventsUrl(ref: string): string {
  return `/api/v1/workflow-runs/${encodeURIComponent(ref)}/events`;
}

function parse(event: MessageEvent<string>): Record<string, unknown> {
  try {
    const data: unknown = JSON.parse(event.data);
    return data && typeof data === "object" ? (data as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

/**
 * Keeps a run's page current while the run is going: one EventSource on the
 * run's events, signed in with the browser session.
 *
 * - `run` (its status, an attempt or progress changed) reads the run and its
 *   attempts again.
 * - `log` says how big an attempt's log is now; `onLog` fetches what's new.
 */
export function useRunUpdates(
  ref: string,
  enabled: boolean,
  onLog: (attempt: number, size: number) => void,
): void {
  const client = useQueryClient();
  const logged = useRef(onLog);
  useEffect(() => {
    logged.current = onLog;
  });
  useStream(
    runEventsUrl(ref),
    enabled,
    () =>
      Promise.all([
        client.invalidateQueries({ queryKey: runQuery(ref).queryKey }),
        client.invalidateQueries({ queryKey: attemptsQuery(ref).queryKey }),
      ]),
    (event, data) => {
      const attempt = data["attempt"];
      const size = data["size"];
      if (event === "log" && typeof attempt === "number" && typeof size === "number") {
        logged.current(attempt, size);
      }
    },
  );
}

/**
 * Keeps a batch's page current while its runs are going: each `run` event (one of its
 * runs started or ended) reads the batch and its runs again, at most every half second.
 */
export function useBatchUpdates(ref: string, enabled: boolean): void {
  const client = useQueryClient();
  const pending = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(
    () => () => {
      clearTimeout(pending.current);
    },
    [],
  );
  useStream(batchEventsUrl(ref), enabled, () => {
    // A pack's runs start and end together: one read for them all.
    pending.current ??= setTimeout(() => {
      pending.current = undefined;
      void Promise.all([
        client.invalidateQueries({ queryKey: batchQuery(ref).queryKey }),
        client.invalidateQueries({ queryKey: [...RUNS, "list", "batch", ref.toUpperCase()] }),
      ]);
    }, BATCH_REFRESH_MS);
    return Promise.resolve();
  });
}

/**
 * Keeps a workflow run's page current while it's going: each `workflow` event (it or one
 * of its steps changed) reads it again.
 */
export function useWorkflowUpdates(ref: string, enabled: boolean): void {
  const client = useQueryClient();
  useStream(
    workflowEventsUrl(ref),
    enabled,
    () => client.invalidateQueries({ queryKey: workflowRunQuery(ref).queryKey }),
    undefined,
    ["workflow"],
  );
}

/** How long a batch's page gathers news of its runs before reading them again. */
export const BATCH_REFRESH_MS = 500;

/**
 * One EventSource on `url` while `enabled`, signed in with the browser session.
 *
 * - `run` and `resync` call `refresh`; other events go to `onEvent`.
 * - Every `ready` after the first means something may have been missed while
 *   it was reconnecting, so `refresh` is called.
 * - Like the app's own stream, a tab hidden for a while closes it, and one the
 *   server refused tries again later.
 */
export function useStream(
  url: string,
  enabled: boolean,
  refresh: () => Promise<unknown>,
  onEvent?: (event: string, data: Record<string, unknown>) => void,
  /** The events that call `refresh`, besides `resync`. */
  changes: readonly string[] = ["run"],
): void {
  const handlers = useRef({ refresh, onEvent });
  useEffect(() => {
    handlers.current = { refresh, onEvent };
  });
  // A string, so a new array each render doesn't open the stream again.
  const kinds = changes.join(" ");
  useEffect(() => {
    if (!enabled || typeof EventSource === "undefined") return;
    let source: EventSource | undefined;
    let hiddenTimer: ReturnType<typeof setTimeout> | undefined;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    let readyBefore = false;
    const reread = () => void handlers.current.refresh();

    const close = () => {
      source?.close();
      source = undefined;
    };

    const open = () => {
      if (source || isSignedOut()) return;
      const stream = new EventSource(url, { withCredentials: true });
      source = stream;
      stream.addEventListener("ready", () => {
        if (readyBefore) reread();
        readyBefore = true;
      });
      for (const change of kinds.split(" ")) stream.addEventListener(change, reread);
      stream.addEventListener("resync", reread);
      stream.addEventListener("log", (event: MessageEvent<string>) => {
        handlers.current.onEvent?.("log", parse(event));
      });
      stream.addEventListener("signed_out", () => {
        close();
        markSignedOut();
      });
      stream.addEventListener("error", () => {
        // CONNECTING: the browser is reconnecting by itself. CLOSED: it gave up.
        if (stream.readyState !== EventSource.CLOSED || source !== stream) return;
        close();
        readyBefore = true;
        retryTimer = setTimeout(open, REFUSED_RETRY_MS * (1 + Math.random() / 2));
      });
    };

    const onVisibility = () => {
      clearTimeout(hiddenTimer);
      if (document.visibilityState === "hidden") {
        hiddenTimer = setTimeout(close, HIDDEN_GRACE_MS);
      } else if (!source) {
        clearTimeout(retryTimer);
        readyBefore = true;
        open();
      }
    };

    open();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      clearTimeout(hiddenTimer);
      clearTimeout(retryTimer);
      close();
    };
  }, [url, enabled, kinds]);
}
