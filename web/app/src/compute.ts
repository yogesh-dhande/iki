/** Compute admin's data: what compute is doing now. */

import { unwrap, type Schemas } from "@ikisci/sdk";
import { infiniteQueryOptions, queryOptions } from "@tanstack/react-query";

import { everyPage, iki } from "./api";
import { machineTime } from "./costs";
import { formatSize } from "./entities";

export type ComputeOverview = Schemas["OverviewOut"];
export type Tier = "self_hosted" | "backend" | "container" | "vm";

export const COMPUTE = ["compute"] as const;

/** How often the overview is read again while it's open. */
export const OVERVIEW_POLL_MS = 15_000;

export const overviewQuery = queryOptions({
  queryKey: [...COMPUTE, "overview"],
  queryFn: async ({ signal }) => unwrap(await iki.GET("/api/v1/compute/overview", { signal })),
  refetchInterval: OVERVIEW_POLL_MS,
});

export type ImageBindings = Schemas["ImageBindingsOut"];
export type EnvironmentImage = Schemas["EnvironmentImageOut"];
export type UnusedImageBinding = Schemas["UnusedImageBindingOut"];

/** The environments that need an image from this deployment, and compute.yaml's pins. */
export const imageBindingsQuery = queryOptions({
  queryKey: [...COMPUTE, "image-bindings"],
  queryFn: async ({ signal }) =>
    unwrap(await iki.GET("/api/v1/compute/image-bindings", { signal })),
});

/** An image reference with its digest cut to its first 12 hex digits, as `docker images`
 * shows IDs. A reference without a digest is shown whole. */
export function shortImage(image: string): string {
  const at = image.lastIndexOf("@sha256:");
  if (at < 0) return image;
  const digest = image.slice(at + "@sha256:".length);
  return digest.length > 12 ? `${image.slice(0, at)}@sha256:${digest.slice(0, 12)}…` : image;
}

/** A plugin version's release numbers, as compute.yaml's comparisons read them: `0.4`
 * is 0.4.0, and so is `0.4.0-rc.1`. None if it doesn't start with any. */
function release(version: string): [number, number, number] | null {
  const match = /^\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?/.exec(version);
  if (!match) return null;
  return [Number(match[1]), Number(match[2] ?? 0), Number(match[3] ?? 0)];
}

/** The example binding's range: the installed release alone. In a validated lab a plugin
 * upgrade is a change, and the image is qualified again against the new release. */
export function exactVersion(version: string): string {
  const numbers = release(version);
  return `==${numbers ? numbers.join(".") : version}`;
}

/** A range that keeps the image across releases, for a lab that has qualified it so: the
 * installed minor release's patches before 1.0, and the rest of its major release after. */
export function compatibleRange(version: string): string {
  const numbers = release(version);
  if (!numbers) return exactVersion(version);
  const [major, minor] = numbers;
  return major === 0
    ? `>=0.${String(minor)},<0.${String(minor + 1)}`
    : `>=${String(major)}.${String(minor)},<${String(major + 1)}`;
}

/** The binding a Compute admin would add to compute.yaml for `environment`. */
export function exampleBinding(environment: EnvironmentImage): string {
  return [
    "apiVersion: iki/v1",
    "kind: EnvironmentImageBinding",
    `environment: ${environment.environment}`,
    `plugin_version: "${exactVersion(environment.installed_version)}"`,
    "image: <registry>/<repository>@sha256:<digest>",
    "pull_secret: <secret name>  # Optional: Kubernetes templates only",
  ].join("\n");
}

/** Each tier by name, as Compute admins call them. */
export const TIER_NAME: Record<string, string> = {
  self_hosted: "Self-hosted",
  backend: "Backend",
  container: "Container",
  vm: "VM",
};

/** What each tier's workers are, for an empty one's explanation. */
export const TIER_WORKERS: Record<string, string> = {
  self_hosted: "Teams’ own machines, each registered with a token for its teams",
  backend: "Workers you run and register with a token",
  container: "Pods Iki starts for runs, on its compute templates, and Slurm clusters’ login nodes",
  vm: "Machines Iki starts for runs, on its compute templates",
};

const TIERS: readonly Tier[] = ["self_hosted", "backend", "container", "vm"];

/** Compute → Workers' address: ?tier=backend shows only that tier's workers. */
export function validateWorkersSearch(search: Record<string, unknown>): { tier?: Tier } {
  const tier = TIERS.find((t) => t === search["tier"]);
  return tier ? { tier } : {};
}

/** "chromeleon, lab-3" (or spaces) as labels. The server checks and sorts them. */
export function parseLabels(text: string): string[] {
  return text
    .split(/[\s,]+/)
    .map((label) => label.trim().toLowerCase())
    .filter(Boolean);
}

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
    queryFn: async ({ pageParam, signal }) =>
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
          signal,
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    refetchInterval: OVERVIEW_POLL_MS,
  });
}

/** Registration tokens, newest first, revoked ones included: few enough for one page. */
export const registrationsQuery = queryOptions({
  queryKey: [...COMPUTE, "registration-tokens"],
  queryFn: async ({ signal }) =>
    unwrap(
      await iki.GET("/api/v1/compute/registration-tokens", {
        params: { query: { limit: 100 } },
        signal,
      }),
    ).items,
});

export type Quota = Schemas["QuotaOut"];
export type QuotaScopeKind = "team" | "project";

/** Every quota, the organization's first, with how much of each is in use now. */
export const quotasQuery = queryOptions({
  queryKey: [...COMPUTE, "quotas"],
  queryFn: ({ signal }) =>
    everyPage(async (page) =>
      unwrap(await iki.GET("/api/v1/compute/quotas", { params: { query: page }, signal })),
    ),
  refetchInterval: OVERVIEW_POLL_MS,
});

/** How many teams or projects a picker of them lists: the best matches for what's typed. */
const SCOPES_SHOWN = 20;

/**
 * Teams or projects to set a quota on or give a self-hosted worker to, any of them, by part
 * of the name; projects only of `teams`, when given. The first {@link SCOPES_SHOWN} that
 * match, for a picker that narrows as people type. No placeholder: until the answer for
 * `search` comes, none are listed, never another search's, which would change under someone
 * choosing from them.
 */
export function quotaScopesQuery(kind: QuotaScopeKind, search: string, teams: string[] = []) {
  return queryOptions({
    queryKey: [...COMPUTE, "quota-scopes", kind, search.trim(), teams],
    queryFn: async ({ signal }) =>
      unwrap(
        await iki.GET("/api/v1/compute/quota-scopes", {
          params: {
            query: {
              kind,
              q: search.trim(),
              limit: SCOPES_SHOWN,
              ...(teams.length ? { team: teams } : {}),
            },
          },
          signal,
        }),
      ).items,
    staleTime: 30_000,
  });
}

/** "8 CPUs, 32 GiB, 1 GPU (80 GiB)": what one job may use on a worker or template. */
export function capacityText(capacity: Record<string, unknown>): string {
  const parts: string[] = [];
  const cpu = Number(capacity["cpu"] ?? 0);
  if (cpu) parts.push(`${String(cpu)} CPU${cpu === 1 ? "" : "s"}`);
  const memory = Number(capacity["memory"] ?? 0);
  if (memory) parts.push(formatSize(memory));
  const gpus = Number(capacity["gpu_count"] ?? 0);
  if (gpus) {
    const each = Number(capacity["gpu_memory"] ?? 0);
    parts.push(
      `${String(gpus)} GPU${gpus === 1 ? "" : "s"}${each ? ` (${formatSize(each)})` : ""}`,
    );
  }
  return parts.join(", ") || "—";
}

// --- Slurm clusters (ADR 0133) ----------------------------------------------------------

export type SlurmCluster = Schemas["SlurmClusterOut"];
export type SlurmTemplate = Schemas["SlurmTemplateOut"];
export type SlurmAccount = Schemas["SlurmAccountOut"];
export type AttemptSlurm = Schemas["AttemptSlurmOut"];

export const SLURM = [...COMPUTE, "slurm"] as const;

/** The clusters the deployment's Slurm templates name, with their workers and accounts. */
export const slurmClustersQuery = queryOptions({
  queryKey: SLURM,
  queryFn: ({ signal }) =>
    everyPage(async (page) =>
      unwrap(await iki.GET("/api/v1/compute/slurm-clusters", { params: { query: page }, signal })),
    ),
  refetchInterval: OVERVIEW_POLL_MS,
});

/** The projects given their own account on `cluster`, newest first, a page at a time. */
export function slurmAccountsQuery(cluster: string) {
  return infiniteQueryOptions({
    queryKey: [...SLURM, cluster, "accounts"],
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam, signal }) =>
      unwrap(
        await iki.GET("/api/v1/compute/slurm-clusters/{cluster}/accounts", {
          params: {
            path: { cluster },
            query: { limit: 50, ...(pageParam ? { cursor: pageParam } : {}) },
          },
          signal,
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
}

/** An account as `sacctmgr` names one: what the API takes. */
export const SLURM_ACCOUNT = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;

/** A Slurm time limit: "7 days", "12 h", "90 min". */
export function walltimeText(seconds: number): string {
  const days = seconds / 86_400;
  if (Number.isInteger(days) && days >= 1) return `${String(days)} day${days === 1 ? "" : "s"}`;
  return machineTime(seconds);
}

/** "8 CPUs, 32 GiB, 1 GPU, 100 GiB of scratch disk, for up to 12 h": what a job asked for. */
export function slurmRequestText(requested: Record<string, unknown>): string {
  const parts: string[] = [];
  const cpus = Number(requested["cpus"] ?? 0);
  if (cpus) parts.push(`${String(cpus)} CPU${cpus === 1 ? "" : "s"}`);
  const memory = Number(requested["memory"] ?? 0);
  if (memory) parts.push(formatSize(memory));
  const gpus = Number(requested["gpus"] ?? 0);
  if (gpus) parts.push(`${String(gpus)} GPU${gpus === 1 ? "" : "s"}`);
  const scratch = Number(requested["scratch_disk"] ?? 0);
  if (scratch) parts.push(`${formatSize(scratch)} of scratch disk`);
  const text = parts.join(", ") || "Slurm’s defaults";
  const walltime = Number(requested["walltime"] ?? 0);
  return walltime ? `${text}, for up to ${walltimeText(walltime)}` : text;
}
