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

/** A type's flows: one per state field, none for a type without one. */
export type TypeFlow = Schemas["TypeFlowOut"];

/** A type's flows. They change only when its plugins do. */
export function typeFlowQuery(name: string) {
  return queryOptions({
    queryKey: ["entity-types", name, "flow"],
    queryFn: async () =>
      unwrap(await iki.GET("/api/v1/entity-types/{name}/flow", { params: { path: { name } } })),
    staleTime: 10 * 60_000,
  });
}
