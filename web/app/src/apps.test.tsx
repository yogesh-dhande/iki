/** Apps (iki/core.App, ADR 0090): panels that read the ones before them, rendered with the
 * real iki-wasm, run here without a worker. */

import { cleanup, screen, waitFor, within } from "@testing-library/react";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it, vi } from "vitest";

import type { Offers, Rendered } from "./apps";
import type { PluginComponent } from "./components";
import { entity, field, KINASE, PROTEIN, renderApp, withTypes } from "./entity-fixtures";
import { connectFrame, lastEntities } from "./frame-fixtures";
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
  name: "iki/dna.DNASequence",
  plugin: "iki/dna",
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
  name: "iki/core.App",
  plugin: "iki/core",
  label: "App",
  label_plural: "Apps",
  hrid_prefix: "APP",
  fields: [
    field({ name: "name", label: "Name", type: "string", plugin: "iki/core" }),
    field({ name: "description", label: "Description", type: "text", plugin: "iki/core" }),
    field({ name: "spec", label: "Spec", type: "json", plugin: "iki/core" }),
  ],
  viewers: [],
};

function plasmid(n: number, name: string, bases: string) {
  return entity({
    id: `0190f5c2-0000-7000-8000-00000000d0a${String(n)}`,
    hrid: `DNA-00000${String(n)}`,
    type: "iki/dna.DNASequence",
    values: { name, bases },
  });
}

const PTEST = plasmid(1, "pTest", "ACGT".repeat(40));
const POTHER = plasmid(2, "pOther", "ACGT".repeat(10));

function app(panels: unknown[]) {
  return entity({
    id: "0190f5c2-0000-7000-8000-0000000a0001",
    hrid: "APP-000001",
    type: "iki/core.App",
    values: { name: "Plasmid QC", spec: { version: 1, panels } },
  });
}

const GRID = {
  id: "plasmids",
  title: "Plasmids",
  width: 6,
  component: "iki.entities",
  props: {
    type: "iki/dna.DNASequence",
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
      type: "iki/dna.DNASequence",
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
    values: {
      ...made.values,
      spec: { version: 1, entity: { type: "iki/dna.DNASequence" }, panels },
    },
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

describe("a plugin's component in an app", () => {
  const MAP: PluginComponent = {
    name: "iki/dna.map",
    plugin: "iki/dna",
    label: "Plasmid map",
    description: null,
    element: "dna-map",
    slots: [{ slot: "entity.tab", type: "iki/dna.DNASequence", label: "Map", search: null }],
    entities: { type: "iki/dna.DNASequence", fields: ["name"], limit: 50 },
    selects: "iki/dna.DNASequence",
    sha256: "ef".repeat(32),
    frame: `/plugin-components/iki/dna.map/${"ef".repeat(32)}/`,
  };
  const MAP_PATH = "/api/v1/components/iki/dna.map/entities";
  const MAPPED = {
    id: "map",
    title: "Plasmid map",
    width: 6,
    component: "iki/dna.map",
    props: { type: "iki/dna.DNASequence", sort: "name" },
  };
  const PICKED = {
    id: "summary",
    component: "iki.text",
    props: { text: "{{ size(map.selection) }} picked." },
  };
  const shown = {
    id: PTEST.id,
    hrid: PTEST.hrid,
    title: "pTest",
    type: PTEST.type,
    revision: 1,
    project: { id: KINASE.id, name: KINASE.name, slug: KINASE.slug },
    values: { name: "pTest" },
    refs: {},
  };

  it("is given what its search finds, and offers what's picked in it", async () => {
    onTypes();
    answer("GET /api/v1/components", [MAP]);
    answer("GET /api/v1/entities/APP-000001", app([MAPPED, PICKED]));
    answer(`POST ${MAP_PATH}`, page([shown]));
    answer("POST /api/v1/entities/search", page([PTEST]));
    await renderApp("/entities/APP-000001/");
    const panel = await screen.findByRole("region", { name: "Plasmid map" });
    const frame = within(panel).getByTitle<HTMLIFrameElement>("Plasmid map");
    expect(frame).toHaveAttribute("sandbox", "allow-scripts");
    const { port, received } = connectFrame(frame);
    await waitFor(() => {
      expect(lastEntities(received)?.items).toEqual([shown]);
    });
    expect(received[0]).toEqual({
      type: "place",
      place: { slot: "app.panel", label: "Plasmid map" },
    });
    // The search the app says, in its project, as the server runs it for the person.
    expect(calls("POST", MAP_PATH)[0]?.body).toEqual({
      home: false,
      search: {
        type: "iki/dna.DNASequence",
        exact_type: false,
        archived: false,
        project: app([]).owner_project?.id,
        sort: { field: "name", descending: false },
      },
      limit: 50,
    });
    expect(await screen.findByText("0 picked.")).toBeInTheDocument();
    // What's picked is read back as the person may read it, and offered as entities.
    port.postMessage({ type: "select", entities: ["DNA-000001", "DNA-000009"] });
    expect(await screen.findByText("1 picked.")).toBeInTheDocument();
  });

  it("is given one entity, as its tab is", async () => {
    onTypes();
    answer("GET /api/v1/components", [MAP]);
    const one = { ...MAPPED, props: { entity: "DNA-000001" } };
    answer("GET /api/v1/entities/APP-000001", app([one]));
    answer("GET /api/v1/entities/DNA-000001", PTEST);
    await renderApp("/entities/APP-000001/");
    const frame = await screen.findByTitle<HTMLIFrameElement>("Plasmid map");
    const { received } = connectFrame(frame);
    await waitFor(() => {
      expect(received.find((m) => m["type"] === "entity")).toEqual({
        type: "entity",
        entity: PTEST,
      });
    });
    // Its tab declares no search, so it's given no others.
    expect(calls("POST", MAP_PATH)).toEqual([]);
  });

  it("that isn't installed costs only its own panel", async () => {
    onTypes();
    answer("GET /api/v1/components", []);
    answer("GET /api/v1/entities/APP-000001", app([MAPPED, PICKED]));
    await renderApp("/entities/APP-000001/");
    const panel = await screen.findByRole("region", { name: "Plasmid map" });
    expect(await within(panel).findByText(/which isn’t installed here/)).toHaveTextContent(
      "This panel shows iki/dna.map, which isn’t installed here.",
    );
    expect(await screen.findByText("0 picked.")).toBeInTheDocument();
    expect(screen.queryByTitle("Plasmid map")).not.toBeInTheDocument();
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
    // Nothing to save yet: unavailable, not busy.
    expect(save).toHaveAttribute("aria-disabled", "true");
    expect(save).not.toHaveAttribute("aria-busy");
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
      "/projects/kinase-atlas/entities/new/?type=iki%2Fcore.App",
    );
  });
});

describe("an app's draft", () => {
  const DRAFT_ID = "0190f5c2-0000-7000-8000-00000000df01";
  const NOTE = { id: "note", component: "iki.text", props: { text: "Welcome to the atlas." } };
  const DRAFT = {
    id: DRAFT_ID,
    project: { id: KINASE.id, name: KINASE.name, slug: KINASE.slug },
    name: "Atlas",
    spec: { version: 1, panels: [NOTE] },
    entity: null,
    agent: "Claude (abc)",
    created_at: "2026-11-15T09:00:00Z",
    expires_at: "2099-11-16T09:00:00Z",
  };

  function onProject() {
    onTypes();
    answer("GET /api/v1/projects", page([project()]));
  }

  it("shows its person the app as it would be, and saves it as one", async () => {
    onProject();
    answer(`GET /api/v1/apps/drafts/${DRAFT_ID}`, DRAFT);
    const saved = app([NOTE]);
    answer("POST /api/v1/entities", saved);
    answer("GET /api/v1/entities/APP-000001", saved);
    const { user, router } = await renderApp(`/projects/kinase-atlas/apps/drafts/${DRAFT_ID}`);
    expect(await screen.findByText("Welcome to the atlas.")).toBeInTheDocument();
    const note = screen.getByRole("note");
    expect(note).toHaveTextContent("A draft, not saved. Claude (abc) drafted it for you.");
    expect(note).toHaveTextContent("Only you can see it");
    await user.click(screen.getByRole("button", { name: "Save as an app in Kinase Atlas" }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/entities/APP-000001/");
    });
    expect(calls("POST", "/api/v1/entities")[0]?.body).toEqual({
      type: "iki/core.App",
      project: KINASE.id,
      values: { name: "Atlas", spec: DRAFT.spec },
      reason: "Saved from a draft Claude (abc) made.",
    });
    expect(await screen.findByText("Saved Atlas as APP-000001.")).toBeVisible();
  });

  it("says why it can't be opened once it's gone", async () => {
    onProject();
    answer(
      `GET /api/v1/apps/drafts/${DRAFT_ID}`,
      problem(404, "not_found", "No such draft of yours, or it’s expired."),
    );
    await renderApp(`/projects/kinase-atlas/apps/drafts/${DRAFT_ID}`);
    expect(
      await screen.findByRole("heading", { name: "This draft can’t be opened" }),
    ).toBeVisible();
  });
});

describe("previewing an app as it's written", () => {
  function onEditing() {
    onTypes();
    answer("GET /api/v1/entities/APP-000001", app([{ ...TEXT, props: { text: "Hello." } }]));
    answer("GET /api/v1/entities/APP-000001/provenance", {});
    answer("GET /api/v1/entities/APP-000001/projects", []);
  }

  it("checks it as saving would, then shows it without saving", async () => {
    onEditing();
    answer("POST /api/v1/apps/check", { problems: [] });
    const { user } = await renderApp("/entities/APP-000001/edit/");
    await user.click(screen.getByRole("button", { name: "Preview" }));
    const preview = screen.getByRole("region", { name: "Preview" });
    expect(await within(preview).findByText("Hello.")).toBeInTheDocument();
    expect(calls("POST", "/api/v1/apps/check")[0]?.body).toEqual({
      spec: { version: 1, panels: [{ ...TEXT, props: { text: "Hello." } }] },
    });
    expect(calls("PATCH")).toEqual([]);
  });

  it("says what saving it would refuse", async () => {
    onEditing();
    const refusal = "Panel `summary`, `text`: `plasmids` isn’t a panel before it.";
    answer("POST /api/v1/apps/check", { problems: [refusal] });
    const { user } = await renderApp("/entities/APP-000001/edit/");
    await user.click(screen.getByRole("button", { name: "Preview" }));
    const preview = screen.getByRole("region", { name: "Preview" });
    expect(await within(preview).findByRole("alert")).toHaveTextContent(refusal);
    expect(within(preview).queryByText("Hello.")).toBeNull();
  });
});

/** What a spec could try, written by someone who can edit a project, against everyone
 * who reads it (ADR 0090). An app shows text, never markup; links only to entities; and
 * reads only what panels offer. */
describe("an app can't", () => {
  function text(value: string) {
    return { id: "words", component: "iki.text", props: { text: value } };
  }

  it("put markup on the page, as text or from an expression", async () => {
    onTypes();
    const markup = '<img src=x onerror="alert(1)"><a href="javascript:alert(2)">x</a>';
    answer(
      "GET /api/v1/entities/APP-000001",
      app([text(markup), { ...text(`{{ "<b>" + "bold</b>" }}`), id: "made" }]),
    );
    await renderApp("/entities/APP-000001/");
    expect(await screen.findByText(markup)).toBeInTheDocument();
    expect(await screen.findByText("<b>bold</b>")).toBeInTheDocument();
    const grid = document.querySelector(".app-grid");
    expect(grid?.querySelector("img, b, script")).toBeNull();
    expect(grid?.querySelector('a[href^="javascript"]')).toBeNull();
  });

  it("link anywhere but an entity, whatever its viewer's expression names", async () => {
    onTypes();
    for (const [index, value] of [
      "{{ '../../settings/webhooks' }}",
      "{{ 'javascript:alert(1)' }}",
      "{{ 'https://evil.example/PRT-000001' }}",
      "{{ {'hrid': '//evil.example'} }}",
    ].entries()) {
      answer(
        "GET /api/v1/entities/APP-000001",
        app([{ id: "shown", component: "iki.viewer", props: { entity: value } }]),
      );
      await renderApp("/entities/APP-000001/");
      const shown = await screen.findByText(/This names no entity/);
      expect(shown, String(index)).toBeInTheDocument();
      cleanup();
    }
    // Nothing was fetched for them: only the page's own, and whether you watch it.
    expect(
      calls("GET").filter(
        (c) =>
          !/^\/api\/v1\/(entities\/APP-000001|entity-types|me|components|notifications|project-roles)/.test(
            c.path,
          ) && !/^\/api\/v1\/entities\/[0-9a-f-]+\/watch$/.test(c.path),
      ),
    ).toEqual([]);
  });

  it("read a panel that hasn't offered anything, whatever it's called", async () => {
    onTypes();
    let release: (value: unknown) => void = () => undefined;
    answer(
      "POST /api/v1/entities/search",
      () =>
        new Promise((resolve) => {
          release = resolve;
        }),
    );
    const grid = { ...GRID, id: "constructor", title: "Constructor" };
    answer(
      "GET /api/v1/entities/APP-000001",
      app([grid, text("{{ size(constructor.rows) }} rows.")]),
    );
    await renderApp("/entities/APP-000001/");
    // `constructor` is something every object has: it still waits for the grid's offer.
    expect(await screen.findByText("Waiting for the panels before it…")).toBeInTheDocument();
    release(respond(200, { items: [PTEST], next_cursor: null }));
    expect(await screen.findByText("1 rows.")).toBeInTheDocument();
  });
});
