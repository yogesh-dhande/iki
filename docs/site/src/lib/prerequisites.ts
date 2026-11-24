// What a use case needs before its steps: the plugins and their versions, a starter, the
// roles, where its actions run, its data and the outside services it calls. Each use case's
// frontmatter says it, checked against the first-party plugins when the site is built.
import { z } from "astro/zod";

/** Where a use case's actions run: no actions, one of the tiers, or a Slurm cluster's
 * self-hosted workers. */
export const COMPUTE = {
  none: "Nothing: no action runs",
  backend: "Backend: Iki's isolated worker pods",
  container: "Container: the plugin's image on Kubernetes",
  vm: "VM: a virtual machine",
  self_hosted: "Self-hosted: a machine a compute admin registered",
  slurm: "Slurm: a cluster's self-hosted workers",
} as const;

export const prerequisites = z.object({
  plugins: z.array(z.object({ name: z.string(), version: z.string() })).min(1),
  starter: z.string().optional(),
  roles: z.array(z.object({ role: z.string(), needs: z.string() })).min(1),
  compute: z.enum(Object.keys(COMPUTE) as [keyof typeof COMPUTE, ...(keyof typeof COMPUTE)[]]),
  data: z
    .array(z.object({ name: z.string(), source: z.string(), licence: z.string() }))
    .default([]),
  services: z.array(z.object({ name: z.string(), use: z.string() })).default([]),
});

export type Prerequisites = z.infer<typeof prerequisites>;

/** What the check needs to know of a plugin. */
export interface Known {
  name: string;
  version: string;
  starters?: string[] | undefined;
}

function parts(version: string): number[] {
  return version
    .split(/[.+-]/)
    .slice(0, 3)
    .map((part) => Number.parseInt(part, 10) || 0);
}

function compare(a: string, b: string): number {
  const [x, y] = [parts(a), parts(b)];
  for (let i = 0; i < 3; i += 1) {
    const difference = (x[i] ?? 0) - (y[i] ?? 0);
    if (difference !== 0) return difference;
  }
  return 0;
}

/** Whether a version meets comparisons such as `>=0.4,<1`. A bare version means `==`. */
export function satisfies(version: string, range: string): boolean {
  return range.split(",").every((raw) => {
    const found = /^\s*(>=|<=|==|!=|>|<)?\s*(\d+(?:\.\d+){0,2})\s*$/.exec(raw);
    if (!found?.[2]) throw new Error(`"${raw.trim()}" isn't a version comparison`);
    const order = compare(version, found[2]);
    switch (found[1] ?? "==") {
      case ">=":
        return order >= 0;
      case "<=":
        return order <= 0;
      case ">":
        return order > 0;
      case "<":
        return order < 0;
      case "!=":
        return order !== 0;
      default:
        return order === 0;
    }
  });
}

/** What's wrong with a use case's prerequisites, against the plugins there are. */
export function problems(given: Prerequisites, known: readonly Known[]): string[] {
  const found: string[] = [];
  for (const wanted of given.plugins) {
    const plugin = known.find((candidate) => candidate.name === wanted.name);
    if (!plugin) {
      found.push(`${wanted.name} isn't one of Iki's plugins`);
      continue;
    }
    let meets: boolean;
    try {
      meets = satisfies(plugin.version, wanted.version);
    } catch (error) {
      found.push(`${wanted.name}: ${(error as Error).message}`);
      continue;
    }
    if (!meets) {
      found.push(`${wanted.name} is at ${plugin.version}, not ${wanted.version}`);
    }
  }
  if (given.starter !== undefined) {
    const dot = given.starter.lastIndexOf(".");
    const [owner, name] = [given.starter.slice(0, dot), given.starter.slice(dot + 1)];
    const plugin = known.find((candidate) => candidate.name === owner);
    if (dot < 0 || !plugin?.starters?.includes(name)) {
      found.push(`no plugin ships the starter ${given.starter}`);
    } else if (!given.plugins.some((wanted) => wanted.name === owner)) {
      found.push(`the starter ${given.starter} needs ${owner} among the plugins`);
    }
  }
  return found;
}
