/**
 * Environments' volumes and the setup builds that fill them (ADR 0075): what Compute
 * admins see and do under Compute → Volumes.
 */

import { unwrap, type Schemas } from "@ikisci/sdk";
import { queryOptions, useQueryClient } from "@tanstack/react-query";

import { iki } from "./api";
import { useStream } from "./runs";

export type EnvironmentVolumes = Schemas["EnvironmentVolumesOut"];
export type SetupBuild = Schemas["SetupBuildOut"];
export type SetupState = SetupBuild["state"];

export const VOLUMES = ["volumes"] as const;

export const volumesQuery = queryOptions({
  queryKey: [...VOLUMES, "environments"],
  queryFn: async () => unwrap(await iki.GET("/api/v1/compute/volumes")),
});

/** How often the builds are read again while one hasn't finished. */
export const BUILD_POLL_MS = 5_000;

export function setupsQuery(environment: string) {
  return queryOptions({
    queryKey: [...VOLUMES, "builds", environment],
    queryFn: async () =>
      unwrap(
        await iki.GET("/api/v1/compute/setups", {
          params: { query: { environment, limit: 20 } },
        }),
      ).items,
    refetchInterval: (query) =>
      (query.state.data ?? []).some((b) => !FINISHED.has(b.state)) ? BUILD_POLL_MS : false,
  });
}

export function setupQuery(ref: string) {
  return queryOptions({
    queryKey: [...VOLUMES, "build", ref],
    queryFn: async () =>
      unwrap(await iki.GET("/api/v1/compute/setups/{ref}", { params: { path: { ref } } })),
  });
}

export async function readSetupLog(ref: string, offset: number) {
  return unwrap(
    await iki.GET("/api/v1/compute/setups/{ref}/log", {
      params: { path: { ref }, query: { offset } },
    }),
  );
}

export const FINISHED: ReadonlySet<SetupState> = new Set([
  "succeeded",
  "failed",
  "timed_out",
  "cancelled",
]);

export const SETUP_STATE: Record<SetupState, string> = {
  awaiting_approval: "Awaiting approval",
  queued: "Queued",
  running: "Running",
  snapshotting: "Snapshotting",
  succeeded: "Succeeded",
  failed: "Failed",
  timed_out: "Timed out",
  cancelled: "Cancelled",
};

export function setupTone(state: SetupState): "good" | "bad" | "busy" | "quiet" {
  switch (state) {
    case "succeeded":
      return "good";
    case "failed":
    case "timed_out":
      return "bad";
    case "cancelled":
    case "awaiting_approval":
      return "quiet";
    default:
      return "busy";
  }
}

/** Seconds, as a setup's timeout reads: "12 h", "10 min", "90 s". */
export function timeoutText(seconds: number): string {
  if (seconds % 3600 === 0) return `${String(seconds / 3600)} h`;
  if (seconds % 60 === 0) return `${String(seconds / 60)} min`;
  return `${String(seconds)} s`;
}

/**
 * Keeps a build current while it's going: its stream says when its state, progress or
 * log changed, and the build (and the environment's builds and versions) are read again.
 */
export function useSetupUpdates(buildId: string, enabled: boolean): void {
  const client = useQueryClient();
  useStream(
    `/api/v1/compute/setups/${buildId}/events`,
    enabled,
    () => client.invalidateQueries({ queryKey: VOLUMES }),
    undefined,
    SETUP_CHANGES,
  );
}

const SETUP_CHANGES = ["setup"] as const;
