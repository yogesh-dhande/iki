/**
 * Who a record is assigned to (ADR 0122): each of its type's assignment fields, whom it
 * names and whether it's their work in the record's states; who can't be assigned it any
 * more; and taking it, for those whose lab role it's open to.
 */

import { useQueryClient } from "@tanstack/react-query";
import { useId } from "react";

import { problemOf, useApiMutation } from "../api";
import { entityQuery, stateLabel, type Entity, type EntityType, type FieldDef } from "../entities";
import { AFTER_ASSIGNING, personLabel, staleIn, take } from "../people";
import { Badge, Button, ProblemAlert, useMessages } from "../ui";

function stateLabels(field: FieldDef | undefined, names: string[]): string {
  const labels = names.map((n) => (field ? stateLabel(field, n) : n));
  if (labels.length <= 1) return labels.join("");
  return `${labels.slice(0, -1).join(", ")} or ${labels[labels.length - 1] ?? ""}`;
}

/** What a state field (ADR 0125) is set to on a record, by name: "" if nothing. */
function stateNow(entity: Pick<Entity, "values">, field: string): string {
  const value = entity.values[field];
  return typeof value === "string" ? value : "";
}

/** Whether a field's assignment counts in the record's states: always, if it names no
 * state field; otherwise while that field is in one of its states. */
export function countsNow(field: FieldDef, entity: Pick<Entity, "values">): boolean {
  if (!field.assignment_field) return true;
  return field.assignment_states.includes(stateNow(entity, field.assignment_field));
}

function named(entity: Entity, field: FieldDef): string[] {
  const value = entity.values[field.name];
  if (value === null || value === undefined) return [];
  return Array.isArray(value) ? (value as string[]) : [value as string];
}

export function EntityWork({ entity, type }: { entity: Entity; type: EntityType }) {
  const id = useId();
  const fields = type.fields.filter((f) => f.assignment);
  const client = useQueryClient();
  const { say } = useMessages();
  const taking = useApiMutation((field: string) => take(entity, field), AFTER_ASSIGNING);
  if (!fields.length) return null;
  const takeable = new Set(entity.can.take ?? []);
  const stale = entity.needs_reassigning ?? [];
  return (
    <section className="card" aria-labelledby={`${id}-title`}>
      <h2 className="section-title" id={`${id}-title`}>
        Assigned
      </h2>
      {stale.length ? (
        <div className="alert alert-warning" role="note">
          <p>
            It needs reassigning:{" "}
            {stale
              .map((s) => {
                const field = fields.find((f) => f.name === s.field);
                return `${personLabel(s.person, entity)} can’t be ${field?.label ?? s.field} any more (${s.why})`;
              })
              .join("; ")}
            .
          </p>
        </div>
      ) : null}
      <ProblemAlert problem={taking.error ? problemOf(taking.error) : undefined} />
      <ul className="rows" aria-label="Assignments">
        {fields.map((f) => {
          const people = named(entity, f);
          const now = countsNow(f, entity);
          const meta = `${id}-${f.name}-meta`;
          const states = f.assignment_field ? f.assignment_states : [];
          const by = type.fields.find((s) => s.name === f.assignment_field);
          const byLabel = by?.label ?? f.assignment_field ?? "";
          return (
            <li key={f.name} className="row">
              <div className="row-main">
                <span className="row-title">
                  {f.label}:{" "}
                  {people.length ? people.map((p) => personLabel(p, entity)).join(", ") : "Nobody"}
                  {staleIn(entity, f.name).length ? (
                    <>
                      {" "}
                      <Badge muted>Needs reassigning</Badge>
                    </>
                  ) : null}
                </span>
                <span className="row-meta" id={meta}>
                  {states.length
                    ? now
                      ? `Their work now: its ${byLabel} is ${by ? stateLabel(by, stateNow(entity, by.name)) : ""}.`
                      : `Their work once its ${byLabel} is ${stateLabels(by, states)}.`
                    : "Their work while they’re named."}
                  {f.role_label ? ` Someone with the ${f.role_label} role.` : ""}
                </span>
              </div>
              {takeable.has(f.name) ? (
                <div className="row-actions">
                  <Button
                    describedBy={meta}
                    label={`Take it as ${f.label}`}
                    busy={taking.isPending && taking.variables === f.name}
                    busyText="Taking…"
                    onClick={() => {
                      taking.mutate(f.name, {
                        onSuccess: (saved) => {
                          client.setQueryData(entityQuery(saved.hrid).queryKey, saved);
                          say(`${saved.hrid} is yours, as ${f.label}.`);
                        },
                      });
                    }}
                  >
                    Take it
                  </Button>
                </div>
              ) : null}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
