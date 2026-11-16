/** Compute admin's data: what compute is doing now. */

import { unwrap, type Schemas } from "@ikisci/sdk";
import { queryOptions } from "@tanstack/react-query";

import { iki } from "./api";

export type ComputeOverview = Schemas["OverviewOut"];
export type Tier = ComputeOverview["workers"][number]["tier"];

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
