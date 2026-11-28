/**
 * People in records (ADR 0122): whom a field can name, how a person reads on a record,
 * and each person's work: what's assigned to them, and what's open to their lab roles.
 */

import { unwrap, type Schemas, ifMatch } from "@ikisci/sdk";
import { infiniteQueryOptions, queryOptions } from "@tanstack/react-query";

import { iki } from "./api";
import {
  ENTITIES,
  formatDate,
  formatDateTime,
  stateLabel,
  type Entity,
  type EntityType,
  type FieldDef,
} from "./entities";

export type Person = Schemas["PersonOut"];
export type Nameable = Schemas["NameableOut"];
export type WorkItem = Schemas["WorkItemOut"];
export type Reassign = Schemas["ReassignOut"];
export type Assigned = Schemas["AssignOut"];

export const WORK = ["work"] as const;

/** The state fields (ADR 0125) a type's assignment fields count in: whose states say
 * whose work a record is. */
export function workStateFields(type: EntityType | undefined): FieldDef[] {
  const named = new Set((type?.fields ?? []).map((f) => f.assignment_field).filter(Boolean));
  return (type?.fields ?? []).filter((f) => f.type === "state" && named.has(f.name));
}

/** What those state fields are on a record now, each by its state's label. */
export function workStates(type: EntityType | undefined, entity: Entity): string[] {
  return workStateFields(type).map((f) => {
    const value = entity.values[f.name];
    return stateLabel(f, typeof value === "string" ? value : "");
  });
}

/** How many people a picker asks the server for at most. */
const NAMEABLE_LIMIT = 20;

/**
 * Whom the server lists for `text`, worked out from what it listed for `before`, or
 * undefined when that can't be known. It can when the earlier list was whole (under the
 * limit) and `text` contains `before`: everyone matching `text` was in it. The server's
 * match and order (name or email containing the text, those starting with it first, then
 * by name), so its answer, when it comes, is the same list: suggestions don't move under
 * someone choosing one.
 */
export function narrowNameable(
  listed: Nameable[],
  before: string,
  text: string,
): Nameable[] | undefined {
  const was = before.trim().toLowerCase();
  const key = text.trim().toLowerCase();
  if (listed.length >= NAMEABLE_LIMIT || !key.includes(was)) return undefined;
  const rank = (p: Nameable): [number, string] => {
    const name = p.name.toLowerCase();
    const email = p.email.toLowerCase();
    const starts = name.startsWith(key) || email.startsWith(key) || ` ${name}`.includes(` ${key}`);
    return [!key || starts ? 0 : 1, name];
  };
  const order = (a: [number, string], b: [number, string]): number =>
    a[0] - b[0] || (a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0);
  return listed
    .filter((p) => p.name.toLowerCase().includes(key) || p.email.toLowerCase().includes(key))
    .map((p) => ({ p, at: rank(p) }))
    .sort((a, b) => order(a.at, b.at))
    .map(({ p }) => p);
}

/** The people a project's records can name, for a picker: those a field can name. Until
 * the server answers, only an answer known to be the same shows: never people listed for
 * other text or another role, who would then move or vanish as someone clicks. */
export function nameableQuery(projectId: string, role: string | null | undefined, q: string) {
  const key = ["people", projectId, role ?? "", q.trim()] as const;
  return queryOptions({
    queryKey: key,
    queryFn: async ({ signal }) =>
      unwrap(
        await iki.GET("/api/v1/projects/{project_id}/people", {
          params: {
            path: { project_id: projectId },
            query: {
              limit: NAMEABLE_LIMIT,
              ...(role ? { role } : {}),
              ...(q.trim() ? { q: q.trim() } : {}),
            },
          },
          signal,
        }),
      ),
    placeholderData: (previous, previousQuery) => {
      const was = previousQuery?.queryKey;
      if (!previous || was?.[1] !== key[1] || was[2] !== key[2]) return undefined;
      return narrowNameable(previous, was[3], key[3]);
    },
    staleTime: 30_000,
  });
}

/** Someone a record names, by name: "(deactivated)" after it once they are. */
export function personLabel(
  id: string,
  entity: { people?: Record<string, Person> | undefined } | undefined,
): string {
  const found = entity?.people?.[id];
  if (!found) return "Someone you can’t see";
  return found.active ? found.name : `${found.name} (deactivated)`;
}

/** Who of a record's assignees needs replacing, for one field. */
export function staleIn(entity: Pick<Entity, "needs_reassigning">, field: string): Reassign[] {
  return (entity.needs_reassigning ?? []).filter((s) => s.field === field);
}

export interface WorkFilter {
  open?: boolean | undefined;
  project?: string | undefined;
  type?: string | undefined;
  state?: string | undefined;
  sort?: "due" | "updated" | undefined;
}

/** My work's address, `?open=true&project=…&type=…&state=…&sort=updated`: its filter. */
export function validateWorkSearch(search: Record<string, unknown>): WorkFilter {
  const out: WorkFilter = {};
  if (search["open"] === true || search["open"] === "true") out.open = true;
  for (const key of ["project", "type", "state"] as const) {
    const value = search[key];
    if (typeof value === "string" && value && value.length <= 200) out[key] = value;
  }
  if (search["sort"] === "updated") out.sort = "updated";
  return out;
}

/** A page of your work at a time: what's assigned to you, or open to your lab roles. */
export function workQuery(filter: WorkFilter) {
  return infiniteQueryOptions({
    queryKey: [...WORK, filter],
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam, signal }) => {
      const query = {
        limit: 50,
        sort: filter.sort ?? "due",
        ...(filter.project ? { project: [filter.project] } : {}),
        ...(filter.type ? { type: [filter.type] } : {}),
        ...(filter.state ? { state: [filter.state] } : {}),
        ...(pageParam ? { cursor: pageParam } : {}),
      };
      return unwrap(
        filter.open
          ? await iki.GET("/api/v1/me/work/open", { params: { query }, signal })
          : await iki.GET("/api/v1/me/work", { params: { query }, signal }),
      );
    },
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
}

/** "2026-11-20" or a datetime, as people read it; and whether it's past. */
export function dueText(due: string): { text: string; late: boolean } {
  const date = due.length === 10 ? new Date(`${due}T23:59:59`) : new Date(due);
  return {
    text: due.length === 10 ? formatDate(due) : formatDateTime(due),
    late: date.getTime() < Date.now(),
  };
}

/** Takes a record nobody is assigned, in one of its `can.take` fields. */
export async function take(entity: Pick<Entity, "id" | "revision">, field: string) {
  return unwrap(
    await iki.POST("/api/v1/entities/{ref}/take", {
      params: { path: { ref: entity.id }, header: ifMatch(entity.revision) },
      body: { field, reason: "" },
    }),
  );
}

/** Names someone in an assignment field of many records at once. */
export async function assignMany(body: Schemas["AssignIn"]): Promise<Assigned> {
  return unwrap(await iki.POST("/api/v1/entities/assign", { body }));
}

/** What changing someone's work makes stale: their work, and the records. */
export const AFTER_ASSIGNING = [WORK, ENTITIES] as const;
