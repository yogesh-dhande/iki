/** Compute admin's data: what compute is doing now. */

import { unwrap, type Schemas } from "@ikisci/sdk";
import { infiniteQueryOptions, queryOptions } from "@tanstack/react-query";

import { iki } from "./api";

export type ComputeOverview = Schemas["OverviewOut"];
export type Tier = "client" | "backend" | "container" | "vm";

export const COMPUTE = ["compute"] as const;

/** How often the overview is read again while it's open. */
export const OVERVIEW_POLL_MS = 15_000;

export const overviewQuery = queryOptions({
  queryKey: [...COMPUTE, "overview"],
  queryFn: async () => unwrap(await iki.GET("/api/v1/compute/overview")),
  refetchInterval: OVERVIEW_POLL_MS,
});

/** Each tier by name, as Compute admins call them. */
export const TIER_NAME: Record<string, string> = {
  client: "Client",
  backend: "Backend",
  container: "Container",
  vm: "VM",
};

/** What each tier's workers are, for an empty one's explanation. */
export const TIER_WORKERS: Record<string, string> = {
  client: "People’s own computers, running Iki’s worker",
  backend: "Workers you run and register with a token",
  container: "Pods Iki starts for runs, on its compute templates",
  vm: "Machines Iki starts for runs, on its compute templates",
};

export type Worker = Schemas["WorkerOut"];
export type Registration = Schemas["WorkerRegistrationOut"];
export type RegistrationCreated = Schemas["WorkerRegistrationCreatedOut"];

export interface WorkerFilter {
  tier: "all" | Tier;
  revoked: boolean;
}

/** Registered workers, newest first, a page at a time ("Show more"). */
export function workersQuery(filter: WorkerFilter) {
  return infiniteQueryOptions({
    queryKey: [...COMPUTE, "workers", filter],
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam }) =>
      unwrap(
        await iki.GET("/api/v1/compute/workers", {
          params: {
            query: {
              limit: 50,
              ...(filter.tier === "all" ? {} : { tier: filter.tier }),
              ...(filter.revoked ? { revoked: true } : {}),
              ...(pageParam ? { cursor: pageParam } : {}),
            },
          },
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    refetchInterval: OVERVIEW_POLL_MS,
  });
}

/** Registration tokens, newest first, revoked ones included: few enough for one page. */
export const registrationsQuery = queryOptions({
  queryKey: [...COMPUTE, "registration-tokens"],
  queryFn: async () =>
    unwrap(
      await iki.GET("/api/v1/compute/registration-tokens", { params: { query: { limit: 100 } } }),
    ).items,
});
