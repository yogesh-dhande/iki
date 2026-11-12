/** The Costs page's address: which month, grouped how. Kept apart from the page, which
 * loads only when it's opened. */

import type { OrgCostsBy } from "./costs";

export interface CostsSearch {
  month?: string;
  by?: OrgCostsBy;
}

export const BY: readonly { value: OrgCostsBy; label: string }[] = [
  { value: "project", label: "Project" },
  { value: "team", label: "Team" },
  { value: "action", label: "Action" },
  { value: "template", label: "Compute template" },
  { value: "tier", label: "Tier" },
];

/** `?month=2026-11&by=team`: anything else is this month, by project. */
export function validateCostsSearch(search: Record<string, unknown>): CostsSearch {
  const found: CostsSearch = {};
  const month = search["month"];
  if (typeof month === "string" && /^\d{4}-(0[1-9]|1[0-2])$/.test(month)) found.month = month;
  const by = BY.find((option) => option.value === search["by"]);
  if (by && by.value !== "project") found.by = by.value;
  return found;
}
