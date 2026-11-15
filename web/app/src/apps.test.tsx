/** Apps (core.App, ADR 0090): panels that read the ones before them, rendered with the
 * real iki-wasm, run here without a worker. */

import { screen, waitFor, within } from "@testing-library/react";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it, vi } from "vitest";

import type { Offers, Rendered } from "./apps";
import { entity, field, KINASE, PROTEIN, renderApp, withTypes } from "./entity-fixtures";
import { answer, calls, page, problem, project, respond } from "./test-server";

vi.mock("./apps", async (original) => {
  const real = await original<typeof import("./apps")>();
  // Built by scripts/build-wasm.sh, as the TypeScript job does before the tests.
  const here = join(process.cwd(), "wasm", "iki_wasm.wasm");
  const bytes = readFileSync(existsSync(here) ? here : join("web", "app", "wasm", "iki_wasm.wasm"));
  const { instance } = await WebAssembly.instantiate(bytes, {});
  const wasm = instance.exports as unknown as {
    memory: WebAssembly.Memory;
    iki_input: (len: number) => number;
    iki_render: () => void;
    iki_output: () => number;
    iki_output_len: () => number;
  };
  return {
    ...real,
    renderTemplates: (templates: string[], variables: Offers, rows?: unknown[]) => {
      const request = new TextEncoder().encode(
        JSON.stringify({ templates, variables, ...(rows ? { rows } : {}) }),
      );
      const at = wasm.iki_input(request.length);
      new Uint8Array(wasm.memory.buffer, at, request.length).set(request);
      wasm.iki_render();
      const out = new Uint8Array(wasm.memory.buffer, wasm.iki_output(), wasm.iki_output_len());
      const found = JSON.parse(new TextDecoder().decode(out)) as { values: Rendered[][] };
      return Promise.resolve(found.values);
    },
  };
});

const PLASMID = {
  ...PROTEIN,
  name: "dna.DNASequence",
  plugin: "dna",
  label: "DNA sequence",
  label_plural: "DNA sequences",
  hrid_prefix: "DNA",
  fields: [
    field({ name: "name", label: "Name", type: "string" }),
    field({ name: "bases", label: "Bases", type: "string" }),
  ],
  viewers: [
    {
      viewer: "sequence" as const,
      label: "Sequence",
      sequence: "bases",
      alphabet: "dna" as const,
      features: null,
      tracks: [],
    },
  ],
};

const APP = {
  ...PROTEIN,
  name: "core.App",
  plugin: "core",
  label: "App",
  label_plural: "Apps",
  hrid_prefix: "APP",
  fields: [
    field({ name: "name", label: "Name", type: "string", plugin: "core" }),
    field({ name: "description", label: "Description", type: "text", plugin: "core" }),
    field({ name: "spec", label: "Spec", type: "json", plugin: "core" }),
  ],
  viewers: [],
};

function plasmid(n: number, name: string, bases: string) {
  return entity({
    id: `0190f5c2-0000-7000-8000-00000000d0a${String(n)}`,
    hrid: `DNA-00000${String(n)}`,
    type: "dna.DNASequence",
    values: { name, bases },
  });
}

const PTEST = plasmid(1, "pTest", "ACGT".repeat(40));
const POTHER = plasmid(2, "pOther", "ACGT".repeat(10));

function app(panels: unknown[]) {
  return entity({
    id: "0190f5c2-0000-7000-8000-0000000a0001",
    hrid: "APP-000001",
    type: "core.App",
    values: { name: "Plasmid QC", spec: { version: 1, panels } },
  });
}

const GRID = {
  id: "plasmids",
  title: "Plasmids",
  width: 6,
  component: "iki.entities",
  props: {
    type: "dna.DNASequence",
    where: { all: [{ field: "name", op: "starts_with", value: "p" }] },
    sort: "name",
    columns: [{ field: "name" }, { label: "Length", value: "{{ size(row.values.bases) }} bp" }],
  },
};
const VIEWER = {
  id: "shown",
  title: "Picked",
  width: 6,
  component: "iki.viewer",
  props: { entity: "{{ plasmids.selection }}" },
};
const TEXT = {
  id: "summary",
  component: "iki.text",
  props: { text: "{{ size(plasmids.selection) }} of {{ size(plasmids.rows) }} picked." },
};

function onTypes() {
  withTypes();
  answer("GET /api/v1/entity-types", [PROTEIN, PLASMID, APP]);
}

describe("an app", () => {
  it("shows panels that read what the ones before them offer", async () => {
    onTypes();
    answer("GET /api/v1/entities/APP-000001", app([GRID, VIEWER, TEXT]));
    answer("POST /api/v1/entities/search", { items: [PTEST, POTHER], next_cursor: null });
    answer("GET /api/v1/entities/DNA-000001", PTEST);
    const { user } = await renderApp("/entities/APP-000001/");

    const grid = await screen.findByRole("region", { name: "Plasmids" });
    const rows = await within(grid).findAllByRole("row");
    expect(rows.map((r) => r.textContent)).toEqual([
      "PickIDNameLength",
      "DNA-000001pTest160 bp",
      "DNA-000002pOther40 bp",
    ]);
    expect(await screen.findByText("0 of 2 picked.")).toBeInTheDocument();
    const picked = screen.getByRole("region", { name: "Picked" });
    expect(within(picked).getByText("Nothing to show yet.")).toBeInTheDocument();

    // The grid searches as the app says, in the app's project, as the person viewing it.
    const search = calls("POST", "/api/v1/entities/search")[0]?.body as Record<string, unknown>;
    expect(search).toMatchObject({
      type: "dna.DNASequence",
      project: app([]).owner_project?.id,
      where: GRID.props.where,
      sort: { field: "name", descending: false },
    });

    await user.click(within(grid).getByRole("radio", { name: "Pick pTest" }));
    expect(await screen.findByText("1 of 2 picked.")).toBeInTheDocument();
    expect(await within(picked).findByRole("link", { name: "pTest" })).toHaveAttribute(
      "href",
      "/entities/DNA-000001/",
    );
    expect(within(picked).getByText(/Sequence of/)).toBeInTheDocument();
    // Its spec is there to read, folded away.
    expect(screen.getByText("Its spec, as saved")).toBeInTheDocument();
  });

  it("says why an expression has no value, as text", async () => {
    onTypes();
    const broken = {
      ...TEXT,
      props: { text: "Ratio: {{ size(plasmids.rows) / size(plasmids.selection) }}" },
    };
    const html = { id: "html", component: "iki.text", props: { text: "<b>{{ 'bold' }}</b>" } };
    answer("GET /api/v1/entities/APP-000001", app([GRID, broken, html]));
    answer("POST /api/v1/entities/search", { items: [PTEST], next_cursor: null });
    await renderApp("/entities/APP-000001/");

    expect(
      await screen.findByText(
        "In `{{ size(plasmids.rows) / size(plasmids.selection) }}`: It divides by zero.",
      ),
    ).toBeInTheDocument();
    // What it makes is text, never HTML.
    expect(await screen.findByText("<b>bold</b>")).toBeInTheDocument();
    expect(document.querySelector(".app-grid b")).toBeNull();
  });

  it("says when it's an app this version can't show", async () => {
    onTypes();
    answer(
      "GET /api/v1/entities/APP-000001",
      app([{ id: "x", component: "iki.script", props: {} }]),
    );
    await renderApp("/entities/APP-000001/");
    expect(await screen.findByText("This version of Iki can’t show this app.")).toBeInTheDocument();
    await waitFor(() => {
      expect(calls("POST", "/api/v1/entities/search")).toHaveLength(0);
    });
  });
});

function about(panels: unknown[]) {
  const made = app(panels);
  return {
    ...made,
    values: { ...made.values, spec: { version: 1, entity: { type: "dna.DNASequence" }, panels } },
  };
}

const OTHERS = {
  ...GRID,
  id: "others",
  title: "Others",
  props: {
    ...GRID.props,
    where: { all: [{ field: "name", op: "ne", value: "{{ entity.values.name }}" }] },
  },
};
const ITS_LENGTH = {
  id: "length",
  component: "iki.text",
  props: { text: "{{ entity.values.name }} is {{ size(entity.values.bases) }} bp." },
};

describe("an app about an entity", () => {
  it("is a tab on its page, reading it, its grids' conditions too", async () => {
    onTypes();
    answer("GET /api/v1/entities/DNA-000001", PTEST);
    answer("GET /api/v1/entities/DNA-000001/apps", [
      { id: about([]).id, hrid: "APP-000001", name: "Plasmid QC" },
    ]);
    answer("GET /api/v1/entities/APP-000001", about([ITS_LENGTH, OTHERS]));
    answer("POST /api/v1/entities/search", { items: [POTHER], next_cursor: null });
    const { user } = await renderApp("/entities/DNA-000001/");

    const tabs = screen.getByRole("navigation", { name: "Entity" });
    await user.click(await within(tabs).findByRole("link", { name: "Plasmid QC" }));
    expect(await screen.findByText("pTest is 160 bp.")).toBeInTheDocument();
    const others = await screen.findByRole("region", { name: "Others" });
    expect(await within(others).findByText("pOther")).toBeInTheDocument();
    const search = calls("POST", "/api/v1/entities/search")[0]?.body as Record<string, unknown>;
    expect(search["where"]).toEqual({ all: [{ field: "name", op: "ne", value: "pTest" }] });
    // The page's tabs are drawn again on the app's tab, with it the current one.
    const shown = screen.getByRole("navigation", { name: "Entity" });
    expect(within(shown).getByRole("link", { name: "Plasmid QC" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  it("opens only on the entities the server lists it for", async () => {
    onTypes();
    answer("GET /api/v1/entities/DNA-000001", PTEST);
    answer("GET /api/v1/entities/DNA-000001/apps", []);
    answer("GET /api/v1/entities/APP-000001", about([ITS_LENGTH]));
    await renderApp("/entities/DNA-000001/apps/APP-000001/");
    expect(
      await screen.findByText("There’s nothing here, or you don’t have access to it."),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(calls("GET", "/api/v1/entities/APP-000001")).toHaveLength(0);
    });
    expect(screen.queryByText(/is 160 bp/)).toBeNull();
  });

  it("on its own page, says where it's shown", async () => {
    onTypes();
    answer("GET /api/v1/entities/APP-000001", about([ITS_LENGTH]));
    await renderApp("/entities/APP-000001/");
    expect(
      await screen.findByText(/This app is about one DNA sequence\. It’s a tab on each one’s page/),
    ).toBeInTheDocument();
  });

  it("shows nothing in a grid until its conditions can be worked out", async () => {
    onTypes();
    const following = {
      ...GRID,
      id: "same",
      title: "Same name",
      props: {
        ...GRID.props,
        where: { field: "name", op: "eq", value: "{{ plasmids.selection[0].values.name }}" },
      },
    };
    answer("GET /api/v1/entities/APP-000001", app([GRID, following]));
    answer("POST /api/v1/entities/search", { items: [PTEST, POTHER], next_cursor: null });
    const { user } = await renderApp("/entities/APP-000001/");
    const same = await screen.findByRole("region", { name: "Same name" });
    expect(await within(same).findByText(/Nothing to show yet/)).toBeInTheDocument();
    expect(calls("POST", "/api/v1/entities/search")).toHaveLength(1);

    const grid = screen.getByRole("region", { name: "Plasmids" });
    await user.click(await within(grid).findByRole("radio", { name: "Pick pOther" }));
    await waitFor(() => {
      expect(calls("POST", "/api/v1/entities/search")).toHaveLength(2);
    });
    const search = calls("POST", "/api/v1/entities/search")[1]?.body as Record<string, unknown>;
    expect(search["where"]).toEqual({ field: "name", op: "eq", value: "pOther" });
  });
});

describe("an app's grid, when it can't search", () => {
  it("shows nothing while a condition's value is empty", async () => {
    onTypes();
    const unnamed = { ...PTEST, values: { ...PTEST.values, name: null } };
    answer("GET /api/v1/entities/DNA-000001", unnamed);
    answer("GET /api/v1/entities/DNA-000001/apps", [
      { id: about([]).id, hrid: "APP-000001", name: "Plasmid QC" },
    ]);
    answer("GET /api/v1/entities/APP-000001", about([OTHERS]));
    await renderApp("/entities/DNA-000001/apps/APP-000001/");
    const others = await screen.findByRole("region", { name: "Others" });
    expect(
      await within(others).findByText("Nothing to show yet: a condition’s value is empty."),
    ).toBeInTheDocument();
    expect(calls("POST", "/api/v1/entities/search")).toHaveLength(0);
  });

  it("says where in its props each problem is", async () => {
    onTypes();
    answer("GET /api/v1/entities/APP-000001", app([GRID]));
    answer(
      "POST /api/v1/entities/search",
      respond(422, {
        type: "about:blank",
        title: "Unprocessable",
        status: 422,
        detail: "Some fields are invalid.",
        code: "validation_failed",
        errors: [{ pointer: "#/where/all/0/value", detail: "must be some text." }],
      }),
    );
    await renderApp("/entities/APP-000001/");
    const grid = await screen.findByRole("region", { name: "Plasmids" });
    const alert = await within(grid).findByRole("alert");
    expect(alert).toHaveTextContent(
      "Some fields are invalid.where.all.0.value: must be some text.",
    );
  });
});

describe("an entity's apps", () => {
  it("are told apart by their IDs when they share a name", async () => {
    onTypes();
    answer("GET /api/v1/entities/DNA-000001", PTEST);
    answer("GET /api/v1/entities/DNA-000001/apps", [
      { id: "a", hrid: "APP-000001", name: "QC" },
      { id: "b", hrid: "APP-000002", name: "QC" },
      { id: "c", hrid: "APP-000003", name: "Maps" },
    ]);
    await renderApp("/entities/DNA-000001/");
    const tabs = screen.getByRole("navigation", { name: "Entity" });
    expect(await within(tabs).findByRole("link", { name: "QC (APP-000001)" })).toBeInTheDocument();
    expect(within(tabs).getByRole("link", { name: "QC (APP-000002)" })).toBeInTheDocument();
    expect(within(tabs).getByRole("link", { name: "Maps" })).toBeInTheDocument();
  });
});

describe("a project's dashboard", () => {
  const DASHBOARD = `/api/v1/projects/${KINASE.id}/dashboard`;
  const NOTE = { id: "note", component: "iki.text", props: { text: "Welcome to the atlas." } };
  const CHOSEN = { id: "a", hrid: "APP-000001", name: "Plasmid QC" };

  function onProject(overrides: Parameters<typeof project>[0] = {}) {
    onTypes();
    answer("GET /api/v1/projects", page([project(overrides)]));
  }

  it("opens the project's page, for everyone who can read it", async () => {
    onProject({ my_role: "viewer", can: { ...KINASE.can, edit: false, manage: false } });
    answer(`GET ${DASHBOARD}`, { app: CHOSEN, set_by: null, set_at: null });
    answer("GET /api/v1/entities/APP-000001", app([NOTE]));
    await renderApp("/projects/kinase-atlas/");
    const shown = await screen.findByRole("region", { name: "Plasmid QC" });
    expect(await within(shown).findByText("Welcome to the atlas.")).toBeInTheDocument();
    expect(
      within(shown).getByRole("link", { name: "How it’s built (APP-000001)" }),
    ).toHaveAttribute("href", "/entities/APP-000001/");
  });

  it("is offered to Admins when there's none, and to no one else", async () => {
    onProject();
    answer(`GET ${DASHBOARD}`, { app: null, set_by: null, set_at: null });
    await renderApp("/projects/kinase-atlas/");
    expect(await screen.findByText(/Open this page with one of the project’s apps/)).toBeVisible();
  });

  it("is chosen in Settings from the project's apps, and can be removed", async () => {
    onProject();
    answer(`GET ${DASHBOARD}`, { app: null, set_by: null, set_at: null });
    answer(`GET ${DASHBOARD}/choices`, [CHOSEN, { id: "b", hrid: "APP-000002", name: "Maps" }]);
    answer(`PUT ${DASHBOARD}`, { app: CHOSEN, set_by: null, set_at: null });
    const { user } = await renderApp("/projects/kinase-atlas/settings/");
    const card = (await screen.findByRole("heading", { name: "Dashboard" })).closest("section");
    if (!card) throw new Error("no Dashboard card");
    const select = await within(card).findByRole("combobox", {
      name: "Open the project’s page with",
    });
    expect(select).toHaveAccessibleDescription(/Apps about one entity are tabs/);
    const save = within(card).getByRole("button", { name: "Save" });
    expect(save).toHaveAttribute("aria-disabled", "true");
    await user.selectOptions(select, "APP-000001");
    answer(`GET ${DASHBOARD}`, { app: CHOSEN, set_by: null, set_at: null });
    await user.click(save);
    expect(await screen.findByText("Kinase Atlas’s page opens with Plasmid QC now.")).toBeVisible();
    expect(calls("PUT", DASHBOARD)[0]?.body).toEqual({ app: "APP-000001" });

    answer(`DELETE ${DASHBOARD}`, respond(204));
    await user.selectOptions(select, "");
    answer(`GET ${DASHBOARD}`, { app: null, set_by: null, set_at: null });
    await user.click(save);
    expect(await screen.findByText("Kinase Atlas’s page opens without an app now.")).toBeVisible();
    expect(calls("DELETE", DASHBOARD)).toHaveLength(1);
  });

  it("says why it can't be chosen, and when its app is gone", async () => {
    onProject();
    answer(`GET ${DASHBOARD}`, { app: null, set_by: { id: "x", name: "Ada" }, set_at: null });
    answer(`GET ${DASHBOARD}/choices`, [CHOSEN]);
    const refusal = "APP-000001 is archived. Restore it first.";
    answer(`PUT ${DASHBOARD}`, problem(409, "refused", refusal));
    const { user } = await renderApp("/projects/kinase-atlas/settings/");
    expect(await screen.findByText(/was archived or taken out of the project/)).toBeVisible();
    const card = screen.getByRole("heading", { name: "Dashboard" }).closest("section");
    if (!card) throw new Error("no Dashboard card");
    await user.selectOptions(
      within(card).getByRole("combobox", { name: "Open the project’s page with" }),
      "APP-000001",
    );
    await user.click(within(card).getByRole("button", { name: "Save" }));
    expect(await within(card).findByRole("alert")).toHaveTextContent(refusal);
  });

  it("points to making an app when the project has none", async () => {
    onProject();
    answer(`GET ${DASHBOARD}`, { app: null, set_by: null, set_at: null });
    answer(`GET ${DASHBOARD}/choices`, []);
    await renderApp("/projects/kinase-atlas/settings/");
    expect(await screen.findByRole("link", { name: "make one" })).toHaveAttribute(
      "href",
      "/projects/kinase-atlas/entities/new/?type=core.App",
    );
  });
});
