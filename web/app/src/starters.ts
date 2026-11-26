/**
 * Starters (ADR 0198): versioned recipes a project's Admin applies to set an assay service
 * up, choosing its review flow and who holds each lab role.
 * This is their data and the checks a setup screen makes before asking Iki, which makes
 * the same ones.
 */

import { unwrap, type Schemas } from "@ikisci/sdk";
import { queryOptions } from "@tanstack/react-query";

import { iki } from "./api";
import type { FlowField } from "./flow";

export type Starter = Schemas["StarterOut"];
export type StarterFlow = Schemas["StarterFlowOut"];
export type StarterSetup = Schemas["StarterSetupIn"];
export type StarterPlan = Schemas["StarterSetupOut"];
export type Member = Schemas["ProjectMemberOut"];

export const STARTERS = ["starters"] as const;

/** The installed starters, and what's applied in a project its reader can read. */
export function startersQuery(projectId: string) {
  return queryOptions({
    queryKey: [...STARTERS, projectId],
    queryFn: async ({ signal }) =>
      unwrap(
        await iki.GET("/api/v1/starters", { params: { query: { project: projectId } }, signal }),
      ),
  });
}

export async function previewStarter(id: string, setup: StarterSetup): Promise<StarterPlan> {
  return unwrap(
    await iki.POST("/api/v1/starters/{starter}/preview", {
      params: { path: { starter: id } },
      body: setup,
    }),
  );
}

/** Applies the plan a preview returned. If what applying does has changed since, nothing is
 * applied: Iki answers 409 `starter_plan_changed` with the plan as it is now (ADR 0207). */
export async function applyStarter(
  id: string,
  setup: StarterSetup,
  plan: string,
): Promise<StarterPlan> {
  return unwrap(
    await iki.POST("/api/v1/starters/{starter}/apply", {
      params: { path: { starter: id } },
      body: { ...setup, plan },
    }),
  );
}

/** Each lab role grant a plan adds, as `role member` pairs. */
function grantsOf(plan: StarterPlan): Set<string> {
  return new Set(
    Object.entries(plan.grants_to_add).flatMap(([role, ids]) => ids.map((m) => `${role} ${m}`)),
  );
}

/**
 * How what applying does changed between the plan previewed and the one Iki would apply
 * now: each grant it now adds or no longer adds, and whether someone applied it first. A
 * sentence each, without its full stop.
 */
export function planChanges(
  shown: StarterPlan,
  now: StarterPlan,
  label: (role: string) => string,
  memberName: (membership: string) => string,
): { key: string; body: string }[] {
  if (!now.changed) return [{ key: "nothing", body: "It’s set up this way already" }];
  const out: { key: string; body: string }[] = [];
  const before = grantsOf(shown);
  const after = grantsOf(now);
  const said = (grant: string) => {
    const [role = "", member = ""] = grant.split(" ");
    return `${memberName(member)} becomes ${label(role)}`;
  };
  for (const grant of after)
    if (!before.has(grant))
      out.push({ key: `+${grant}`, body: `Applying now also: ${said(grant)}` });
  for (const grant of before)
    if (!after.has(grant))
      out.push({ key: `-${grant}`, body: `Applying no longer does: ${said(grant)}` });
  if (!out.length) out.push({ key: "else", body: "What’s set up changed" });
  return out;
}

const RANK = { viewer: 0, editor: 1, runner: 2, admin: 3 } as const;

/** The members a lab role can be given to: people with a membership of their own, active,
 * whose project role is at least the one the lab role's work needs. Not a team's grant:
 * a lab role given there would be every member's. */
export function eligible(
  members: readonly Member[],
  minimum: keyof typeof RANK,
): (Member & { user: NonNullable<Member["user"]> })[] {
  return members.filter(
    (m): m is Member & { user: NonNullable<Member["user"]> } =>
      m.team === null &&
      m.user !== null &&
      m.user.kind === "person" &&
      m.user.active &&
      RANK[m.role] >= RANK[minimum],
  );
}

/**
 * Whether the people chosen for these roles can each sign a different step: for every
 * group of the roles, at least as many different people hold one of them as there are
 * roles in the group (Hall's condition, as Iki checks it). One person may hold several
 * roles, so long as that holds.
 */
export function independentEnough(
  roles: readonly string[],
  chosen: Readonly<Record<string, readonly string[]>>,
): boolean {
  const count = roles.length;
  for (let mask = 1; mask < 1 << count; mask += 1) {
    const group = roles.filter((_, i) => mask & (1 << i));
    const people = new Set(group.flatMap((r) => chosen[r] ?? []));
    if (people.size < group.length) return false;
  }
  return true;
}

/**
 * A flow as one of a starter's review flows uses it: when its records share one type with
 * other flows (experiments do), only the states it uses and the moves between them.
 */
export function flowUsed(
  field: FlowField,
  states: readonly string[] | null | undefined,
): FlowField {
  if (!states) return field;
  const kept = new Set(states);
  return {
    ...field,
    states: field.states
      .filter((s) => kept.has(s.name))
      .map((s) => (s.on_edit && !kept.has(s.on_edit) ? { ...s, on_edit: null } : s)),
    transitions: field.transitions.filter((t) => kept.has(t.from) && kept.has(t.to)),
  };
}
