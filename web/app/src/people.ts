/**
 * People in records (ADR 0122): whom a field can name, how a person reads on a record,
 * and each person's work: what's assigned to them, and what's open to their lab roles.
 */

import { unwrap, type Schemas } from "@ikisci/sdk";
import { infiniteQueryOptions, queryOptions } from "@tanstack/react-query";

import { iki } from "./api";
import { ENTITIES, formatDate, formatDateTime, type Entity, type FieldDef } from "./entities";

export type Person = Schemas["PersonOut"];
export type Nameable = Schemas["NameableOut"];
export type WorkItem = Schemas["WorkItemOut"];
export type Reassign = Schemas["ReassignOut"];
export type Assigned = Schemas["AssignOut"];

export const WORK = ["work"] as const;

/** Whether a field names people: one (`person`), or a list of them. */
export function namesPeople(field: Pick<FieldDef, "type" | "items">): boolean {
  return field.type === "person" || (field.type === "list" && field.items === "person");
}

/** The people a project's records can name, for a picker: those a field can name. */
export function nameableQuery(projectId: string, role: string | null | undefined, q: string) {
  return queryOptions({
    queryKey: ["people", projectId, role ?? "", q.trim()],
    queryFn: async () =>
      unwrap(
        await iki.GET("/api/v1/projects/{project_id}/people", {
          params: {
            path: { project_id: projectId },
            query: { ...(role ? { role } : {}), ...(q.trim() ? { q: q.trim() } : {}) },
          },
        }),
      ),
    placeholderData: (previous) => previous,
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

/** A page of your work at a time: what's assigned to you, or open to your lab roles. */
export function workQuery(filter: WorkFilter) {
  return infiniteQueryOptions({
    queryKey: [...WORK, filter],
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam }) => {
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
          ? await iki.GET("/api/v1/me/work/open", { params: { query } })
          : await iki.GET("/api/v1/me/work", { params: { query } }),
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
      params: { path: { ref: entity.id }, header: { "If-Match": `"${String(entity.revision)}"` } },
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
