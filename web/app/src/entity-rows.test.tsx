/**
 * Lists of rows (ADR 0137): typed in a row at a time, each row's fields checked and told
 * what the API said of them, shown as a table, and searched for a row at a time.
 */

import type { Schemas } from "@ikisci/sdk";
import { screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { valueText } from "./entities";
import { entity, field, KINASE, PROTEIN, renderApp } from "./entity-fixtures";
import { encode, toDraft } from "./entity-values";
import { searchWords } from "./filters";
import { answer, calls, page, problem, respond, signedIn } from "./test-server";

const HEAVY = "00000000-0000-7000-8000-00000000b001";

const part = field({
  name: "part",
  label: "Part",
  type: "ref",
  to: ["iki/protein.Protein"],
  required: true,
});
const copies = field({ name: "copies", label: "Copies", type: "int" });
const role = field({
  name: "role",
  label: "Role",
  type: "enum",
  values: ["heavy", "light"],
  required: true,
});

const COMPONENTS = {
  ...field({ name: "components", label: "Components", type: "list", items: "row" }),
  row: {
    fields: [part, copies, role],
    identity: [],
    by: "role",
    roles: [
      { name: "heavy", label: "Heavy", min: 2, max: 2, to: { part: ["iki/protein.Protein"] } },
    ],
  },
};

const BISPECIFIC: Schemas["EntityTypeOut"] = {
  ...PROTEIN,
  name: "lab.Bispecific",
  plugin: "acme/lab",
  label: "Bispecific",
  label_plural: "Bispecifics",
  description: null,
  hrid_prefix: "BSP",
  identity: [],
  values_schema: {
    properties: {
      name: { type: "string", minLength: 1, title: "Name" },
      components: {
        type: "array",
        items: {
          type: "object",
          properties: {
            part: { type: "string", format: "uuid" },
            copies: { anyOf: [{ type: "integer", minimum: 1 }, { type: "null" }], default: 1 },
            role: { type: "string", enum: ["heavy", "light"] },
          },
        },
      },
    },
  },
  fields: [field({ name: "name", label: "Name", type: "string", required: true }), COMPONENTS],
};

const ENTITY = { refs: {}, files: {}, people: {} };

afterEach(() => {
  vi.restoreAllMocks();
});

describe("a list of rows' values", () => {
  it("encodes each row's fields, and says which one is wrong", () => {
    const drafts = toDraft(
      COMPONENTS,
      [
        { part: HEAVY, copies: 2, role: "heavy" },
        { part: HEAVY, copies: null, role: "light" },
      ],
      ENTITY,
    );
    expect(encode(COMPONENTS, drafts, BISPECIFIC)).toEqual({
      ok: true,
      value: [
        { part: HEAVY, copies: 2, role: "heavy" },
        { part: HEAVY, role: "light" },
      ],
    });
    const missing = toDraft(COMPONENTS, [{ copies: 0, role: "heavy" }], ENTITY);
    expect(encode(COMPONENTS, missing, BISPECIFIC)).toEqual({
      ok: false,
      error: "Row 1: Part: Choose part.",
    });
    const few = toDraft(COMPONENTS, [{ part: HEAVY, copies: 0, role: "heavy" }], ENTITY);
    expect(encode(COMPONENTS, few, BISPECIFIC)).toEqual({
      ok: false,
      error: "Row 1: Copies: It must be at least 1.",
    });
  });

  it("reads as a row's values, rows apart", () => {
    const shown = valueText(
      COMPONENTS,
      [
        { part: HEAVY, copies: 2, role: "heavy" },
        { part: HEAVY, copies: null, role: "light" },
      ],
      {
        ...ENTITY,
        refs: {
          [HEAVY]: {
            hrid: "PRT-000001",
            title: "Heavy",
            type: "p",
            archived: false,
            purged: false,
          },
        },
      },
    );
    expect(shown).toBe("PRT-000001 · 2 · heavy; PRT-000001 · light");
  });

  it("is searched a row at a time from the search box", () => {
    expect(searchWords("components.part:PRT-000001 components.role:heavy bs")).toEqual({
      text: "bs",
      conditions: [
        { field: "components", op: "has", value: { part: "PRT-000001", role: "heavy" } },
      ],
    });
  });
});

describe("typing rows in", () => {
  it("adds a row of inputs, and shows the API's message on the row's field", async () => {
    signedIn();
    answer("GET /api/v1/entity-types", [PROTEIN, BISPECIFIC]);
    answer("GET /api/v1/projects", page([KINASE]));
    answer("GET /api/v1/entities", page([]));
    answer(
      "POST /api/v1/entities",
      problem(422, "validation_failed", "Some values are invalid.", [
        { pointer: "#/values/components", detail: "needs at least 2 heavy rows, and has 0." },
        { pointer: "#/values/components/0/role", detail: "must be heavy here." },
      ]),
    );
    const { user } = await renderApp("/projects/kinase-atlas/entities/new/?type=lab.Bispecific");
    await user.type(screen.getByRole("textbox", { name: "Name" }), "BS-1");
    await user.click(screen.getByRole("button", { name: "Add a row to Components" }));
    // The new row's first field has the focus, and its picker opens: once it has, close it.
    const picker = await screen.findByRole("combobox", { expanded: true });
    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(picker).toHaveAttribute("aria-expanded", "false");
    });
    const row = screen.getByRole("group", { name: "Components, row 1" });
    // Copies starts at its default; Role is chosen.
    expect(within(row).getByRole("textbox", { name: /Copies/ })).toHaveValue("1");
    await user.selectOptions(within(row).getByRole("combobox", { name: /Role/ }), "light");
    await user.click(screen.getByRole("button", { name: "Create bispecific" }));
    // Part is required: the browser says so before sending.
    expect(await screen.findByText(/Row 1: Part: Choose part\./)).toBeInTheDocument();
    expect(calls("POST", "/api/v1/entities")).toHaveLength(0);
  });

  it("names the row's field the API points at", async () => {
    signedIn();
    answer("GET /api/v1/entity-types", [PROTEIN, BISPECIFIC]);
    answer("GET /api/v1/projects", page([KINASE]));
    answer(
      "GET /api/v1/entities",
      page([entity({ id: HEAVY, hrid: "PRT-000001", values: { name: "Heavy" } })]),
    );
    answer(
      "POST /api/v1/entities",
      problem(422, "validation_failed", "Some values are invalid.", [
        { pointer: "#/values/components/0/role", detail: "must be heavy here." },
      ]),
    );
    const { user } = await renderApp("/projects/kinase-atlas/entities/new/?type=lab.Bispecific");
    await user.type(screen.getByRole("textbox", { name: "Name" }), "BS-1");
    await user.click(screen.getByRole("button", { name: "Add a row to Components" }));
    // The new row's first field has the focus, and its picker opens: once it has, close it.
    const picker = await screen.findByRole("combobox", { expanded: true });
    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(picker).toHaveAttribute("aria-expanded", "false");
    });
    const row = screen.getByRole("group", { name: "Components, row 1" });
    await user.type(within(row).getByRole("combobox", { name: /Part/ }), "hea");
    await user.click(await screen.findByRole("option", { name: /Heavy/ }));
    await user.selectOptions(within(row).getByRole("combobox", { name: /Role/ }), "light");
    await user.click(screen.getByRole("button", { name: "Create bispecific" }));
    const chosen = within(row).getByRole("combobox", { name: /Role/ });
    await waitFor(() => {
      expect(chosen).toHaveAccessibleDescription(/must be heavy here\./);
    });
    const [sent] = calls("POST", "/api/v1/entities");
    expect((sent?.body as { values: unknown }).values).toEqual({
      name: "BS-1",
      components: [{ part: HEAVY, copies: 1, role: "light" }],
    });
  });
});

describe("a record's rows", () => {
  it("shows them as a table", async () => {
    signedIn();
    answer("GET /api/v1/entity-types", [PROTEIN, BISPECIFIC]);
    answer("GET /api/v1/projects", page([KINASE]));
    const made = entity({
      hrid: "BSP-000001",
      type: "lab.Bispecific",
      values: {
        name: "BS-1",
        components: [
          { part: HEAVY, copies: 2, role: "heavy" },
          { part: HEAVY, copies: null, role: "light" },
        ],
      },
      refs: {
        [HEAVY]: {
          hrid: "PRT-000001",
          title: "Heavy",
          type: "iki/protein.Protein",
          archived: false,
          purged: false,
        },
      },
    });
    answer("GET /api/v1/entities/BSP-000001", made);
    answer("GET /api/v1/entities/BSP-000001/provenance", {});
    answer("GET /api/v1/entities/BSP-000001/projects", []);
    answer("GET /api/v1/entities/BSP-000001/making", respond(200, []));
    await renderApp("/entities/BSP-000001/");
    const table = await screen.findByRole("table", { name: "Components" });
    const rows = within(table).getAllByRole("row");
    expect(rows.map((r) => r.textContent)).toEqual([
      "PartCopiesRole",
      "Heavy PRT-0000012heavy",
      "Heavy PRT-000001—light",
    ]);
  });
});
