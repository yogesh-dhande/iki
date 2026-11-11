/** A new run's address: `?action=…&entities=PRT-000001,PRT-000002`. */

export interface NewRunSearch {
  action?: string | undefined;
  /** HRIDs, comma-separated. */
  entities?: string | undefined;
}

const HRIDS = /^[A-Za-z0-9-]+(,[A-Za-z0-9-]+)*$/;

export function validateNewRunSearch(search: Record<string, unknown>): NewRunSearch {
  const out: NewRunSearch = {};
  if (typeof search["action"] === "string" && search["action"]) out.action = search["action"];
  const entities = search["entities"];
  if (typeof entities === "string" && HRIDS.test(entities)) out.entities = entities.toUpperCase();
  return out;
}
