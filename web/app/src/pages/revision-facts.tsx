/**
 * What the audit trail says of a revision under its title, and the title itself: when, how
 * and which fields, why, its identity as given, and the signatures and holds it carries.
 * The trail's rows and a revision's own page (ADR 0189) say it the same way.
 */

import { useQuery } from "@tanstack/react-query";

import { apiRef, personName, type Entity, type Revision } from "../entities";
import { RelativeTime } from "../ui";
import { signaturesQuery } from "./entity-signatures";
import { KIND, revisionWhat, UtcTime, Via } from "./revision-text";

/** A value as given, cut short: a genome's bases needn't fill the page. */
function submittedText(value: unknown): string {
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return text.length > 80 ? `${text.slice(0, 80)}…` : text;
}

/** Failing observations from this revision (ADR 0127) and the hold review. */
function HoldsOpened({ revision }: { revision: Revision }) {
  const holds = revision.holds ?? [];
  if (!holds.length) return null;
  return (
    <ul className="row-holds" aria-label={`Holds at revision ${String(revision.number)}`}>
      {holds.map((h) => (
        <li key={h.occurrence_id}>
          Held for review: {h.label}. {h.message}{" "}
          {h.legacy_gap
            ? "Earlier hold evidence is incomplete"
            : h.held_by
              ? `Saved anyway by ${personName(h.held_by)}`
              : "Held by Iki’s check of its plugins’ rules"}
          {h.agent ? `, through ${h.agent}` : ""}
          {h.reason === null ? ". Reason unavailable." : <>: “{h.reason}”</>}
          {h.entry_revision == null
            ? ""
            : ` Registry entry revision ${String(h.entry_revision)}.`}{" "}
          {h.reviewer && h.reviewed_at ? (
            <>
              Reviewed by {personName(h.reviewer)} <RelativeTime value={h.reviewed_at} />: “
              {h.review_reason}”
            </>
          ) : (
            "Still held, for review."
          )}
        </li>
      ))}
    </ul>
  );
}

/** The electronic signatures that set a revision's state (ADR 0126), as people read them,
 * with each signer's reason. */
function SignedWith({ entity, revision }: { entity: Entity; revision: Revision }) {
  const listed = revision.state?.signatures ?? [];
  const given = useQuery({
    ...signaturesQuery(apiRef(entity), false),
    enabled: listed.length > 0,
  }).data?.signatures;
  if (!listed.length) return null;
  const byId = new Map((given ?? []).map((s) => [s.id, s]));
  return (
    <ul
      className="trail-signatures"
      aria-label={`Signatures of revision ${String(revision.number)}`}
    >
      {listed.map((s) => {
        const found = byId.get(s.id);
        return (
          <li key={s.id}>
            <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
              <path d="M12 3 4 6v6c0 4.5 3.4 8 8 9 4.6-1 8-4.5 8-9V6z" />
              <path d="m9 12 2 2 4-4" />
            </svg>
            <span>
              {s.present
                ? s.manifestation
                : `Signature ${s.id} is gone from the database: something outside Iki deleted it.`}
              {found?.reason ? <> Reason: “{found.reason}”</> : null}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

/** What a revision did and who did it, as its title says: “Edited by Ada Lovelace”, or of an
 * entry's revision made through a project the reader can't read, only what it set. */
export function revisionTitle(
  revision: Revision,
  labels: ReadonlyMap<string, string>,
  entry: boolean,
): string {
  const changed = revision.changed_fields.map((n) => labels.get(n) ?? n);
  // An entry's revision made through a project the reader can't read: what, never who,
  // how or when.
  if (revision.created_at === null) {
    return changed.length ? `Set ${changed.join(", ")}` : (KIND[revision.kind] ?? revision.kind);
  }
  const what = entry ? (KIND[revision.kind] ?? revision.kind) : revisionWhat(revision);
  return `${what} by ${personName(revision.user)}`;
}

/** What the trail says of a revision under its title: when, how and which fields, why,
 * its identity as it was given, and the signatures and holds it carries. Its own page says
 * the same. */
export function RevisionFacts({
  entity,
  revision,
  labels,
}: {
  /** The record, whose signatures set its states: none for a registry entry's own page. */
  entity: Entity | undefined;
  revision: Revision;
  labels: ReadonlyMap<string, string>;
}) {
  const changed = revision.changed_fields.map((n) => labels.get(n) ?? n);
  return (
    <>
      <p className="trail-meta">
        {revision.created_at ? (
          <>
            <UtcTime value={revision.created_at} /> · <Via revision={revision} />
          </>
        ) : (
          "Through a project you can’t read"
        )}
        {changed.length ? ` · ${changed.join(", ")}` : ""}
      </p>
      {revision.reason ? <p className="row-reason">“{revision.reason}”</p> : null}
      {/* Its identity as it was given, spelled other than its registry entry (ADR 0138). */}
      {revision.submitted ? (
        <p className="row-submitted">
          Given as{" "}
          {Object.entries(revision.submitted).map(([name, value], index) => (
            <span key={name}>
              {index ? "; " : ""}
              {labels.get(name) ?? name}: <code>{submittedText(value)}</code>
            </span>
          ))}
          , which its registry entry spells another way.
        </p>
      ) : null}
      {entity ? <SignedWith entity={entity} revision={revision} /> : null}
      <HoldsOpened revision={revision} />
    </>
  );
}
