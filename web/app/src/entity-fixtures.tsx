/**
 * Entity types, entities and revisions for the entity pages' tests, and a
 * way to render the app at an address.
 */

import type { Schemas } from "@ikisci/sdk";
import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, RouterProvider } from "@tanstack/react-router";
import { render, screen } from "@testing-library/react";
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

export type Field = Schemas["FieldOut"];

export function field(overrides: Partial<Field> & Pick<Field, "name" | "label" | "type">): Field {
  return {
    description: null,
    plugin: "protein",
    required: false,
    immutable: false,
    computed: false,
    unit: null,
    unit_symbol: null,
    display_unit: null,
    display_unit_symbol: null,
    display_scale: null,
    display_offset: null,
    input_units: [],
    to: null,
    values: [],
    items: null,
    ...overrides,
  };
}

export const PROTEIN: Schemas["EntityTypeOut"] = {
  name: "protein.Protein",
  plugin: "protein",
  label: "Protein",
  label_plural: "Proteins",
  description: "A protein sequence.",
  extends: null,
  abstract: false,
  subtypes: [],
  hrid_prefix: "PRT",
  identity: ["sequence"],
  on_duplicate: "reuse",
  values_schema: {},
  fields: [
    field({ name: "name", label: "Name", type: "string" }),
    field({ name: "sequence", label: "Sequence", type: "string", required: true, immutable: true }),
    field({
      name: "molecular_weight",
      label: "Molecular weight",
      type: "quantity",
      computed: true,
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
  name: "protein.ProteinStructure",
  label: "Protein structure",
  label_plural: "Protein structures",
  description: null,
  hrid_prefix: "PST",
  identity: [],
  on_duplicate: null,
  fields: [
    field({ name: "structure", label: "Structure", type: "file", required: true }),
    field({
      name: "resolution",
      label: "Resolution",
      type: "quantity",
      unit: "Ao",
      unit_symbol: "Å",
    }),
    field({ name: "chains", label: "Chains", type: "list", items: "ref", to: "protein.Protein" }),
  ],
};

export const KINASE = project();
export const ALICE = {
  id: ME.id,
  email: ME.email,
  name: ME.name,
  active: true,
  kind: "person" as const,
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
    type: "protein.Protein",
    revision: 2,
    owner_project: { id: KINASE.id, name: KINASE.name, slug: KINASE.slug },
    created_by: ALICE,
    created_at: "2026-03-14T09:00:00Z",
    updated_at: "2026-03-15T09:00:00Z",
    archived: false,
    archived_at: null,
    values,
    refs: {},
    files: {},
    can: { edit: true },
    ...overrides,
  };
}

export function revision(overrides: Partial<Schemas["RevisionOut"]> = {}): Schemas["RevisionOut"] {
  return {
    number: 1,
    kind: "created",
    changed_fields: ["name", "sequence"],
    reason: "",
    via: "web",
    user: ALICE,
    created_at: "2026-03-14T09:00:00Z",
    ...overrides,
  };
}

export function withTypes(): void {
  signedIn();
  answer("GET /api/v1/entity-types", [PROTEIN, STRUCTURE]);
}
