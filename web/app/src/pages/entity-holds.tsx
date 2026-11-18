/**
 * What a record's plugins' rules say of it (ADR 0127): the holds a save opened, kept with
 * a reason and waiting for review by a lab role, and the rules a deploy found it breaks.
 * A hold's reviewer says why it may stand, and that clears it; a breach clears at the next
 * save that keeps the rule.
 */

import { unwrap, type Schemas } from "@ikisci/sdk";
import { useQueryClient } from "@tanstack/react-query";
import { useId, useState } from "react";

import { fieldErrors, iki, problemOf, useApiMutation } from "../api";
import { ENTITIES, entityQuery, personName, type Entity } from "../entities";
import { WORK } from "../people";
import { Button, ConfirmDialog, RelativeTime, TextField, useMessages } from "../ui";

type Hold = Schemas["HoldOut"];

function roles(names: string[]): string {
  const short = names.map((n) => n.split(":").pop() ?? n);
  if (short.length <= 1) return short.join("");
  return `${short.slice(0, -1).join(", ")} or ${short[short.length - 1] ?? ""}`;
}

export function EntityHolds({ entity }: { entity: Entity }) {
  const [reviewing, setReviewing] = useState<Hold | null>(null);
  const id = useId();
  const holds = entity.holds ?? [];
  const breaches = entity.breaches ?? [];
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
                  <span className="row-meta">{h.message}</span>
                  <span className="row-meta">
                    {h.held_by ? personName(h.held_by) : "Iki’s check of its plugin’s rules"}
                    {h.agent ? `, through ${h.agent}` : ""}, <RelativeTime value={h.held_at} />{" "}
                    (revision {h.revision}): “{h.reason}”
                  </span>
                  {h.last_revision !== h.revision ? (
                    <span className="row-meta">Revision {h.last_revision} broke it again.</span>
                  ) : null}
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
          onClose={() => {
            setReviewing(null);
          }}
        />
      ) : null}
    </>
  );
}

function ReviewDialog({
  entity,
  hold,
  onClose,
}: {
  entity: Entity;
  hold: Hold;
  onClose: () => void;
}) {
  const [reason, setReason] = useState("");
  const [missing, setMissing] = useState(false);
  const { say } = useMessages();
  const client = useQueryClient();
  const review = useApiMutation(
    async () =>
      unwrap(
        await iki.POST("/api/v1/entities/{ref}/holds/{hold_id}/review", {
          params: { path: { ref: entity.id, hold_id: hold.id } },
          body: { reason: reason.trim() },
        }),
      ),
    [ENTITIES, WORK],
  );
  const problem = review.error ? problemOf(review.error) : undefined;
  const errors = fieldErrors(problem);
  const reasonError = errors["reason"] ?? (missing ? "Say why it may stand as it is." : undefined);
  return (
    <ConfirmDialog
      isOpen
      title={`Review the hold on ${entity.hrid}`}
      confirmLabel="Clear the hold"
      confirmVariant="primary"
      busy={review.isPending}
      problem={problem && !errors["reason"] ? problem : undefined}
      onCancel={onClose}
      onConfirm={() => {
        if (!reason.trim()) {
          setMissing(true);
          return;
        }
        review.mutate(undefined, {
          onSuccess: (saved) => {
            client.setQueryData(entityQuery(saved.hrid).queryKey, saved);
            onClose();
            say(`The hold on ${saved.hrid} (“${hold.label}”) is reviewed.`);
          },
        });
      }}
    >
      <p>
        {hold.message} {hold.held_by ? personName(hold.held_by) : "Iki"} saved it, saying: “
        {hold.reason}”.
      </p>
      <p>
        As one of its {roles(hold.reviewed_by)} reviewers, you say why it may stand as it is. Your
        name and reason are kept in its history, and the hold is cleared.
      </p>
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
