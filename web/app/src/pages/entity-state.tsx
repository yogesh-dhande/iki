/**
 * A record's state (ADR 0118): where it is in its type's states, whether that locks it,
 * and the transitions from there, each one this person can make or why they can't.
 */

import { ApiError, unwrap, type Schemas } from "@ikisci/sdk";
import { useQueryClient } from "@tanstack/react-query";
import { useId, useState } from "react";

import { fieldErrors, iki, problemOf, useApiMutation } from "../api";
import { ENTITIES, entityQuery, type Entity, type EntityType } from "../entities";
import { Badge, Button, ConfirmDialog, TextField, useMessages } from "../ui";

type TransitionCan = Schemas["TransitionCan"];

/** The state's badge for a page's header: its label, and that it's locked. */
export function StateBadges({ entity }: { entity: Entity }) {
  if (!entity.state) return null;
  return (
    <>
      {" "}
      <Badge>{entity.state.label}</Badge>
      {entity.locked ? (
        <>
          {" "}
          <Badge muted>Locked</Badge>
        </>
      ) : null}
    </>
  );
}

/** What takes a locked record out of its state: the transitions to states that don't lock. */
export function unlockedBy(entity: Entity, type: EntityType | undefined): string[] {
  const state = entity.state?.name;
  if (!state || !type) return [];
  const locking = new Set(type.states.filter((s) => s.locks).map((s) => s.name));
  return type.transitions
    .filter((t) => t.from.includes(state) && !locking.has(t.to))
    .map((t) => t.label);
}

function either(words: string[]): string {
  const quoted = words.map((w) => `“${w}”`);
  if (quoted.length <= 1) return quoted.join("");
  return `${quoted.slice(0, -1).join(", ")} or ${quoted[quoted.length - 1] ?? ""}`;
}

export function EntityState({ entity, type }: { entity: Entity; type: EntityType | undefined }) {
  const [chosen, setChosen] = useState<TransitionCan | null>(null);
  const id = useId();
  if (!entity.state) return null;
  const transitions = entity.can.transitions;
  const out = unlockedBy(entity, type);
  return (
    <section className="card" aria-labelledby={`${id}-title`}>
      <h2 className="section-title" id={`${id}-title`}>
        State: {entity.state.label}
      </h2>
      {entity.state.description ? <p>{entity.state.description}</p> : null}
      {entity.locked ? (
        <p className="muted">
          {entity.state.label} locks it: its values can’t change
          {out.length ? `, nor can it be archived, until ${either(out)} takes it out.` : "."}
        </p>
      ) : null}
      {transitions.length ? (
        <ul className="rows" aria-label="Transitions">
          {transitions.map((t) => {
            const why = `${id}-${t.name}-why`;
            return (
              <li key={t.name} className="row">
                <div className="row-main">
                  <span className="row-title">{t.label}</span>
                  <span className="row-meta" id={why}>
                    To {t.to_label}
                    {t.reason === "required" ? " · asks why" : ""}
                    {t.why_not ? ` · ${t.why_not}` : ""}
                  </span>
                </div>
                <div className="row-actions">
                  <Button
                    disabled={!t.allowed}
                    describedBy={why}
                    onClick={() => {
                      setChosen(t);
                    }}
                  >
                    {t.label}
                  </Button>
                </div>
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="muted">No transition leads out of {entity.state.label}.</p>
      )}
      {chosen ? (
        <TransitionDialog
          entity={entity}
          transition={chosen}
          onClose={() => {
            setChosen(null);
          }}
        />
      ) : null}
    </section>
  );
}

function TransitionDialog({
  entity,
  transition,
  onClose,
}: {
  entity: Entity;
  transition: TransitionCan;
  onClose: () => void;
}) {
  const [reason, setReason] = useState("");
  // Said before asking Iki, which would say the same.
  const [missing, setMissing] = useState(false);
  const { say } = useMessages();
  const client = useQueryClient();
  const make = useApiMutation(
    async () =>
      unwrap(
        await iki.POST("/api/v1/entities/{ref}/transitions/{name}", {
          params: {
            path: { ref: entity.id, name: transition.name },
            header: { "If-Match": `"${String(entity.revision)}"` },
          },
          body: { reason: reason.trim() },
        }),
      ),
    [ENTITIES],
  );
  const problem = make.error ? problemOf(make.error) : undefined;
  const stale = make.error instanceof ApiError && make.error.problem.code === "stale";
  const errors = fieldErrors(problem);
  const reasonError =
    errors["reason"] ?? (missing ? `“${transition.label}” needs a reason: say why.` : undefined);
  return (
    <ConfirmDialog
      isOpen
      title={`${transition.label} ${entity.hrid}?`}
      confirmLabel={transition.label}
      confirmVariant="primary"
      busy={make.isPending}
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
        if (transition.reason === "required" && !reason.trim()) {
          setMissing(true);
          return;
        }
        make.mutate(undefined, {
          onSuccess: (saved) => {
            client.setQueryData(entityQuery(saved.hrid).queryKey, saved);
            onClose();
            say(`${saved.hrid} is ${saved.state?.label ?? transition.to_label} now.`);
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
        It moves from {entity.state?.label} to {transition.to_label}, as a revision of its own with
        your name{transition.reason ? " and your reason" : ""}.
      </p>
      {transition.reason ? (
        <TextField
          label="Reason"
          value={reason}
          onChange={(value) => {
            setReason(value);
            setMissing(false);
          }}
          maxLength={2000}
          optional={transition.reason === "optional"}
          multiline
          error={reasonError}
          help="Why, for the record. It’s kept in its history."
        />
      ) : null}
    </ConfirmDialog>
  );
}
