/**
 * What a record's plugins' rules say of it (ADR 0127): the holds a save opened, kept with
 * a reason and waiting for review by a lab role, and the rules a deploy found it breaks.
 * A hold's reviewer says why it may stand, and that clears it; a breach clears at the next
 * save that keeps the rule.
 */

import { unwrap, type Schemas, isStale } from "@ikisci/sdk";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useId, useState } from "react";

import { fieldErrors, iki, labRolesQuery, problemOf, useApiMutation, type LabRole } from "../api";
import { ENTITIES, entityQuery, personName, type Entity } from "../entities";
import { WORK } from "../people";
import { changedProblem, useRevisioned, type IfMatch } from "../revisioned";
import { Button, ConfirmDialog, RelativeTime, TextField, useMessages } from "../ui";

type Hold = Schemas["HoldOut"];

/** The lab roles (ADR 0116) that review a hold, as their plugins label them: “QA”, not
 * `iki/lab_roles.qa`. A role no plugin declares now is named by its own short name. */
function roles(names: string[], declared: readonly LabRole[]): string {
  const labels = names.map(
    (n) => declared.find((r) => r.name === n)?.label ?? n.split(/[.:]/).pop() ?? n,
  );
  if (labels.length <= 1) return labels.join("");
  return `${labels.slice(0, -1).join(", ")} or ${labels[labels.length - 1] ?? ""}`;
}

function Observations({ hold }: { hold: Hold }) {
  const evidence = hold.occurrences;
  const latest = evidence.at(-1);
  return (
    <ul className="row-holds" aria-label="Failing observations">
      {evidence.map((o) => (
        <li key={o.id}>
          {o.message ? (
            <>
              <span>{o.message}</span>{" "}
            </>
          ) : null}
          {o.legacy_gap
            ? "Earlier hold evidence is incomplete"
            : o.held_by
              ? personName(o.held_by)
              : "Iki’s check of its plugins’ rules"}
          {o.agent ? `, through ${o.agent}` : ""}
          {o.held_at ? (
            <>
              , <RelativeTime value={o.held_at} />
            </>
          ) : null}{" "}
          (revision {o.revision}
          {o.entry_revision == null ? "" : `, registry entry revision ${String(o.entry_revision)}`}
          ): {o.reason === null ? "Reason unavailable." : <>“{o.reason}”</>}
        </li>
      ))}
      {!latest || latest.revision < hold.last_revision ? (
        <li>Earlier hold evidence is incomplete.</li>
      ) : null}
    </ul>
  );
}

export function EntityHolds({ entity }: { entity: Entity }) {
  const [reviewing, setReviewing] = useState<Hold | null>(null);
  const id = useId();
  const holds = entity.holds ?? [];
  const breaches = entity.breaches ?? [];
  // Who reviews a hold, by their roles' labels: asked for with the holds, not the dialog.
  const declared = useQuery({ ...labRolesQuery, enabled: holds.length > 0 }).data ?? [];
  if (!holds.length && !breaches.length) return null;
  return (
    <>
      {holds.length ? (
        <section className="alert alert-warning" aria-labelledby={`${id}-holds`}>
          <h2 className="section-title" id={`${id}-holds`}>
            Held for review
          </h2>
          <p>
            Saved though it breaks {holds.length === 1 ? "a rule" : "rules"} that{" "}
            {holds.length === 1 ? "holds" : "hold"} it for review. Until{" "}
            {holds.length === 1 ? "it’s" : "they’re"} reviewed, it can’t be set to a state that
            locks it.
          </p>
          <ul className="rows" aria-label="Holds">
            {holds.map((h) => (
              <li key={h.id} className="row">
                <div className="row-main">
                  <span className="row-title">{h.label}</span>
                  <Observations hold={h} />
                  {!h.can_review && h.why_not ? (
                    <span className="row-meta" id={`${id}-${h.id}-why`}>
                      {h.why_not}
                    </span>
                  ) : null}
                </div>
                <div className="row-actions">
                  <Button
                    disabled={!h.can_review}
                    describedBy={h.can_review ? undefined : `${id}-${h.id}-why`}
                    onClick={() => {
                      setReviewing(h);
                    }}
                  >
                    Review
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
      {breaches.length ? (
        <section className="alert alert-warning" aria-labelledby={`${id}-breaches`}>
          <h2 className="section-title" id={`${id}-breaches`}>
            Breaks its plugins’ rules
          </h2>
          <p>
            Its plugins’ rules changed after it was saved, or it was brought from elsewhere as it
            was. Its next save is checked against{" "}
            {breaches.length === 1 ? "this rule" : "these rules"}.
          </p>
          <ul className="rows" aria-label="Rules it breaks">
            {breaches.map((b) => (
              <li key={b.rule} className="row">
                <div className="row-main">
                  <span className="row-title">{b.label}</span>
                  <span className="row-meta">
                    {b.message} Found <RelativeTime value={b.found_at} />.
                  </span>
                </div>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
      {reviewing ? (
        <ReviewDialog
          entity={entity}
          hold={reviewing}
          declared={declared}
          onClose={() => {
            setReviewing(null);
          }}
        />
      ) : null}
    </>
  );
}

/** The hold on a record, by its ID, as the record was read. */
function holdIn(entity: Entity, id: string): Hold | undefined {
  return entity.holds?.find((h) => h.id === id);
}

function ReviewDialog({
  entity,
  hold: opened,
  declared,
  onClose,
}: {
  entity: Entity;
  hold: Hold;
  declared: readonly LabRole[];
  onClose: () => void;
}) {
  const [reason, setReason] = useState("");
  const [missing, setMissing] = useState(false);
  const { say } = useMessages();
  const client = useQueryClient();
  const review = useApiMutation(
    async ({ header, version }: { header: IfMatch; version: string }) =>
      unwrap(
        await iki.POST("/api/v1/entities/{ref}/holds/{hold_id}/review", {
          params: { path: { ref: entity.id, hold_id: opened.id }, header },
          body: { reason: reason.trim(), review_version: version },
        }),
      ),
    [ENTITIES, WORK],
  );
  // The record and the hold as they were read, which the reviewer reviews: if either
  // changes, the reason they gave is for what they saw, so they give it again.
  const rev = useRevisioned(entity, {
    error: review.error,
    refresh: ENTITIES,
    version: (e) => `${String(e.revision)}:${holdIn(e, opened.id)?.review_version ?? ""}`,
    onCatchUp: () => {
      setReason("");
    },
  });
  const hold = holdIn(rev.read, opened.id);
  const problem = review.error && !isStale(review.error) ? problemOf(review.error) : undefined;
  const errors = fieldErrors(problem);
  const reasonError = errors["reason"] ?? (missing ? "Say why it may stand as it is." : undefined);
  return (
    <ConfirmDialog
      isOpen
      title={`Review the hold on ${entity.hrid}`}
      confirmLabel="Clear the hold"
      confirmVariant="primary"
      confirmDisabled={!hold}
      busy={review.isPending || rev.rereading}
      problem={
        rev.changed
          ? changedProblem(
              hold
                ? "Someone changed the record or its hold while this was open. Check what it says now, then give your reason again."
                : "Someone changed the record while this was open, and it’s no longer held by this rule.",
            )
          : problem && !errors["reason"]
            ? problem
            : undefined
      }
      onCancel={onClose}
      onConfirm={() => {
        if (!hold) return;
        if (!reason.trim()) {
          setMissing(true);
          return;
        }
        rev.submit((header) => {
          review.mutate(
            { header, version: hold.review_version },
            {
              onSuccess: (saved) => {
                client.setQueryData(entityQuery(saved.hrid).queryKey, saved);
                onClose();
                say(`The hold on ${saved.hrid} (“${hold.label}”) is reviewed.`);
              },
            },
          );
        });
      }}
    >
      <p>You’re reviewing record revision {rev.read.revision}.</p>
      {hold ? (
        <>
          <Observations hold={hold} />
          <p>
            As one of its {roles(hold.reviewed_by, declared)} reviewers, you say why it may stand as
            it is. Your name and reason are kept in its history, and the hold is cleared.
          </p>
        </>
      ) : null}
      <TextField
        label="Why it may stand"
        value={reason}
        onChange={(value) => {
          setReason(value);
          setMissing(false);
        }}
        maxLength={2000}
        multiline
        error={reasonError}
      />
    </ConfirmDialog>
  );
}
