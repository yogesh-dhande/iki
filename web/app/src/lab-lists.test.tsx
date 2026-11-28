/**
 * Lab lists (ADR 0270) against a fake API: a record's dropdown of a list's active
 * options, which keeps an option archived since; how a record, the grid and a sheet read
 * an option; filtering by one; and a list's page, where its curators add, change,
 * reorder, archive and supersede options, each with a reason, and a project's Editors
 * move their records off a superseded one.
 */

import type { Schemas } from "@ikisci/sdk";
import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { queryClient } from "./api";
import { valueText } from "./entities";
import { entity, field, KINASE, renderApp } from "./entity-fixtures";
import { filterable } from "./filters";
import { matching, optionText } from "./lab-lists";
import { movedTo } from "./pages/list-options";
import { FilterBar } from "./pages/entity-filters";
import { cellText } from "./sheet";
import {
  answer,
  calls,
  made,
  MAIN_REGISTRY,
  page,
  problem,
  project,
  respond,
  signedIn,
  type Sent,
} from "./test-server";

const METHOD = "acme/protein.PurificationMethod";
const CATEGORY = "acme/protein.MethodCategory";
const PREP_TYPE = "acme/protein.Prep";
const OPTIONS = `/api/v1/lists/${METHOD}/options`;

function listType(
  name: string,
  label: string,
  plural: string,
  facts: Schemas["FieldOut"][] = [],
): Schemas["EntityTypeOut"] {
  return {
    name,
    plugin: "acme/protein",
    label,
    label_plural: plural,
    description: null,
    extends: null,
    abstract: false,
    sample: false,
    lab_list: true,
    detached: false,
    subtypes: [],
    hrid_prefix: null,
    identity: ["code"],
    viewers: [],
    notifications: [],
    values_schema: {
      properties: {
        code: { type: "string", maxLength: 64, pattern: "^[A-Za-z0-9][A-Za-z0-9_.+-]*$" },
        label: { type: "string", maxLength: 200 },
        description: { anyOf: [{ type: "string", maxLength: 2000 }, { type: "null" }] },
        sop: { anyOf: [{ type: "string", format: "uri" }, { type: "null" }] },
        category: { anyOf: [{ type: "string", format: "uuid" }, { type: "null" }] },
      },
    },
    fields: [
      field({ name: "code", label: "Code", type: "string", required: true, immutable: true }),
      field({ name: "label", label: "Label", type: "string", required: true, registry: "fact" }),
      field({ name: "description", label: "Description", type: "text", registry: "fact" }),
      field({ name: "position", label: "Position", type: "int", registry: "fact" }),
      ...facts,
    ],
  };
}

const CATEGORY_TYPE = listType(CATEGORY, "Method category", "Method categories");
const METHOD_TYPE = listType(METHOD, "Purification method", "Purification methods", [
  field({ name: "category", label: "Category", type: "ref", to: [CATEGORY], registry: "fact" }),
  field({ name: "sop", label: "SOP", type: "string", format: "uri", registry: "fact" }),
]);

const PREP: Schemas["EntityTypeOut"] = {
  ...listType(PREP_TYPE, "Prep", "Preps"),
  lab_list: false,
  hrid_prefix: "PRP",
  identity: [],
  values_schema: {
    properties: {
      name: { type: "string", minLength: 1, maxLength: 100 },
      method: { anyOf: [{ type: "string", format: "uuid" }, { type: "null" }] },
      polishing: { type: "array", maxItems: 5, items: { type: "string", format: "uuid" } },
    },
  },
  fields: [
    field({ name: "name", label: "Name", type: "string", required: true }),
    field({ name: "method", label: "Method", type: "ref", to: [METHOD] }),
    field({ name: "polishing", label: "Polishing", type: "list", items: "ref", to: [METHOD] }),
  ],
};

const id = (n: number) => `00000000-0000-7000-8000-0000000c${String(n).padStart(4, "0")}`;

function option(
  n: number,
  code: string,
  label: string,
  more: Partial<Schemas["OptionOut"]> = {},
): Schemas["OptionOut"] {
  return {
    id: id(n),
    hrid: `OPTION-00000${String(n)}`,
    code,
    label,
    description: "",
    position: n,
    archived: false,
    superseded_by: null,
    values: {},
    revision: 1,
    uses: null,
    ...more,
  };
}

const CHROMATOGRAPHY = { ...option(9, "chromatography", "Chromatography"), hrid: "OPTION-000009" };
const PROTEIN_A = option(1, "protein_a", "Protein A affinity", {
  description: "Captured on a Protein A column and eluted at low pH.",
  values: { category: CHROMATOGRAPHY.id, sop: "https://sop.example.com/PUR-001" },
});
const SEC = option(2, "sec", "Size exclusion", { values: { category: CHROMATOGRAPHY.id } });
const DIALYSIS = option(3, "dialysis", "Dialysis", { archived: true });
const EVERY = [PROTEIN_A, SEC, DIALYSIS];

function list(over: Partial<Schemas["ListOut"]> = {}): Schemas["ListOut"] {
  return {
    type: METHOD,
    plugin: "acme/protein",
    label: "Purification method",
    label_plural: "Purification methods",
    description: null,
    registry: MAIN_REGISTRY,
    options: 2,
    detached: false,
    can: { curate: false },
    ...over,
  };
}

function registry(over: Partial<Schemas["RegistryOut"]> = {}): Schemas["RegistryOut"] {
  return {
    ...MAIN_REGISTRY,
    description: "",
    default: true,
    all_teams: true,
    teams: [],
    created_at: "2026-01-01T00:00:00Z",
    can: { manage: false, curate: false, curate_lists: false },
    ...over,
  };
}

/** The list's options: the active ones, or with `archived`, every one. */
function onOptions(every: Schemas["OptionOut"][] = EVERY): void {
  answer(`GET ${OPTIONS}`, ({ query }: Sent) =>
    query.get("archived") === "true" ? every : every.filter((o) => !o.archived && !o.superseded_by),
  );
  answer(`GET /api/v1/lists/${CATEGORY}/options`, [CHROMATOGRAPHY]);
}

function onLab(): void {
  signedIn();
  answer("GET /api/v1/entity-types", [PREP, METHOD_TYPE, CATEGORY_TYPE]);
  answer("GET /api/v1/projects", page([KINASE]));
  answer("GET /api/v1/components", []);
  answer("GET /api/v1/registries", page([registry()]));
  onOptions();
}

const PREP_ID = "00000000-0000-7000-8000-00000000b001";

function prep(values: Record<string, unknown>, refs: Schemas["EntityOut"]["refs"] = {}) {
  return entity({
    id: PREP_ID,
    hrid: "PRP-000001",
    type: PREP_TYPE,
    title: "Anti-HER2 prep 12",
    values: { name: "Anti-HER2 prep 12", ...values },
    refs,
  });
}

function ref(o: Schemas["OptionOut"], superseded = false): Schemas["RefOut"] {
  return {
    hrid: o.hrid,
    title: o.label,
    type: METHOD,
    archived: o.archived,
    code: o.code,
    superseded,
    purged: false,
  };
}

function onPrep(record: Schemas["EntityOut"]): void {
  answer("GET /api/v1/entities/PRP-000001", record);
  answer(`GET /api/v1/entities/${PREP_ID}/provenance`, {});
  answer(`GET /api/v1/entities/${PREP_ID}/projects`, []);
}

describe("a record's dropdown of a lab list", () => {
  it("offers the list's active options, in its order, by label, and sends the option", async () => {
    onLab();
    answer(
      "POST /api/v1/entities",
      respond(201, { ...prep({ method: SEC.id }, { [SEC.id]: ref(SEC) }), reused: false }),
    );
    onPrep(prep({ method: SEC.id }, { [SEC.id]: ref(SEC) }));
    const { user } = await renderApp(`/projects/kinase-atlas/entities/new/?type=${PREP_TYPE}`);
    const method = screen.getByRole("combobox", { name: /^Method/ });
    await waitFor(() => {
      expect(
        within(method)
          .getAllByRole("option")
          .map((o) => o.textContent),
      ).toEqual(["Not set", "Protein A affinity", "Size exclusion"]);
    });
    // In the project's registry, the options a dropdown offers.
    const asked = calls("GET", OPTIONS)[0]?.query;
    expect(asked?.get("project")).toBe(KINASE.id);
    expect(asked?.get("archived")).toBe("false");
    await user.type(screen.getByRole("textbox", { name: "Name" }), "Anti-HER2 prep 12");
    await user.selectOptions(method, "Size exclusion");
    await user.click(screen.getByRole("button", { name: "Add Polishing" }));
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Polishing, item 1" }),
      "Protein A affinity",
    );
    await user.click(screen.getByRole("button", { name: "Create prep" }));
    await screen.findByRole("heading", { level: 1, name: /Anti-HER2 prep 12/ });
    expect(calls("POST", "/api/v1/entities")[0]?.body).toMatchObject({
      values: { method: SEC.id, polishing: [PROTEIN_A.id] },
    });
  });

  it("keeps an option archived since, marked, unless someone chooses another", async () => {
    onLab();
    const record = prep({ method: DIALYSIS.id }, { [DIALYSIS.id]: ref(DIALYSIS) });
    onPrep(record);
    answer(`PATCH /api/v1/entities/${PREP_ID}`, { ...record, revision: 3 });
    const { user } = await renderApp("/entities/PRP-000001/edit/");
    const method = await screen.findByRole("combobox", { name: /^Method/ });
    await waitFor(() => {
      expect(
        within(method)
          .getAllByRole("option")
          .map((o) => o.textContent),
      ).toEqual(["Not set", "Dialysis (archived)", "Protein A affinity", "Size exclusion"]);
    });
    expect(method).toHaveDisplayValue("Dialysis (archived)");
    const name = screen.getByRole("textbox", { name: "Name" });
    await user.clear(name);
    await user.type(name, "Anti-HER2 prep 12b");
    await user.click(screen.getByRole("button", { name: /^Save/ }));
    await waitFor(() => {
      expect(calls("PATCH", `/api/v1/entities/${PREP_ID}`)).toHaveLength(1);
    });
    // What it named is kept: only the name changed.
    const sent = calls("PATCH", `/api/v1/entities/${PREP_ID}`)[0]?.body as {
      values: Record<string, unknown>;
    };
    expect(sent.values).toEqual({ name: "Anti-HER2 prep 12b" });
  });

  it("searches a long list by label or code as people type", async () => {
    onLab();
    const many = Array.from({ length: 18 }, (_, i) =>
      option(i + 1, `step_${String(i + 1)}`, `Column ${String(i + 1)}`),
    );
    onOptions([
      ...many,
      option(19, "imac", "Ni-NTA affinity", { description: "His-tag capture." }),
    ]);
    const { user } = await renderApp(`/projects/kinase-atlas/entities/new/?type=${PREP_TYPE}`);
    // Once its options come, there are too many to scroll: it's searched.
    const method = await waitFor(() => {
      const box = screen.getByRole("combobox", { name: /^Method/ });
      expect(box).toHaveAttribute("aria-autocomplete", "list");
      return box;
    });
    await user.type(method, "imac");
    const found = await screen.findByRole("option", { name: /Ni-NTA affinity/ });
    expect(screen.getAllByRole("option")).toHaveLength(1);
    expect(found).toHaveTextContent("His-tag capture.");
    await user.click(found);
    expect(method).toHaveValue("Ni-NTA affinity");
  });

  it("shows a long list's first matches as people type, and how many there are", async () => {
    onLab();
    // A lab's vendor catalogue: 5,000 options, which nobody scrolls through.
    const many = Array.from({ length: 5000 }, (_, i) =>
      option(i + 1, `resin_${String(i + 1)}`, `Resin ${String(i + 1)}`),
    );
    onOptions(many);
    const { user } = await renderApp(`/projects/kinase-atlas/entities/new/?type=${PREP_TYPE}`);
    const method = await waitFor(() => {
      const box = screen.getByRole("combobox", { name: /^Method/ });
      expect(box).toHaveAttribute("aria-autocomplete", "list");
      return box;
    });
    await user.click(method);
    const options = await screen.findByRole("listbox");
    expect(within(options).getAllByRole("option")).toHaveLength(50);
    expect(screen.getByRole("status")).toHaveTextContent(
      "Showing 50 of 5,000. Type to narrow them.",
    );
    // "Resin 49", "Resin 490" to "Resin 499" and "Resin 4900" to "Resin 4999": 111.
    await user.type(method, "Resin 49");
    await waitFor(() => {
      expect(screen.getByRole("status")).toHaveTextContent("Showing 50 of 111.");
    });
    expect(within(options).getAllByRole("option")[0]).toHaveTextContent("Resin 49");
    await user.type(method, "87");
    expect(within(options).getAllByRole("option")).toHaveLength(1);
    expect(screen.getByRole("status")).toHaveTextContent("");
    await user.click(within(options).getByRole("option", { name: "Resin 4987" }));
    expect(method).toHaveValue("Resin 4987");
  });

  it("says when a list has no options", async () => {
    onLab();
    answer(`GET ${OPTIONS}`, []);
    await renderApp(`/projects/kinase-atlas/entities/new/?type=${PREP_TYPE}`);
    const method = await screen.findByRole("combobox", { name: /^Method/ });
    await waitFor(() => {
      expect(method).toHaveAccessibleDescription(
        "There are no purification methods to choose yet: the list’s curators add them on its page.",
      );
    });
    expect(method).toBeDisabled();
  });

  it("says when its options can't be read, and keeps the option the record names", async () => {
    onLab();
    answer(`GET ${OPTIONS}`, problem(500, "internal", "Something went wrong."));
    const record = prep({ method: SEC.id }, { [SEC.id]: ref(SEC) });
    onPrep(record);
    const saved = prep({ name: "Anti-HER2 prep 12b", method: SEC.id }, { [SEC.id]: ref(SEC) });
    answer(`PATCH /api/v1/entities/${PREP_ID}`, { ...saved, revision: 3 });
    const { user } = await renderApp("/entities/PRP-000001/edit/");
    const method = await screen.findByRole("combobox", { name: /^Method/ });
    // After its retries.
    await waitFor(
      () => {
        expect(method).toHaveAccessibleDescription(
          "Couldn’t load its options. Try again in a moment.",
        );
      },
      { timeout: 10_000 },
    );
    // Unread isn't archived: it's what the record says it is.
    expect(method).toHaveDisplayValue("Size exclusion");
    const name = screen.getByRole("textbox", { name: "Name" });
    await user.clear(name);
    await user.type(name, "Anti-HER2 prep 12b");
    await user.click(screen.getByRole("button", { name: /^Save/ }));
    await waitFor(() => {
      expect(calls("PATCH", `/api/v1/entities/${PREP_ID}`)).toHaveLength(1);
    });
    const sent = calls("PATCH", `/api/v1/entities/${PREP_ID}`)[0]?.body as {
      values: Record<string, unknown>;
    };
    expect(sent.values).toEqual({ name: "Anti-HER2 prep 12b" });
    // Saved, the record still names it.
    expect(await screen.findByRole("link", { name: "Size exclusion" })).toBeVisible();
  });
});

describe("an option, as records read it", () => {
  it("reads as its label, marked when it's retired, and leads to its list", async () => {
    onLab();
    onPrep(
      prep(
        { method: DIALYSIS.id, polishing: [SEC.id] },
        { [DIALYSIS.id]: ref(DIALYSIS), [SEC.id]: ref(SEC, true) },
      ),
    );
    await renderApp("/entities/PRP-000001/");
    const link = await screen.findByRole("link", { name: "Dialysis" });
    expect(link).toHaveAttribute("href", `/types/${METHOD}/?project=kinase-atlas#option-dialysis`);
    expect(link.parentElement).toHaveTextContent("Dialysis (archived)");
    expect(screen.getByRole("link", { name: "Size exclusion" }).parentElement).toHaveTextContent(
      "Size exclusion (superseded)",
    );
    // Its HRID, nobody's record, isn't shown.
    expect(screen.queryByText("OPTION-000003")).not.toBeInTheDocument();
  });

  it("reads as its label in the grid, and in a sheet's cell", () => {
    const method = nthField(PREP, "method");
    const polishing = nthField(PREP, "polishing");
    const record = prep(
      { method: DIALYSIS.id, polishing: [SEC.id, PROTEIN_A.id] },
      { [DIALYSIS.id]: ref(DIALYSIS), [SEC.id]: ref(SEC), [PROTEIN_A.id]: ref(PROTEIN_A) },
    );
    expect(valueText(method, DIALYSIS.id, record)).toBe("Dialysis (archived)");
    expect(cellText(method, DIALYSIS.id, record)).toBe("Dialysis");
    expect(cellText(polishing, [SEC.id, PROTEIN_A.id], record)).toBe(
      "Size exclusion; Protein A affinity",
    );
    expect(matching(EVERY, "AFFIN")).toEqual([PROTEIN_A]);
    expect(optionText("Protien G", "superseded")).toBe("Protien G (superseded)");
  });
});

function nthField(type: Schemas["EntityTypeOut"], name: string): Schemas["FieldOut"] {
  const found = type.fields.find((f) => f.name === name);
  if (!found) throw new Error(name);
  return found;
}

describe("filtering by a list's option", () => {
  it("offers every option, archived ones marked, and says it by its label", async () => {
    signedIn();
    onOptions();
    const user = userEvent.setup();
    const onApply = vi.fn();
    render(
      <QueryClientProvider client={queryClient}>
        <FilterBar
          type={PREP}
          types={[PREP, METHOD_TYPE]}
          fields={filterable(PREP, [])}
          applied={[]}
          projectId={KINASE.id}
          noun="preps"
          onApply={onApply}
        />
      </QueryClientProvider>,
    );
    await user.click(screen.getByRole("button", { name: "Filter" }));
    const menu = await screen.findByRole("menu", { name: "Filter" });
    await user.click(within(menu).getByRole("menuitem", { name: /^Method/ }));
    const editor = await screen.findByRole("dialog");
    const value = within(editor).getByRole("combobox", { name: "Filter 1: value" });
    await waitFor(() => {
      expect(
        within(value)
          .getAllByRole("option")
          .map((o) => o.textContent),
      ).toEqual(["Choose one", "Protein A affinity", "Size exclusion", "Dialysis (archived)"]);
    });
    await user.selectOptions(value, "Dialysis (archived)");
    await waitFor(() => {
      expect(onApply).toHaveBeenLastCalledWith([
        { field: "method", op: "eq", value: DIALYSIS.hrid },
      ]);
    });
    await user.click(within(editor).getByRole("button", { name: "Done" }));
    expect(
      within(screen.getByRole("group", { name: "Filters" })).getByRole("button", {
        name: "Filter 1: Method is Dialysis (archived)",
      }),
    ).toBeVisible();
  });
});

// --- A list's page ----------------------------------------------------------------------------

function onList(over: Partial<Schemas["ListOut"]> = {}, reg = registry()): void {
  onLab();
  answer("GET /api/v1/registries", page([reg]));
  answer(`GET /api/v1/lists/${METHOD}`, list(over));
  // Its own curators are a registry role, asked for by role and list (ADR 0292).
  const curator: Schemas["RegistryRoleOut"] = {
    id: "00000000-0000-7000-8000-0000000000c1",
    role: "list_curator",
    list: METHOD,
    user: {
      id: "00000000-0000-7000-8000-000000000009",
      name: "Ada Purifier",
      email: "ada@example.com",
      active: true,
      kind: "person",
      imported_from: "",
    },
    granted_by: null,
    granted_at: "2026-11-01T00:00:00Z",
  };
  answer(`GET /api/v1/registries/${MAIN_REGISTRY.id}/roles`, ({ query }: Sent) =>
    page(query.get("role") === "list_curator" && query.get("list") === METHOD ? [curator] : []),
  );
}

const PAGE = `/types/${METHOD}/`;

describe("a list's page", () => {
  it("shows its members the active options in order, read-only, and who curates it", async () => {
    onList();
    await renderApp(PAGE);
    const table = await screen.findByRole("table", { name: /Purification methods, in the order/ });
    const body = within(table).getAllByRole("row").slice(1);
    expect(body.map((r) => within(r).getByRole("rowheader").textContent)).toEqual([
      "Protein A affinityCaptured on a Protein A column and eluted at low pH.",
      "Size exclusion",
    ]);
    // Its other facts: a category from another list, by its label; an SOP, linked.
    expect(within(nthRow(body, 0)).getByText("Chromatography")).toBeVisible();
    expect(within(nthRow(body, 0)).getByRole("link", { name: /PUR-001/ })).toHaveAttribute(
      "href",
      "https://sop.example.com/PUR-001",
    );
    expect(screen.queryByRole("button", { name: "Add an option" })).not.toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: "Records" })).not.toBeInTheDocument();
    expect(
      await screen.findByText(/Ada Purifier, and the Main registry’s registrars/),
    ).toBeVisible();
    // The archived one is there on asking.
    await userEvent
      .setup()
      .click(screen.getByRole("checkbox", { name: "Show archived and superseded options (1)" }));
    expect(within(table).getAllByRole("row")).toHaveLength(4);
    expect(within(table).getByText("Archived")).toBeVisible();
    // In the registry's scope, every option.
    expect(calls("GET", OPTIONS).some((c) => c.query.get("registry") === MAIN_REGISTRY.id)).toBe(
      true,
    );
  });

  it("lets its curators add an option, with its facts, at the end of the list", async () => {
    onList({ can: { curate: true } });
    answer(`POST ${OPTIONS}`, respond(201, option(4, "imac", "Ni-NTA affinity")));
    const { user } = await renderApp(PAGE);
    await user.click(await screen.findByRole("button", { name: "Add an option" }));
    const dialog = await screen.findByRole("dialog", {
      name: "Add an option to purification methods",
    });
    await user.click(within(dialog).getByRole("button", { name: "Add option" }));
    expect(within(dialog).getByRole("textbox", { name: "Code" })).toHaveAccessibleDescription(
      /Enter its code\./,
    );
    await user.type(within(dialog).getByRole("textbox", { name: "Code" }), "imac");
    await user.type(within(dialog).getByRole("textbox", { name: "Label" }), "Ni-NTA affinity");
    await user.selectOptions(
      within(dialog).getByRole("combobox", { name: /^Category/ }),
      "Chromatography",
    );
    await user.click(within(dialog).getByRole("button", { name: "Add option" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(calls("POST", OPTIONS)[0]?.body).toEqual({
      code: "imac",
      label: "Ni-NTA affinity",
      description: "",
      values: { category: CHROMATOGRAPHY.id },
      reason: "",
    });
    expect(calls("POST", OPTIONS)[0]?.query.get("registry")).toBe(MAIN_REGISTRY.id);
    expect(await screen.findByText(/Added Ni-NTA affinity/)).toBeVisible();
  });

  it("shows the API's word on a field where it points", async () => {
    onList({ can: { curate: true } });
    answer(
      `POST ${OPTIONS}`,
      problem(422, "validation_failed", "Some values are invalid.", [
        { pointer: "#/values/label", detail: "is `sec`'s label already: the list has each once." },
      ]),
    );
    const { user } = await renderApp(PAGE);
    await user.click(await screen.findByRole("button", { name: "Add an option" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByRole("textbox", { name: "Code" }), "gel");
    await user.type(within(dialog).getByRole("textbox", { name: "Label" }), "size exclusion");
    await user.click(within(dialog).getByRole("button", { name: "Add option" }));
    const label = within(dialog).getByRole("textbox", { name: "Label" });
    await waitFor(() => {
      expect(label).toHaveAccessibleDescription(/is `sec`'s label already/);
    });
    expect(label).toHaveFocus();
  });

  it("relabels an option from the revision read, with a reason, and brings a colleague's change in", async () => {
    onList({ can: { curate: true } });
    const relabelled = { ...SEC, label: "Gel filtration", revision: 3 };
    answer(`PATCH ${OPTIONS}/sec`, (call: { headers: Headers }) => {
      if (call.headers.get("If-Match") !== '"1"') return relabelled;
      // A colleague described it meanwhile: the list, read again, has it at revision 2.
      onOptions([PROTEIN_A, { ...SEC, revision: 2, description: "Superdex 200" }, DIALYSIS]);
      return problem(412, "stale", "It changed since you read it: it’s at revision 2 now.");
    });
    const { user } = await renderApp(PAGE);
    await user.click(await screen.findByRole("button", { name: "Change Size exclusion" }));
    await user.click(await screen.findByRole("menuitem", { name: "Edit…" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit Size exclusion" });
    const label = within(dialog).getByRole("textbox", { name: "Label" });
    await user.clear(label);
    await user.type(label, "Gel filtration");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    expect(within(dialog).getByRole("textbox", { name: /^Reason/ })).toHaveAccessibleDescription(
      /Say why/,
    );
    await user.type(within(dialog).getByRole("textbox", { name: /^Reason/ }), "What we call it");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    const first = await waitFor(() => {
      const sent = calls("PATCH", `${OPTIONS}/sec`)[0];
      if (!sent) throw new Error("not sent");
      return sent;
    });
    expect(first.headers.get("If-Match")).toBe('"1"');
    expect(first.body).toEqual({ label: "Gel filtration", values: {}, reason: "What we call it" });
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent(
      "Someone changed Size exclusion while you were editing (it’s at revision 2 now)",
    );
    // Theirs comes in; the new label and the reason stay; saving sends revision 2.
    expect(alert).toHaveTextContent("Description now has its new value.");
    expect(within(dialog).getByRole("textbox", { name: /^Description/ })).toHaveValue(
      "Superdex 200",
    );
    expect(label).toHaveValue("Gel filtration");
    expect(within(dialog).getByRole("textbox", { name: /^Reason/ })).toHaveValue("What we call it");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => {
      expect(calls("PATCH", `${OPTIONS}/sec`)).toHaveLength(2);
    });
    const second = calls("PATCH", `${OPTIONS}/sec`)[1];
    expect(second?.headers.get("If-Match")).toBe('"2"');
    expect(second?.body).toEqual({
      label: "Gel filtration",
      values: {},
      reason: "What we call it",
    });
    expect(await screen.findByText(/Saved Gel filtration/)).toBeVisible();
  });

  it("reorders the list by its buttons, saying where each went, with a reason", async () => {
    onList({ can: { curate: true } });
    answer(`PUT /api/v1/lists/${METHOD}/order`, [SEC, PROTEIN_A, DIALYSIS]);
    const { user } = await renderApp(PAGE);
    await user.click(await screen.findByRole("button", { name: "Reorder" }));
    const order = screen.getByRole("list", { name: "Purification methods, in their new order" });
    expect(screen.getByRole("button", { name: "Save order" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    expect(screen.getByRole("button", { name: "Move Protein A affinity up" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Move Size exclusion up" }));
    expect(
      within(order)
        .getAllByRole("listitem")
        .map((i) => i.textContent),
    ).toEqual([
      expect.stringContaining("Size exclusion"),
      expect.stringContaining("Protein A affinity"),
      expect.stringContaining("Dialysis (archived)"),
    ]);
    expect(screen.getByText("Moved Size exclusion to position 1 of 3.")).toBeInTheDocument();
    // At the top, focus goes to its other button.
    expect(screen.getByRole("button", { name: "Move Size exclusion down" })).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "Save order" }));
    expect(screen.getByRole("textbox", { name: /^Reason/ })).toHaveAccessibleDescription(/Say why/);
    await user.type(screen.getByRole("textbox", { name: /^Reason/ }), "Most used first");
    await user.click(screen.getByRole("button", { name: "Save order" }));
    await waitFor(() => {
      expect(calls("PUT", `/api/v1/lists/${METHOD}/order`)[0]?.body).toEqual({
        codes: ["sec", "protein_a", "dialysis"],
        // Each option as it was read: one someone changed since refuses the order (412).
        expected_revisions: { protein_a: 1, sec: 1, dialysis: 1 },
        reason: "Most used first",
      });
    });
    expect(await screen.findByText(/Saved the order of purification methods/)).toBeVisible();
  });

  it("refuses an order made before someone changed an option, and starts again from theirs", async () => {
    onList({ can: { curate: true } });
    const order = `/api/v1/lists/${METHOD}/order`;
    answer(
      `PUT ${order}`,
      problem(412, "stale", "Someone changed the list since you read it.", undefined, {
        stale: [{ code: "sec", expected_revision: 1, current_revision: 2 }],
      }),
    );
    const { user } = await renderApp(PAGE);
    await user.click(await screen.findByRole("button", { name: "Reorder" }));
    await user.click(screen.getByRole("button", { name: "Move Size exclusion up" }));
    await user.type(screen.getByRole("textbox", { name: /^Reason/ }), "Most used first");
    await user.click(screen.getByRole("button", { name: "Save order" }));
    expect(await screen.findByText("Someone changed the list since you read it.")).toBeVisible();
    // The latest, with Size exclusion at revision 2, is read again: the next order is made
    // from it.
    onOptions([PROTEIN_A, { ...SEC, revision: 2 }, DIALYSIS]);
    await user.click(screen.getByRole("button", { name: "Start again from the latest order" }));
    await user.click(await screen.findByRole("button", { name: "Move Size exclusion up" }));
    answer(`PUT ${order}`, [SEC, PROTEIN_A, DIALYSIS]);
    await user.click(screen.getByRole("button", { name: "Save order" }));
    await waitFor(() => {
      expect(calls("PUT", order).map((c) => c.body)).toEqual([
        {
          codes: ["sec", "protein_a", "dialysis"],
          expected_revisions: { protein_a: 1, sec: 1, dialysis: 1 },
          reason: "Most used first",
        },
        {
          codes: ["sec", "protein_a", "dialysis"],
          expected_revisions: { protein_a: 1, sec: 2, dialysis: 1 },
          reason: "Most used first",
        },
      ]);
    });
  });

  it("archives and restores an option, and supersedes a wrong one, each with a reason", async () => {
    onList({ can: { curate: true } });
    let now = [
      { ...PROTEIN_A, uses: 12 },
      { ...SEC, uses: 0 },
      { ...DIALYSIS, uses: 3 },
    ];
    onOptions(now);
    answer(`POST ${OPTIONS}/protein_a/archive`, () => {
      now = now.map((o) => (o.code === "protein_a" ? { ...o, archived: true } : o));
      onOptions(now);
      return { ...PROTEIN_A, archived: true };
    });
    answer(`POST ${OPTIONS}/dialysis/restore`, () => {
      now = now.map((o) => (o.code === "dialysis" ? { ...o, archived: false } : o));
      onOptions(now);
      return { ...DIALYSIS, archived: false };
    });
    answer(`POST ${OPTIONS}/sec/supersede`, {
      ...SEC,
      superseded_by: { id: DIALYSIS.id, hrid: DIALYSIS.hrid, code: "dialysis", label: "Dialysis" },
    });
    const { user } = await renderApp(PAGE);
    // Its curators see how many records name each, never which.
    expect(await screen.findByRole("columnheader", { name: "Records" })).toBeVisible();

    await user.click(await screen.findByRole("button", { name: "Change Protein A affinity" }));
    await user.click(await screen.findByRole("menuitem", { name: "Archive…" }));
    let dialog = await screen.findByRole("dialog", { name: "Archive Protein A affinity?" });
    expect(dialog).toHaveTextContent("The 12 records that name it keep it");
    await user.click(within(dialog).getByRole("button", { name: "Archive" }));
    expect(calls("POST", `${OPTIONS}/protein_a/archive`)).toEqual([]);
    await user.type(within(dialog).getByRole("textbox", { name: /^Reason/ }), "Column retired");
    await user.click(within(dialog).getByRole("button", { name: "Archive" }));
    await waitFor(() => {
      expect(calls("POST", `${OPTIONS}/protein_a/archive`).map(made)).toEqual([
        { "If-Match": '"1"', reason: "Column retired" },
      ]);
    });
    // Its row is gone, and focus goes to the row in its place, not to the page.
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(screen.queryByRole("button", { name: "Change Protein A affinity" })).toBeNull();
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Change Size exclusion" })).toHaveFocus();
    });

    await user.click(screen.getByRole("checkbox", { name: /Show archived and superseded/ }));
    await user.click(screen.getByRole("button", { name: "Change Dialysis" }));
    await user.click(await screen.findByRole("menuitem", { name: "Restore…" }));
    dialog = await screen.findByRole("dialog", { name: "Restore Dialysis?" });
    await user.type(within(dialog).getByRole("textbox", { name: /^Reason/ }), "Back in use");
    await user.click(within(dialog).getByRole("button", { name: "Restore" }));
    await waitFor(() => {
      expect(calls("POST", `${OPTIONS}/dialysis/restore`).map(made)).toEqual([
        { "If-Match": '"1"', reason: "Back in use" },
      ]);
    });
    // Its row stays, archived ones shown: focus goes back to it.
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Change Dialysis" })).toHaveFocus();
    });

    await user.click(screen.getByRole("button", { name: "Change Size exclusion" }));
    await user.click(await screen.findByRole("menuitem", { name: "Supersede with another…" }));
    dialog = await screen.findByRole("dialog", { name: "Supersede Size exclusion?" });
    // Only another active option replaces it.
    expect(
      within(within(dialog).getByRole("combobox", { name: "Replaced by" }))
        .getAllByRole("option")
        .map((o) => o.textContent),
    ).toEqual(["Choose one", "Dialysis (dialysis)"]);
    await user.click(within(dialog).getByRole("button", { name: "Supersede" }));
    expect(
      within(dialog).getByRole("combobox", { name: "Replaced by" }),
    ).toHaveAccessibleDescription("Choose the option that replaces it.");
    await user.selectOptions(
      within(dialog).getByRole("combobox", { name: "Replaced by" }),
      "dialysis",
    );
    await user.type(within(dialog).getByRole("textbox", { name: /^Reason/ }), "Added twice");
    await user.click(within(dialog).getByRole("button", { name: "Supersede" }));
    await waitFor(() => {
      expect(calls("POST", `${OPTIONS}/sec/supersede`).map(made)).toEqual([
        { "If-Match": '"1"', replacement: "dialysis", reason: "Added twice" },
      ]);
    });
    expect(
      await screen.findByText(/Each project whose records name it has been told/),
    ).toBeVisible();
  });

  it("shows a long list a page at a time, finds an option, and opens at a record's", async () => {
    onList();
    const many = Array.from({ length: 250 }, (_, i) =>
      option(i + 1, `resin_${String(i + 1)}`, `Resin ${String(i + 1)}`),
    );
    onOptions(many);
    const { user } = await renderApp(PAGE);
    const table = await screen.findByRole("table", { name: /Purification methods, in the order/ });
    const rows = () => within(table).getAllByRole("rowheader");
    expect(rows()).toHaveLength(100);
    const pages = screen.getByRole("navigation", { name: "Pages of options" });
    expect(within(pages).getByRole("status")).toHaveTextContent("1–100 of 250");
    await user.click(within(pages).getByRole("button", { name: "Next 100" }));
    expect(within(pages).getByRole("status")).toHaveTextContent("101–200 of 250");
    expect(rows()[0]).toHaveTextContent("Resin 101");
    await user.click(within(pages).getByRole("button", { name: "Next 50" }));
    expect(rows()).toHaveLength(50);
    expect(within(pages).getByRole("button", { name: "Next 0" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    // Found by its label or code, from the first page of what matches.
    await user.type(
      screen.getByRole("searchbox", { name: "Find an option, by its label or code" }),
      "resin_24",
    );
    expect(rows().map((r) => r.textContent)).toEqual([
      "Resin 24",
      ...Array.from({ length: 10 }, (_, i) => `Resin ${String(240 + i)}`),
    ]);
    expect(screen.queryByRole("navigation", { name: "Pages of options" })).toBeNull();
    await user.type(screen.getByRole("searchbox"), "x");
    expect(screen.getByText("No option’s label or code has “resin_24x” in it.")).toBeVisible();
  });

  it("opens a long list at the page of the option a record's link names", async () => {
    const scrolled = vi.fn();
    Element.prototype.scrollIntoView = scrolled;
    onList();
    onOptions(
      Array.from({ length: 250 }, (_, i) =>
        option(i + 1, `resin_${String(i + 1)}`, `Resin ${String(i + 1)}`),
      ),
    );
    await renderApp(`${PAGE}#option-resin_230`);
    const target = await screen.findByRole("row", { current: true });
    expect(target).toHaveTextContent("Resin 230");
    expect(scrolled.mock.contexts).toContain(target);
    expect(
      within(screen.getByRole("navigation", { name: "Pages of options" })).getByRole("status"),
    ).toHaveTextContent("201–250 of 250");
  });

  it("moves an option to a position in a long list's order, by keyboard", async () => {
    onList({ can: { curate: true } });
    const many = Array.from({ length: 250 }, (_, i) =>
      option(i + 1, `resin_${String(i + 1)}`, `Resin ${String(i + 1)}`),
    );
    onOptions(many);
    answer(`PUT /api/v1/lists/${METHOD}/order`, many);
    const { user } = await renderApp(PAGE);
    await user.click(await screen.findByRole("button", { name: "Reorder" }));
    const order = screen.getByRole("list", { name: "Purification methods, in their new order" });
    expect(within(order).getAllByRole("listitem")).toHaveLength(100);
    const pages = screen.getByRole("navigation", { name: "Pages of the order" });
    await user.click(within(pages).getByRole("button", { name: "Next 100" }));
    await user.click(within(pages).getByRole("button", { name: "Next 50" }));
    await user.click(screen.getByRole("button", { name: "Move Resin 240 to a position" }));
    const dialog = await screen.findByRole("dialog", { name: "Move Resin 240" });
    const position = within(dialog).getByRole("textbox", { name: "New position" });
    expect(position).toHaveValue("240");
    await user.clear(position);
    await user.type(position, "251");
    await user.click(within(dialog).getByRole("button", { name: "Move" }));
    expect(position).toHaveAccessibleDescription(/Enter a whole number from 1, the top, to 250/);
    await user.clear(position);
    await user.type(position, "1{Enter}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    // Its page follows it, and focus stays with it.
    expect(within(order).getAllByRole("listitem")[0]).toHaveTextContent("Resin 240");
    expect(screen.getByText("Moved Resin 240 to position 1 of 250.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Move Resin 240 to a position" })).toHaveFocus();
    // Its own form: the order isn't saved until it's asked to be.
    expect(calls("PUT", `/api/v1/lists/${METHOD}/order`)).toEqual([]);
    await user.type(screen.getByRole("textbox", { name: /^Reason/ }), "Most used first");
    await user.click(screen.getByRole("button", { name: "Save order" }));
    await waitFor(() => {
      expect(
        (calls("PUT", `/api/v1/lists/${METHOD}/order`)[0]?.body as { codes: string[] }).codes,
      ).toEqual(["resin_240", ...many.map((o) => o.code).filter((c) => c !== "resin_240")]);
    });
  });

  it("is read-only while its plugin is detached", async () => {
    onList({ detached: true, can: { curate: false } });
    await renderApp(PAGE);
    expect(
      await screen.findByText(/is detached: its options can be read, but not changed/),
    ).toBeVisible();
    expect(screen.queryByRole("button", { name: "Add an option" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Change / })).not.toBeInTheDocument();
  });

  it("reads the registry of the project a record's link names, and says what it can't read", async () => {
    onList();
    const partner = {
      ...MAIN_REGISTRY,
      id: "00000000-0000-7000-8000-0000000000r2",
      name: "Partner",
    };
    answer(`GET /api/v1/lists/${METHOD}`, ({ query }: Sent) =>
      query.get("project") === "kinase-atlas" || query.get("registry") === partner.id
        ? list({ registry: partner })
        : problem(404, "not_found", "No such project, or you can’t read it."),
    );
    answer(`GET /api/v1/registries/${partner.id}/roles`, page([]));
    await renderApp(`${PAGE}?project=kinase-atlas`);
    await screen.findByRole("table", { name: /Purification methods/ });
    expect(
      screen.getByText(/What records in the Partner registry’s projects pick from/),
    ).toBeVisible();
    expect(calls("GET", OPTIONS).at(-1)?.query.get("registry")).toBe(partner.id);
  });
});

function nthRow(rowsOf: HTMLElement[], index: number): HTMLElement {
  const found = rowsOf[index];
  if (!found) throw new Error(String(index));
  return found;
}

describe("a superseded option's page", () => {
  it("lets a project's Editor move its records to the replacement", async () => {
    onLab();
    const other = project({
      id: "00000000-0000-7000-8000-0000000000p2",
      name: "Antibody Lab",
      slug: "antibody-lab",
    });
    answer("GET /api/v1/projects", page([KINASE, other]));
    const wrong = option(5, "protien_g", "Protein G (typo)", {
      superseded_by: { id: id(6), hrid: "OPTION-000006", code: "protein_g", label: "Protein G" },
    });
    onOptions([...EVERY, wrong, option(6, "protein_g", "Protein G")]);
    answer("GET /api/v1/entries/OPTION-000005", {
      id: wrong.id,
      hrid: wrong.hrid,
      type: METHOD,
      registry: MAIN_REGISTRY,
      revision: 3,
      created_at: "2026-11-20T00:00:00Z",
      values: { code: "protien_g", label: "Protein G (typo)", description: null, position: 5 },
      refs: {},
      files: {},
      holders: null,
      records: [],
      add_to: [],
      can: { curate: false },
      superseded: {
        at: "2026-11-25T10:00:00Z",
        reason: "Typo in the code",
        by_id: id(6),
        by: {
          hrid: "OPTION-000006",
          title: "Protein G",
          type: METHOD,
          archived: false,
          code: "protein_g",
          superseded: false,
          purged: false,
        },
      },
    });
    answer("GET /api/v1/entries/OPTION-000005/revisions", page([]));
    answer(`POST ${OPTIONS}/protien_g/move`, { moved: ["PRP-000001", "PRP-000004"] });
    const { user } = await renderApp("/entries/OPTION-000005/?project=antibody-lab");
    expect(screen.getByRole("heading", { level: 1, name: /Protein G \(typo\)/ })).toBeVisible();
    // Named by its code, not by the HRID its page's address has.
    expect(screen.getByText("protien_g", { selector: "code" })).toBeVisible();
    expect(screen.queryByText(/OPTION-000005/)).not.toBeInTheDocument();
    expect(screen.getByRole("note")).toHaveTextContent(
      /Superseded by Protein G .*“Typo in the code”\. Dropdowns offer Protein G instead\./,
    );
    // The project its notification named is chosen.
    expect(await screen.findByRole("combobox", { name: "Project" })).toHaveDisplayValue(
      "Antibody Lab",
    );
    await user.click(screen.getByRole("button", { name: "Move Antibody Lab’s records" }));
    expect(calls("POST", `${OPTIONS}/protien_g/move`)).toEqual([]);
    await user.type(screen.getByRole("textbox", { name: /^Reason/ }), "Typo in the code");
    await user.click(screen.getByRole("button", { name: "Move Antibody Lab’s records" }));
    await waitFor(() => {
      expect(calls("POST", `${OPTIONS}/protien_g/move`)[0]?.body).toEqual({
        project: other.id,
        reason: "Typo in the code",
      });
    });
    expect(
      await screen.findByText(
        /Moved 2 of Antibody Lab’s records to Protein G: PRP-000001, PRP-000004\./,
      ),
    ).toBeVisible();
  });
});

describe("moving an option in the order", () => {
  it("moves a code from one place to another", () => {
    expect(movedTo(["a", "b", "c"], 2, 0)).toEqual(["c", "a", "b"]);
    expect(movedTo(["a", "b", "c"], 0, 2)).toEqual(["b", "c", "a"]);
    expect(movedTo(["a", "b", "c"], 1, 1)).toEqual(["a", "b", "c"]);
  });
});
