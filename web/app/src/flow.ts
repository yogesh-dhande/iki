/**
 * How a type's records move between the states of each of its state fields (ADR 0125):
 * the flow a diagram draws (ADR 0199), as `GET /api/v1/entity-types/{type}/flow` answers
 * (ADR 0198). The server works the transitions out from each state's guards (ADR 0128),
 * knowing only the field and the two states: a transition is conditional when a guard
 * also needs the record, the person or the history.
 *
 * This is the one place the app reads a type's flow.
 */

import { unwrap, type Schemas } from "@ikisci/sdk";
import { queryOptions } from "@tanstack/react-query";

import { iki } from "./api";

/** A move from one state to another that a state field allows. */
export type FlowTransition = Schemas["FlowTransitionOut"];

/** One state field's flow. */
export type FlowField = Schemas["FlowFieldOut"];

/** A type's flows: one per state field, none for a type without one. A reviewed type's
 * review field is in `review` instead, once per flow its records can follow (ADR 0306). */
export type TypeFlow = Schemas["TypeFlowOut"];

/** A flow of a review procedure a reviewed type's records can be pinned to: its review
 * field as a record pinned to it has it, and whether it's the project's choice, the
 * deployment's default, its plugin's suggestion or the pin asked for. */
export type ReviewFlow = Schemas["ReviewFlowOut"];

/** Whose choice and which pin a reviewed type's flows are drawn for: `project` binds the
 * procedure's parts to its lab roles and marks its choice; `procedure` and `flow`, such as
 * a record's pin, add that flow. */
export interface FlowFor {
  project?: string | undefined;
  procedure?: string | undefined;
  flow?: string | undefined;
}

/** A type's flows. They change only when its plugins do, or a review choice is made. */
export function typeFlowQuery(name: string, at: FlowFor = {}) {
  // Only what's given, so the same flows share a key however they were asked for.
  const query = Object.fromEntries(Object.entries(at).filter(([, v]) => v)) as {
    [K in keyof FlowFor]?: string;
  };
  return queryOptions({
    queryKey: ["entity-types", name, "flow", query],
    queryFn: async ({ signal }) =>
      unwrap(
        await iki.GET("/api/v1/entity-types/{name}/flow", {
          params: { path: { name }, query },
          signal,
        }),
      ),
    staleTime: 10 * 60_000,
  });
}

/** The review flow a record is pinned to, as `GET …/flow` drew it for the record's project. */
export function pinnedFlow(flow: TypeFlow | undefined): ReviewFlow | undefined {
  return flow?.review?.flows.find((f) => f.pinned);
}

/** How a review flow is named: its procedure and its flow, as a person reads them. */
export function reviewFlowName(flow: ReviewFlow): string {
  return `${flow.procedure_label}, ${flow.label}`;
}
