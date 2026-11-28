/**
 * Quarantine (ADR 0216, 0217): what the malware scan holds, and why, in words people use.
 * Admin → Quarantine lists the held files for Quarantine Admins; a file's page and the
 * file lists say a file is held, and send Quarantine Admins to review it.
 */

import { unwrap, type Schemas } from "@ikisci/sdk";
import { infiniteQueryOptions, queryOptions } from "@tanstack/react-query";

import { iki } from "./api";

export type Quarantined = Schemas["QuarantineOut"];
export type HeldCopy = Schemas["HeldCopyOut"];
export type Scan = Schemas["FileScanOut"];

export const QUARANTINE = ["quarantine"] as const;

export type QuarantineStatus = "held" | "released";
export type QuarantineState = "infected" | "not_scanned";

export interface QuarantineFilter {
  status: QuarantineStatus;
  state: QuarantineState | undefined;
  q: string;
}

/** The list's filters, as its address holds them: `?status=released&state=infected&q=…`. */
export interface QuarantineSearch {
  status?: "released";
  state?: QuarantineState;
  q?: string;
}

export function validateQuarantineSearch(search: Record<string, unknown>): QuarantineSearch {
  const state = search["state"];
  const q = search["q"];
  return {
    ...(search["status"] === "released" ? { status: "released" as const } : {}),
    ...(state === "infected" || state === "not_scanned" ? { state } : {}),
    ...(typeof q === "string" && q.trim() ? { q: q.trim().slice(0, 255) } : {}),
  };
}

export function quarantineFilterOf(search: QuarantineSearch): QuarantineFilter {
  return { status: search.status ?? "held", state: search.state, q: search.q ?? "" };
}

export function quarantineSearchOf(filter: QuarantineFilter): QuarantineSearch {
  return {
    ...(filter.status === "released" ? { status: "released" as const } : {}),
    ...(filter.state ? { state: filter.state } : {}),
    ...(filter.q ? { q: filter.q } : {}),
  };
}

export function quarantineQuery(filter: QuarantineFilter) {
  return infiniteQueryOptions({
    queryKey: [...QUARANTINE, "list", filter],
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam, signal }) =>
      unwrap(
        await iki.GET("/api/v1/quarantine-decisions", {
          params: {
            query: {
              limit: 50,
              released: filter.status === "released",
              ...(filter.state ? { state: filter.state } : {}),
              ...(filter.q ? { q: filter.q } : {}),
              ...(pageParam ? { cursor: pageParam } : {}),
            },
          },
          signal,
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
}

export function quarantinedQuery(id: string) {
  return queryOptions({
    queryKey: [...QUARANTINE, "one", id],
    queryFn: async ({ signal }) =>
      unwrap(
        await iki.GET("/api/v1/quarantine-decisions/{decision_id}", {
          params: { path: { decision_id: id } },
          signal,
        }),
      ),
  });
}

/** Where a Quarantine Admin reviews a decision. */
export function reviewHref(decisionId: string): string {
  return `/admin/quarantine/${decisionId}/`;
}

/** Why a scan held a file: a short label, and a sentence that says what it means. */
export interface HeldBecause {
  label: string;
  detail: string;
  /** The scanner's own name for what it found, to show as code: a signature's, say. */
  finding?: string;
}

export function heldBecause(scan: { state: string; reason: string }): HeldBecause {
  const reason = scan.reason;
  if (scan.state === "infected") {
    return {
      label: "Malware found",
      detail: "The scanner matched one of its malware signatures.",
      finding: reason,
    };
  }
  if (reason === "too_large") {
    return {
      label: "Too large to scan",
      detail:
        "It’s larger than the deployment’s maximum scan size, and the deployment holds such files rather than let them be used unscanned.",
    };
  }
  if (reason === "clamd_stream_limit") {
    return {
      label: "Too large for the scanner",
      detail: "The scanner stopped reading it at its own size limit, so it didn’t see all of it.",
    };
  }
  if (reason === "unsupported_source") {
    return {
      label: "Couldn’t be read to scan",
      detail:
        "It’s registered where Iki can’t read one fixed version of it, so there’s nothing that could be scanned once and stay the same.",
    };
  }
  if (reason.startsWith("Heuristics.Encrypted.")) {
    return {
      label: "Encrypted",
      detail: "The scanner can’t look inside encrypted content.",
      finding: reason,
    };
  }
  if (reason.startsWith("Heuristics.Limits.Exceeded")) {
    return {
      label: "Beyond the scanner’s limits",
      detail:
        "It holds more files, or files nested more deeply, than the scanner inspects, so it didn’t see all of it.",
      finding: reason,
    };
  }
  return {
    label: "Scan incomplete",
    detail: "The scanner couldn’t inspect all of it.",
    finding: reason,
  };
}

/** A file's scan, in a word or two, where a list or a picker names the file: none for a
 * file that can be used as it is. */
export function scanFlag(scan: Scan): { text: string; tone: string } | undefined {
  if (scan.state === "pending") return { text: "Scanning", tone: "badge-busy" };
  if (scan.usable) return undefined;
  if (scan.state === "infected") return { text: "Quarantined", tone: "badge-bad" };
  return { text: "Held: not scanned", tone: "badge-bad" };
}

export const COPY_KINDS: Record<HeldCopy["kind"], string> = {
  file: "File",
  artifact: "Run artifact",
  checkpoint: "Run checkpoint",
  bundle: "Bundle to import",
};
