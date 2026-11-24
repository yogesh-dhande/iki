// What a use case needs before its steps: the plugins and their versions, a starter, the
// roles, where its actions run, its data and the outside services it calls. A use case's
// frontmatter may say any of it; the plugins it names must be Iki's, so their links resolve.
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
  plugins: z.array(z.object({ name: z.string(), version: z.string().optional() })).optional(),
  starter: z.string().optional(),
  roles: z.array(z.object({ role: z.string(), needs: z.string() })).optional(),
  compute: z
    .enum(Object.keys(COMPUTE) as [keyof typeof COMPUTE, ...(keyof typeof COMPUTE)[]])
    .optional(),
  data: z.array(z.object({ name: z.string(), source: z.string(), licence: z.string() })).optional(),
  services: z.array(z.object({ name: z.string(), use: z.string() })).optional(),
});

export type Prerequisites = z.infer<typeof prerequisites>;

/** The plugins a use case names that Iki doesn't have, whose links would go nowhere. */
export function unknownPlugins(given: Prerequisites, known: readonly { name: string }[]): string[] {
  return (given.plugins ?? [])
    .filter((wanted) => !known.some((plugin) => plugin.name === wanted.name))
    .map((wanted) => `${wanted.name} isn't one of Iki's plugins`);
}
