/**
 * Changing something from the revision the person read (If-Match, ADR 0035): one hook for
 * every dialog and form that does. What it changes is kept as it was read when it opened,
 * and the change is made from that revision, never from one a refetch brought in while it
 * was open, which nobody reviewed.
 *
 * If it moved on meanwhile, nothing is sent from the old revision and nothing is saved on
 * one nobody saw: confirming catches up instead, and so does a change the server refused
 * as stale (412), once the page has read it again. Either way the dialog says it changed
 * (`changed`), a form brings the new values in where the person hadn't typed
 * (`onCatchUp`), and the next confirm is made from the revision they now see.
 */

import { ifMatch, ifNoneMatchAny, isStale, type Problem } from "@ikisci/sdk";
import { useQueryClient, type QueryKey } from "@tanstack/react-query";
import { useEffect, useState } from "react";

/** The header a change is made from a revision with (the SDK's `ifMatch`). */
export type IfMatch = ReturnType<typeof ifMatch>;

/** The header a change is made with when nothing was recorded yet, `If-None-Match: *` (ADR
 * 0289), or else from the revision read. */
export type Precondition = IfMatch | ReturnType<typeof ifNoneMatchAny>;

export interface Revisioned<T, H = IfMatch> {
  /** As it was read when it opened (or caught up to since): what the person reviewed. */
  read: T;
  /** The page's copy moved on since it was read: someone changed it meanwhile. */
  changedSince: boolean;
  /** It changed while open, and has been caught up to: the dialog says so, and the person
   * confirms again, from the revision they now see. */
  changed: boolean;
  /** A change was refused as stale, and the page is reading it again. */
  rereading: boolean;
  /** Makes the change from the revision read, unless it moved on: then it catches up,
   * sends nothing, and waits for the person to confirm again. `send` is given the header
   * and what was read, which the change is built from. */
  submit: (send: (header: H, read: T) => void) => void;
}

export interface RevisionedOptions<T> {
  /** For a dialog that stays mounted while closed: read again each time it opens. */
  open?: boolean;
  /** The change's error, if it failed: a stale one starts catching up. One from before it
   * opened isn't this change's. */
  error?: unknown;
  /** What to read again when a change is refused as stale: the queries `live` comes from. */
  refresh: QueryKey;
  /** What the change is made from, when that's more than its revision, such as a
   * signature's record and registry entry revisions: a change to any of it is a change. It
   * changes whenever what the change sends does. */
  version?: (item: NonNullable<T>) => string | number;
  /** It's caught up to `latest` from `was`: a form brings the new values in. Called while
   * rendering, so it may only set its own component's state. */
  onCatchUp?: (latest: T, was: T) => void;
}

interface State<T> {
  /** Open, and read: `read` is what the person reviews. */
  opened: boolean;
  read: T;
  told: boolean;
  /** Refused as stale, and not caught up since: it catches up when the page's copy moves. */
  refused: boolean;
  /** Reading it again after a refusal. */
  awaiting: boolean;
  /** The last error looked at, so each refusal starts catching up once. */
  seen: unknown;
}

/**
 * `live`: the page's copy, as its query has it now. `null` is nothing recorded yet, such as
 * an amount never counted: a change from it is sent with `If-None-Match: *`, so one
 * recorded meanwhile refuses it as stale, as a new revision would.
 */
export function useRevisioned<T extends { revision: number }>(
  live: T,
  options: RevisionedOptions<T>,
): Revisioned<T>;
export function useRevisioned<T extends { revision: number }>(
  live: T | null,
  options: RevisionedOptions<T | null>,
): Revisioned<T | null, Precondition>;
export function useRevisioned<T extends { revision: number }>(
  live: T | null,
  { open = true, error, refresh, version, onCatchUp }: RevisionedOptions<T | null>,
): Revisioned<T | null, Precondition> {
  const client = useQueryClient();
  const versionOf = (item: T | null): string | number =>
    item === null ? "none" : version ? version(item) : item.revision;
  const [state, setState] = useState<State<T | null>>(() => ({
    opened: open,
    read: live,
    told: false,
    refused: false,
    awaiting: false,
    seen: error,
  }));
  // Adjusted while rendering, as React keeps state that follows a prop.
  let next = state;
  if (!open) {
    if (state.opened || state.told || state.refused || state.awaiting) {
      next = { ...state, opened: false, told: false, refused: false, awaiting: false };
    }
  } else {
    // Opened: read now, and an earlier error, from before it opened, isn't this change's.
    if (!next.opened) next = { ...next, opened: true, read: live, told: false, seen: error };
    if (error !== next.seen) {
      const stale = isStale(error);
      next = {
        ...next,
        seen: error,
        ...(stale ? { refused: true, awaiting: true, told: true } : {}),
      };
    }
    const was = next.read;
    // Refused: caught up once the page reads it again.
    if (next.refused && versionOf(live) !== versionOf(was)) {
      onCatchUp?.(live, was);
      next = { ...next, read: live, refused: false, awaiting: false };
    }
  }
  if (next !== state) setState(next);

  // Refused as stale: the page reads it again, which catches up (above) once it comes.
  const awaiting = next.awaiting;
  useEffect(() => {
    if (!awaiting) return;
    let current = true;
    void client.invalidateQueries({ queryKey: refresh }).finally(() => {
      // Read again: no longer busy. It catches up (above) when the new copy renders, which
      // may come after this; if it never differs (what moved on isn't what it's compared
      // by), a retry is made from it as it is, rather than waiting for ever.
      if (current) setState((s) => (s.awaiting ? { ...s, awaiting: false } : s));
    });
    return () => {
      current = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once per refusal
  }, [awaiting]);

  const read = next.opened ? next.read : live;
  const changedSince = versionOf(live) !== versionOf(read);
  return {
    read,
    changedSince,
    changed: next.told,
    rereading: awaiting,
    submit: (send) => {
      if (awaiting) return;
      if (open && changedSince) {
        onCatchUp?.(live, read);
        setState((s) => ({ ...s, read: live, told: true, refused: false }));
        return;
      }
      if (next.told || next.refused) setState((s) => ({ ...s, told: false, refused: false }));
      send(read === null ? ifNoneMatchAny() : ifMatch(read.revision), read);
    },
  };
}

/** What a dialog shows once it's caught up: that it changed, and what to do. */
export function changedProblem(detail: string): Problem {
  return { type: "about:blank", title: "It changed", status: 412, code: "stale", detail };
}
