/**
 * Starters (ADRs 0198 and 0240): versioned recipes a project's Admin applies to set an assay
 * service or a procedure such as lot release testing up, choosing its review flow and who
 * holds each lab role.
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
    await iki.POST("/api/v1/starters/{name}/preview", {
      params: { path: { name: id } },
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
    await iki.POST("/api/v1/starters/{name}/apply", {
      params: { path: { name: id } },
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

/** A flow's own route through its result type's states: all of them, unless the type has a
 * route per flow in one state field and the starter names this flow's (ADR 0240). Moves
 * between its states are kept; those leading elsewhere are dropped. */
export function routeOf(
  field: FlowField | undefined,
  states: readonly string[] | null | undefined,
): FlowField | undefined {
  if (!field || !states) return field;
  const kept = new Set(states);
  return {
    ...field,
    // A correction that would return a record to another flow's state isn't this route's.
    states: field.states
      .filter((s) => kept.has(s.name))
      .map((s) => (s.on_edit && !kept.has(s.on_edit) ? { ...s, on_edit: null } : s)),
    transitions: field.transitions.filter((t) => kept.has(t.from) && kept.has(t.to)),
  };
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

/** The installed lab roles a starter's slot may be played by. */
export type SlotRoles = Readonly<Record<string, string>>;

/**
 * Whether the people chosen can sign a plugin starter's flow (ADR 0303), as Iki checks it:
 * someone for every step, from the roles playing any of its slots, with each pair of steps
 * the procedure keeps apart signed by different people. One person may play several
 * slots, so long as that holds. A flow has a handful of steps, so it tries every way.
 */
export function canStaff(
  flow: Pick<StarterFlow, "steps" | "apart">,
  roles: SlotRoles,
  chosen: Readonly<Record<string, readonly string[]>>,
): boolean {
  const steps = flow.steps ?? [];
  const apart = new Set((flow.apart ?? []).map(([a = "", b = ""]) => [a, b].sort().join(" ")));
  const signer: string[] = [];
  const candidates = (index: number) => [
    ...new Set(
      (steps[index]?.slots ?? []).flatMap((slot) => {
        const role = roles[slot];
        return role ? (chosen[role] ?? []) : [];
      }),
    ),
  ];
  const place = (index: number): boolean => {
    if (index === steps.length) return true;
    const state = steps[index]?.state ?? "";
    for (const person of candidates(index)) {
      const clash = steps
        .slice(0, index)
        .some(
          (other, i) => signer[i] === person && apart.has([state, other.state].sort().join(" ")),
        );
      if (clash) continue;
      signer[index] = person;
      if (place(index + 1)) return true;
    }
    return false;
  };
  return place(0);
}

/** Sets a plugin's starter as the deployment's default for its type (ADR 0302). */
export async function setStarterDefault(
  id: string,
  body: Schemas["StarterDefaultIn"],
): Promise<Schemas["StarterDefaultOut"]> {
  return unwrap(
    await iki.POST("/api/v1/starters/{name}/default", {
      params: { path: { name: id } },
      body,
    }),
  );
}

/** Clears the deployment's default for a starter's type, with a reason. */
export async function clearStarterDefault(
  id: string,
  reason: string,
): Promise<Schemas["StarterDefaultOut"]> {
  return unwrap(
    await iki.POST("/api/v1/starters/{name}/default/clear", {
      params: { path: { name: id } },
      body: { reason },
    }),
  );
}

/** The installed starters, for an Org Admin choosing the deployment's defaults. */
export const deploymentStartersQuery = queryOptions({
  queryKey: [...STARTERS, "deployment"],
  queryFn: async ({ signal }) => unwrap(await iki.GET("/api/v1/starters", { signal })),
});
