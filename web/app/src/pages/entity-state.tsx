/**
 * A record's state fields (ADR 0125): each one's state, whether it locks the record, and a
 * control to set it to another, offering the states this person may set and saying why
 * not of the others.
 */

import { ApiError, unwrap, type Schemas } from "@ikisci/sdk";
import { useQueryClient } from "@tanstack/react-query";
import { useId, useState } from "react";

import { fieldErrors, iki, problemOf, useApiMutation } from "../api";
import {
  ENTITIES,
  entityQuery,
  stateFields,
  stateLabel,
  type Entity,
  type EntityType,
  type FieldDef,
} from "../entities";
import { Badge, Button, ConfirmDialog, RadioField, TextField, useMessages } from "../ui";
import { WORK } from "../people";

type StateCan = Schemas["StateCan"];

/** The badges for a page's header: each state field's state, and that the record's locked. */
export function StateBadges({ entity, type }: { entity: Entity; type: EntityType | undefined }) {
  const fields = stateFields(type);
  if (!fields.length) return null;
  return (
    <>
      {fields.map((f) => (
        <span key={f.name}>
          {" "}
          <Badge>{stateLabel(f, stateOf(entity, f))}</Badge>
        </span>
      ))}
      {entity.locked ? (
        <>
          {" "}
          <Badge muted>Locked</Badge>
        </>
      ) : null}
    </>
  );
}

/** The state a record's state field is in, by name. */
function stateOf(entity: Entity, field: FieldDef): string {
  const value = entity.values[field.name];
  return typeof value === "string" ? value : "";
}

function either(words: string[]): string {
  if (words.length <= 1) return words.join("");
  return `${words.slice(0, -1).join(", ")} or ${words[words.length - 1] ?? ""}`;
}

function and(words: string[]): string {
  if (words.length <= 1) return words.join("");
  return `${words.slice(0, -1).join(", ")} and ${words[words.length - 1] ?? ""}`;
}

/** Why a locked record can't change: the fields whose states lock it, and the states that
 * would unlock each. */
export function lockedWhy(entity: Entity, type: EntityType | undefined): string {
  const fields = stateFields(type).filter((f) => entity.locked_by.includes(f.name));
  const held = fields.map((f) => `its ${f.label} is ${stateLabel(f, stateOf(entity, f))}`);
  const until = fields
    .map((f) => {
      const open = f.states.filter((s) => !s.locks && s.set_by.length).map((s) => s.label);
      return open.length ? `its ${f.label} is set to ${either(open)}` : "";
    })
    .filter(Boolean);
  const how = until.length ? `, until ${and(until)}` : "";
  return `Locked, as ${and(held)}: its other values can’t change, nor can it be archived${how}.`;
}

export function EntityState({ entity, type }: { entity: Entity; type: EntityType | undefined }) {
  const [chosen, setChosen] = useState<FieldDef | null>(null);
  const id = useId();
  const fields = stateFields(type);
  if (!fields.length) return null;
  return (
    <section className="card" aria-labelledby={`${id}-title`}>
      <h2 className="section-title" id={`${id}-title`}>
        States
      </h2>
      {entity.locked ? <p className="muted">{lockedWhy(entity, type)}</p> : null}
      <ul className="rows" aria-label="State fields">
        {fields.map((f) => {
          const current = f.states.find((s) => s.name === entity.values[f.name]);
          const options = entity.can.states[f.name] ?? [];
          const allowed = options.some((o) => o.allowed);
          const reasons = [...new Set(options.map((o) => o.why_not).filter(Boolean))];
          const why = `${id}-${f.name}-why`;
          return (
            <li key={f.name} className="row">
              <div className="row-main">
                <span className="row-title">
                  {f.label}: {current?.label ?? stateOf(entity, f)}
                  {current?.locks ? <span className="cell-meta">Locks it</span> : null}
                </span>
                <span className="row-meta" id={why}>
                  {current?.description ?? ""}
                  {!allowed && reasons.length ? ` ${reasons.join(" ")}` : ""}
                </span>
              </div>
              <div className="row-actions">
                <Button
                  disabled={!allowed}
                  describedBy={why}
                  onClick={() => {
                    setChosen(f);
                  }}
                >
                  {`Set ${f.label}`}
                </Button>
              </div>
            </li>
          );
        })}
      </ul>
      {chosen ? (
        <SetStateDialog
          entity={entity}
          field={chosen}
          options={entity.can.states[chosen.name] ?? []}
          onClose={() => {
            setChosen(null);
          }}
        />
      ) : null}
    </section>
  );
}

function SetStateDialog({
  entity,
  field,
  options,
  onClose,
}: {
  entity: Entity;
  field: FieldDef;
  options: StateCan[];
  onClose: () => void;
}) {
  const [state, setState] = useState(options.find((o) => o.allowed)?.name ?? "");
  const [reason, setReason] = useState("");
  // Said before asking Iki, which would say the same.
  const [missing, setMissing] = useState(false);
  const { say } = useMessages();
  const client = useQueryClient();
  const target = options.find((o) => o.name === state);
  const set = useApiMutation(
    async () =>
      unwrap(
        await iki.POST("/api/v1/entities/{ref}/states/{field}", {
          params: {
            path: { ref: entity.id, field: field.name },
            header: { "If-Match": `"${String(entity.revision)}"` },
          },
          body: { state, reason: reason.trim() },
        }),
      ),
    // Its new state may make it someone's work, or no longer (ADR 0122).
    [ENTITIES, WORK],
  );
  const problem = set.error ? problemOf(set.error) : undefined;
  const stale = set.error instanceof ApiError && set.error.problem.code === "stale";
  const errors = fieldErrors(problem);
  const reasonError =
    errors["reason"] ??
    (missing && target
      ? `Setting its ${field.label} to ${target.label} needs a reason: say why.`
      : undefined);
  const from = stateLabel(field, stateOf(entity, field));
  return (
    <ConfirmDialog
      isOpen
      title={`Set ${field.label} of ${entity.hrid}`}
      confirmLabel={`Set ${field.label}`}
      confirmVariant="primary"
      busy={set.isPending}
      problem={
        stale && problem
          ? {
              ...problem,
              detail:
                "Someone changed it while this was open. The page now shows their change: check it, then try again.",
            }
          : problem && !errors["reason"]
            ? problem
            : undefined
      }
      onCancel={onClose}
      onConfirm={() => {
        if (target?.reason === "required" && !reason.trim()) {
          setMissing(true);
          return;
        }
        set.mutate(undefined, {
          onSuccess: (saved) => {
            client.setQueryData(entityQuery(saved.hrid).queryKey, saved);
            onClose();
            const now = stateLabel(field, stateOf(saved, field) || state);
            say(`${saved.hrid}’s ${field.label} is ${now} now.`);
          },
          onError: (error) => {
            if (error.problem.code === "stale") {
              void client.invalidateQueries({ queryKey: ENTITIES });
            }
          },
        });
      }}
    >
      <p>
        It’s {from} now. The change is a revision of its own, with your name
        {target?.reason ? " and your reason" : ""}.
      </p>
      <RadioField
        label="Set it to"
        value={state}
        onChange={(value) => {
          setState(value);
          setMissing(false);
        }}
        options={options.map((o) => ({
          value: o.name,
          label: o.label,
          disabled: !o.allowed,
          ...(o.why_not
            ? { description: o.why_not }
            : o.locks
              ? { description: "Locks it: its other values can’t change while it’s in it." }
              : {}),
        }))}
      />
      {target?.reason ? (
        <TextField
          label="Reason"
          value={reason}
          onChange={(value) => {
            setReason(value);
            setMissing(false);
          }}
          maxLength={2000}
          optional={target.reason === "optional"}
          multiline
          error={reasonError}
          help="Why, for the record. It’s kept in its history."
        />
      ) : null}
    </ConfirmDialog>
  );
}
