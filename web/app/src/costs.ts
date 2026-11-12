/**
 * What runs cost (ADR 0070): money and machine time as people read them, and the
 * organization's and a project's cost reports.
 */

import { unwrap, type Schemas } from "@ikisci/sdk";
import { queryOptions } from "@tanstack/react-query";

import { iki } from "./api";

export type CostSummary = Schemas["CostSummaryOut"];
export type OrgCostsBy = "project" | "team" | "template" | "action" | "tier";
export type ProjectCostsBy = "action" | "template";

/** An amount in its currency: to the cent, and "less than $0.01" for less but not none. */
export function money(amount: number, currency: string): string {
  const format = (value: number) =>
    new Intl.NumberFormat("en-GB", {
      style: "currency",
      currency,
      currencyDisplay: "narrowSymbol",
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    }).format(value);
  if (amount > 0 && amount < 0.005) return `less than ${format(0.01)}`;
  return format(amount);
}

/** Machine time: "45 s", "12 min", "3 h 5 min". */
export function machineTime(seconds: number): string {
  const whole = Math.max(0, Math.round(seconds));
  if (whole < 60) return `${String(whole)} s`;
  const minutes = Math.round(whole / 60);
  if (minutes < 60) return `${String(minutes)} min`;
  const hours = Math.floor(minutes / 60);
  return minutes % 60 ? `${String(hours)} h ${String(minutes % 60)} min` : `${String(hours)} h`;
}

/** A report's period as the API takes it: `since` up to, not including, `until`. */
export interface Period {
  since: string;
  until: string;
}

export function orgCostsQuery(by: OrgCostsBy, period: Period) {
  return queryOptions({
    queryKey: ["costs", "org", by, period.since, period.until],
    queryFn: async () =>
      unwrap(await iki.GET("/api/v1/compute/costs", { params: { query: { by, ...period } } })),
  });
}

export function projectCostsQuery(projectId: string, by: ProjectCostsBy = "action") {
  return queryOptions({
    queryKey: ["costs", "project", projectId, by],
    queryFn: async () =>
      unwrap(
        await iki.GET("/api/v1/projects/{project_id}/costs", {
          params: { path: { project_id: projectId }, query: { by } },
        }),
      ),
  });
}

/** The address of the CSV of every cost record in the period. */
export function recordsUrl(period: Period): string {
  const query = new URLSearchParams({ since: period.since, until: period.until });
  return `/api/v1/compute/costs/records.csv?${query.toString()}`;
}

/** The first day of this month and of next, in UTC, as the API's default period. */
export function thisMonth(now: Date = new Date()): { since: string; until: string } {
  const since = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 1));
  const until = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 1, 1));
  return { since: since.toISOString().slice(0, 10), until: until.toISOString().slice(0, 10) };
}

/** A month back from `since`, or forward: for stepping through months. */
export function shiftMonth(since: string, by: number): { since: string; until: string } {
  const [year, month] = since.split("-").map(Number) as [number, number];
  return thisMonth(new Date(Date.UTC(year, month - 1 + by, 1)));
}

/** "November 2026", for a month's period. */
export function monthName(since: string): string {
  return new Intl.DateTimeFormat("en-GB", {
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  }).format(new Date(`${since}T00:00:00Z`));
}
