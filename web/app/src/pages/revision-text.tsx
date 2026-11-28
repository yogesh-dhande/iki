/**
 * How a record's page says a revision: what it did, how it was made, and when, in UTC as
 * an inspector reads a trail. The overview's field histories and the Audit trail tab say
 * it the same way (ADR 0176).
 */

import { Link } from "@tanstack/react-router";

import { Through } from "../agent";
import type { Revision } from "../entities";

export const VIA: Record<string, string> = {
  web: "in the web app",
  api: "through the API",
  mcp: "by an AI assistant (MCP)",
  cli: "from the command line",
  scim: "by provisioning",
  system: "by Iki",
};

/** How a revision was made; a run's links to the run. One the reader can't read is said
 * to be hidden; one with no run recorded, as one imported from elsewhere, says so. */
export function Via({ revision }: { revision: Revision }) {
  if (revision.via === "run") {
    if (revision.run) {
      return (
        <>
          by{" "}
          <Link to="/runs/$ref/" params={{ ref: revision.run.hrid }}>
            {revision.run.hrid}
          </Link>
        </>
      );
    }
    return revision.run_withheld ? (
      <>by a run in a project you can’t see</>
    ) : (
      <>by a run not recorded here</>
    );
  }
  const how = VIA[revision.via] ?? revision.via;
  // The agent it came through, if any: an AI assistant's MCP client, say.
  return (
    <>
      {how}
      <Through agent={revision.agent} />
    </>
  );
}

export const KIND: Record<string, string> = {
  created: "Created",
  edited: "Edited",
  migrated: "Migrated",
  archived: "Archived",
  restored: "Restored",
  curated: "Facts changed",
  superseded: "Superseded",
  moved: "Moved",
  state_changed: "State set",
};

/** What a revision did: its kind, or for a state change (ADR 0125), which field, from
 * which state to which: “Status set to Approved, from In review”. */
export function revisionWhat(revision: Revision): string {
  const s = revision.state;
  if (!s) return KIND[revision.kind] ?? revision.kind;
  const set = `${s.field.label} set to ${s.to.label}`;
  return s.from ? `${set}, from ${s.from.label}` : set;
}

const UTC = new Intl.DateTimeFormat("en-GB", {
  day: "numeric",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
  timeZone: "UTC",
});

/** “19 Nov 2026, 16:40 UTC”: a trail's times are in UTC, whoever reads them. */
export function utcText(value: string): string {
  return `${UTC.format(new Date(value))} UTC`;
}

/** A time in UTC, as a trail says it, marked up as a time. */
export function UtcTime({ value }: { value: string }) {
  return <time dateTime={value}>{utcText(value)}</time>;
}
