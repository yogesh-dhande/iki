/**
 * Entity types, entities and revisions for the entity pages' tests, and a
 * way to render the app at an address.
 */

import type { Schemas } from "@ikisci/sdk";
import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, RouterProvider } from "@tanstack/react-router";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { queryClient } from "./api";
import { createAppRouter } from "./router";
import { answer, ME, project, signedIn } from "./test-server";

/** The item at `index`, which the test expects to be there. */
export function nth<T>(items: readonly T[], index: number): T {
  const found = items[index];
  if (found === undefined) throw new Error(`No item ${String(index)} of ${String(items.length)}`);
  return found;
}

export async function renderApp(path: string) {
  const router = createAppRouter(createMemoryHistory({ initialEntries: [path] }));
  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  await screen.findByRole("heading", { level: 1 });
  return { router, user: userEvent.setup() };
}

/** Adds a grid filter by a field, from its "+ Filter" menu: the filter's editor opens,
 * and is returned. The field's menu item starts with its label. */
export async function addFilter(user: ReturnType<typeof userEvent.setup>, field: string) {
  await user.click(screen.getByRole("button", { name: "Filter" }));
  const menu = await screen.findByRole("menu", { name: "Filter" });
  const escaped = field.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  await user.click(within(menu).getByRole("menuitem", { name: new RegExp(`^${escaped}`) }));
  return screen.findByRole("dialog");
}

/** The menu of what a grid filters by: its fields' labels. */
export async function filterFields(user: ReturnType<typeof userEvent.setup>): Promise<string[]> {
  await user.click(screen.getByRole("button", { name: "Filter" }));
  const menu = await screen.findByRole("menu", { name: "Filter" });
  const items = within(menu)
    .getAllByRole("menuitem")
    .map((item) => item.querySelector("span")?.textContent ?? "");
  await user.keyboard("{Escape}");
  return items;
}

/** Opens a field's history on a record's page (ADR 0176), and gives its popover. */
export async function fieldHistory(
  user: ReturnType<typeof userEvent.setup>,
  label: string,
): Promise<HTMLElement> {
  await user.click(await screen.findByRole("button", { name: `History of ${label}` }));
  return screen.findByRole("dialog", { name: `History of ${label}` });
}

/** Chooses one of a record's ⋯ menu's actions. */
export async function moreAction(
  user: ReturnType<typeof userEvent.setup>,
  hrid: string,
  action: string,
): Promise<void> {
  await user.click(screen.getByRole("button", { name: `More actions for ${hrid}` }));
  await user.click(await screen.findByRole("menuitem", { name: action }));
}

export type Field = Schemas["FieldOut"];

export function field(overrides: Partial<Field> & Pick<Field, "name" | "label" | "type">): Field {
  return {
    role: null,
    role_label: null,
    assignment: false,
    assignment_field: null,
    assignment_states: [],
    description: null,
    plugin: "iki/protein",
    required: false,
    immutable: false,
    computed: false,
    unique: false,
    detached: false,
    link: null,
    unit: null,
    unit_symbol: null,
    display_unit: null,
    display_unit_symbol: null,
    display_scale: null,
    display_offset: null,
    input_units: [],
    to: [],
    values: [],
    items: null,
    initial: null,
    states: [],
    ...overrides,
  };
}

/** A file that states nothing of its instrument (ADR 0224), as most do. */
export const NO_FACTS: Schemas["FileFactsOut"] = {
  instrument: null,
  manufacturer: null,
  serial: null,
  instrument_name: null,
  operator: null,
  software: null,
  protocol: null,
};

export const PROTEIN: Schemas["EntityTypeOut"] = {
  name: "iki/protein.Protein",
  plugin: "iki/protein",
  label: "Protein",
  label_plural: "Proteins",
  description: "A protein sequence.",
  extends: null,
  abstract: false,
  sample: false,
  detached: false,
  subtypes: [],
  hrid_prefix: "PRT",
  identity: ["sequence"],
  viewers: [],
  notifications: [],
  values_schema: {},
  fields: [
    field({ name: "name", label: "Name", type: "string" }),
    field({
      name: "sequence",
      label: "Sequence",
      type: "string",
      format: "protein_sequence",
      required: true,
      immutable: true,
      registry: "identity",
    }),
    field({
      name: "molecular_weight",
      label: "Molecular weight",
      type: "quantity",
      computed: true,
      registry: "derived",
      unit: "u",
      unit_symbol: "u",
      display_unit: "ku",
      display_unit_symbol: "ku",
      display_scale: 0.001,
      display_offset: 0,
    }),
  ],
};

export const STRUCTURE: Schemas["EntityTypeOut"] = {
  ...PROTEIN,
  name: "iki/protein.ProteinStructure",
  label: "Protein structure",
  label_plural: "Protein structures",
  description: null,
  hrid_prefix: "PST",
  identity: [],
  fields: [
    field({ name: "structure", label: "Structure", type: "file", required: true }),
    field({
      name: "resolution",
      label: "Resolution",
      type: "quantity",
      unit: "Ao",
      unit_symbol: "Å",
    }),
    field({
      name: "chains",
      label: "Chains",
      type: "list",
      items: "ref",
      to: ["iki/protein.Protein"],
    }),
  ],
};

export const COLLECTION_TYPE: Schemas["EntityTypeOut"] = {
  ...PROTEIN,
  name: "iki/core.Collection",
  plugin: "iki/core",
  label: "Collection",
  label_plural: "Collections",
  description: "A named set of entities, listed one by one or found by a search.",
  hrid_prefix: "COL",
  identity: [],

  fields: [
    field({ name: "name", label: "Name", type: "string", required: true, plugin: "iki/core" }),
    field({ name: "description", label: "Description", type: "text", plugin: "iki/core" }),
    field({
      name: "members",
      label: "Members",
      type: "list",
      items: "ref",
      to: ["iki/core.Entity"],
      plugin: "iki/core",
    }),
    field({ name: "search", label: "Search", type: "json", plugin: "iki/core" }),
  ],
};

export const KINASE = project();
export const ALICE = {
  id: ME.id,
  email: ME.email,
  name: ME.name,
  active: true,
  kind: "person" as const,
  imported_from: "",
};
export const PROTEIN_ID = "00000000-0000-7000-8000-00000000e001";
export const FILE_ID = "00000000-0000-7000-8000-00000000f001";

export function entity(overrides: Partial<Schemas["EntityOut"]> = {}): Schemas["EntityOut"] {
  const values = overrides.values ?? {
    name: "ABL1 kinase",
    sequence: "MLEICLKLVG",
    molecular_weight: 52341.5,
  };
  const hrid = overrides.hrid ?? "PRT-000001";
  return {
    id: PROTEIN_ID,
    hrid,
    title: typeof values["name"] === "string" ? values["name"] : hrid,
    type: "iki/protein.Protein",
    revision: 2,
    owner_project: { id: KINASE.id, name: KINASE.name, slug: KINASE.slug },
    created_by: ALICE,
    created_at: "2026-03-14T09:00:00Z",
    updated_at: "2026-03-15T09:00:00Z",
    archived: false,
    archived_at: null,
    locked: false,
    locked_by: [],
    values,
    refs: {},
    files: {},
    can: {
      edit: true,
      correct: false,
      edit_amount: false,
      draw: false,
      states: {},
      sign: [],
    },
    ...overrides,
  };
}

export function revision(overrides: Partial<Schemas["RevisionOut"]> = {}): Schemas["RevisionOut"] {
  return {
    number: 1,
    kind: "created",
    entry: false,
    changed_fields: ["name", "sequence"],
    reason: "",
    via: "web",
    client: "",
    agent: null,
    run_withheld: false,
    user: ALICE,
    state: null,
    created_at: "2026-03-14T09:00:00Z",
    ...overrides,
  };
}

export function withTypes(): void {
  signedIn();
  answer("GET /api/v1/entity-types", [COLLECTION_TYPE, PROTEIN, STRUCTURE]);
  answer("GET /api/v1/components", []);
}
