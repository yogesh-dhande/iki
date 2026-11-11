/**
 * Action runs, as the app reads them: a project's runs, one run, its
 * attempts and their logs, and live updates about a run while it's going.
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

export const RUNS = ["runs"] as const;

export const STATUS: Record<RunStatus, string> = {
  queued: "Queued",
  running: "Running",
  succeeded: "Succeeded",
  failed: "Failed",
  cancelled: "Cancelled",
  timed_out: "Timed out",
  rejected: "Rejected",
};

export const ATTEMPT_STATE: Record<AttemptState, string> = {
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
    case "interrupted":
      return "quiet";
    default:
      return "busy";
  }
}

export function finished(status: RunStatus): boolean {
  return !(status === "queued" || status === "running");
}

/** The actions installed here, for their labels. */
export const actionsQuery = queryOptions({
  queryKey: ["actions"],
  queryFn: async () => unwrap(await iki.GET("/api/v1/actions")),
  staleTime: 5 * 60_000,
});

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

/** By ID or HRID, as in the page's address. */
export function runQuery(ref: string) {
  return queryOptions({
    queryKey: [...RUNS, "one", ref.toUpperCase()],
    queryFn: async () => unwrap(await iki.GET("/api/v1/runs/{ref}", { params: { path: { ref } } })),
  });
}

export function attemptsQuery(ref: string) {
  return queryOptions({
    queryKey: [...RUNS, "attempts", ref.toUpperCase()],
    queryFn: async () =>
      unwrap(await iki.GET("/api/v1/runs/{ref}/attempts", { params: { path: { ref } } })),
  });
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
 * - Every `ready` after the first means something may have been missed while
 *   it was reconnecting, so everything is read again.
 * - Like the app's own stream, a tab hidden for a while closes it, and one the
 *   server refused tries again later.
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
  useEffect(() => {
    if (!enabled || typeof EventSource === "undefined") return;
    let source: EventSource | undefined;
    let hiddenTimer: ReturnType<typeof setTimeout> | undefined;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    let readyBefore = false;

    const refresh = () =>
      Promise.all([
        client.invalidateQueries({ queryKey: runQuery(ref).queryKey }),
        client.invalidateQueries({ queryKey: attemptsQuery(ref).queryKey }),
      ]);

    const close = () => {
      source?.close();
      source = undefined;
    };

    const open = () => {
      if (source || isSignedOut()) return;
      const stream = new EventSource(runEventsUrl(ref), { withCredentials: true });
      source = stream;
      stream.addEventListener("ready", () => {
        if (readyBefore) void refresh();
        readyBefore = true;
      });
      stream.addEventListener("run", () => void refresh());
      stream.addEventListener("resync", () => void refresh());
      stream.addEventListener("log", (event: MessageEvent<string>) => {
        const data = parse(event);
        const attempt = data["attempt"];
        const size = data["size"];
        if (typeof attempt === "number" && typeof size === "number") {
          logged.current(attempt, size);
        }
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
  }, [client, ref, enabled]);
}
