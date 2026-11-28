/**
 * A record's state fields (ADR 0125): each one's state, whether it locks the record, and a
 * control to set it to another, offering the states this person may set and saying why
 * not of the others.
 */

import { unwrap, type Schemas, isStale } from "@ikisci/sdk";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useId, useState } from "react";

import { fieldErrors, iki, problemOf, useApiMutation } from "../api";
import {
  ENTITIES,
  editReturns,
  entityQuery,
  NOT_SENT,
  stateFields,
  stateLabel,
  unsentState,
  type Entity,
  type EntityType,
  type FieldDef,
} from "../entities";
import { typeFlowQuery } from "../flow";
import { FlowDiagram, useRoleLabels } from "../flow-diagram";
import { flowModel, nextFrom } from "../flow-model";
import {
  Badge,
  Button,
  buttonClass,
  ConfirmDialog,
  RadioField,
  TextField,
  useMessages,
} from "../ui";
import { WORK } from "../people";
import { listed } from "../requests";
import { changedProblem, useRevisioned, type IfMatch } from "../revisioned";

type StateCan = Schemas["StateCan"];

/** The badges for a page's header: each state field's state, and that the record's locked. */
export function StateBadges({ entity, type }: { entity: Entity; type: EntityType | undefined }) {
  const fields = stateFields(type);
  if (!fields.length) return null;
  // A request's service states before it's sent (ADR 0132): one badge says so.
  const unsent = fields.filter((f) => unsentState(entity, f));
  return (
    <>
      {fields
        .filter((f) => !unsent.includes(f))
        .map((f) => (
          <span key={f.name}>
            {" "}
            <Badge>{stateLabel(f, stateOf(entity, f))}</Badge>
          </span>
        ))}
      {unsent.length ? (
        <>
          {" "}
          <Badge muted>{NOT_SENT}</Badge>
        </>
      ) : null}
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
  const returns = editReturns(entity, type);
  if (entity.can.correct && returns.length) {
    return `Locked. Correcting a value will return ${listed(returns.map((r) => r.description))}. Each correction needs a reason.`;
  }
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
  // The state fields whose flow is open.
  const [open, setOpen] = useState<ReadonlySet<string>>(new Set());
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
          if (unsentState(entity, f)) {
            // Nobody sets it until the request is sent (ADR 0132): its initial state's
            // description, such as "Sent, and waiting", isn't so yet.
            return (
              <li key={f.name} className="row">
                <div className="row-main">
                  <span className="row-title">{f.label}: Not sent yet</span>
                  <span className="row-meta">
                    The service sets it once it’s sent, starting from{" "}
                    {stateLabel(f, f.initial ?? stateOf(entity, f))}.
                  </span>
                </div>
              </li>
            );
          }
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
                <button
                  type="button"
                  className={buttonClass("quiet")}
                  aria-expanded={open.has(f.name)}
                  aria-controls={`${id}-${f.name}-flow`}
                  onClick={() => {
                    setOpen((was) => {
                      const now = new Set(was);
                      if (!now.delete(f.name)) now.add(f.name);
                      return now;
                    });
                  }}
                >
                  How it moves
                </button>
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
              <div className="row-flow" id={`${id}-${f.name}-flow`} hidden={!open.has(f.name)}>
                {open.has(f.name) && type ? (
                  <RecordFlow
                    type={type.name}
                    field={f.name}
                    current={stateOf(entity, f)}
                    options={options}
                  />
                ) : null}
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

/** A record's place in its state field's flow (ADR 0199): the diagram, its state now and
 * the states the reader may move it to. */
function RecordFlow({
  type,
  field,
  current,
  options,
}: {
  type: string;
  field: string;
  current: string;
  options: StateCan[];
}) {
  const flow = useQuery(typeFlowQuery(type)).data?.fields.find((f) => f.name === field);
  const roles = useRoleLabels();
  if (!flow) return null;
  const model = flowModel(flow, roles);
  const next = nextFrom(model, current);
  const yours = next.filter((n) => options.some((o) => o.name === n.name && o.allowed));
  return (
    <>
      <p className="row-meta">
        {yours.length
          ? `Next for you: ${either(yours.map((n) => n.label))}.`
          : next.length
            ? `It can move to ${either(next.map((n) => n.label))}, but not by you now.`
            : "It doesn’t move on from here."}
      </p>
      <FlowDiagram flow={flow} roles={roles} current={current} options={options} words="hidden" />
    </>
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
    async (header: IfMatch) =>
      unwrap(
        await iki.POST("/api/v1/entities/{ref}/states/{field}", {
          params: { path: { ref: entity.id, field: field.name }, header },
          body: { state, reason: reason.trim() },
        }),
      ),
    // Its new state may make it someone's work, or no longer (ADR 0122).
    [ENTITIES, WORK],
  );
  // From the revision the person opened it at, which they reviewed.
  const rev = useRevisioned(entity, { error: set.error, refresh: ENTITIES });
  const problem = set.error && !isStale(set.error) ? problemOf(set.error) : undefined;
  const errors = fieldErrors(problem);
  const reasonError =
    errors["reason"] ??
    (missing && target
      ? `Setting its ${field.label} to ${target.label} needs a reason: say why.`
      : undefined);
  const from = stateLabel(field, stateOf(rev.read, field));
  return (
    <ConfirmDialog
      isOpen
      title={`Set ${field.label} of ${entity.hrid}`}
      confirmLabel={`Set ${field.label}`}
      confirmVariant="primary"
      busy={set.isPending || rev.rereading}
      problem={
        rev.changed
          ? changedProblem(
              "Someone changed it while this was open. The page now shows their change: check it, then try again.",
            )
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
        rev.submit((header) => {
          set.mutate(header, {
            onSuccess: (saved) => {
              client.setQueryData(entityQuery(saved.hrid).queryKey, saved);
              onClose();
              const now = stateLabel(field, stateOf(saved, field) || state);
              say(`${saved.hrid}’s ${field.label} is ${now} now.`);
            },
          });
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
