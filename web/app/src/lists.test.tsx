/**
 * The lists across projects (ADR 0176): All entities, a type's table, every request and
 * every run, against a fake API. Each is the same table as a project's tab, over every
 * project its reader can read.
 */

import type { Schemas } from "@ikisci/sdk";
import { cleanup, screen, waitFor, within } from "@testing-library/react";
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";

import {
  addFilter,
  entity,
  field,
  KINASE,
  nth,
  PROTEIN,
  renderApp,
  STRUCTURE,
  withTypes,
} from "./entity-fixtures";
import { ACTION, run } from "./run-fixtures";
import { answer, calls, page, project } from "./test-server";

// jsdom lays nothing out; the grid draws the rows that fit in its scrolling region.
const sizes = { clientHeight: 600, clientWidth: 1000, offsetHeight: 600, offsetWidth: 1000 };
const saved: Record<string, PropertyDescriptor | undefined> = {};
beforeAll(() => {
  for (const [name, value] of Object.entries(sizes)) {
    saved[name] = Object.getOwnPropertyDescriptor(HTMLElement.prototype, name);
    Object.defineProperty(HTMLElement.prototype, name, { configurable: true, get: () => value });
  }
});
afterAll(() => {
  for (const [name, descriptor] of Object.entries(saved)) {
    if (descriptor) Object.defineProperty(HTMLElement.prototype, name, descriptor);
  }
});
beforeEach(() => {
  window.localStorage.clear();
});

/** A project the reader is a Viewer of: they read it, and change and run nothing there. */
const CORE = project({
  id: "00000000-0000-7000-8000-0000000000p2",
  name: "Protein Core",
  slug: "protein-core",
  my_role: "viewer",
  can: {
    read: true,
    edit: false,
    manage: false,
    manage_members: false,
    manage_webhooks: false,
    export: false,
    import_bundles: false,
    change_registry: false,
    run: [],
  },
});
/** A project an Org Admin sees and reads nothing of. */
const SEEN = project({
  id: "00000000-0000-7000-8000-0000000000p3",
  name: "Vector Reference QC",
  slug: "vector-qc",
  my_role: null,
  can: { ...CORE.can, read: false },
});

const ref = (p: Schemas["ProjectOut"]) => ({ id: p.id, name: p.name, slug: p.slug });

const ABL1 = entity({ projects: [ref(KINASE), ref(CORE)] });
const INSULIN = entity({
  id: "00000000-0000-7000-8000-00000000e009",
  hrid: "PRT-000009",
  title: "Insulin",
  values: { name: "Insulin", sequence: "GIVEQCCTSICSLYQLENYCN" },
  owner_project: ref(CORE),
  projects: [ref(CORE)],
});

function everywhere(rows: Schemas["EntityOut"][] = [ABL1, INSULIN]): void {
  withTypes();
  answer("GET /api/v1/projects", page([KINASE, CORE, SEEN]));
  answer("POST /api/v1/entities/search", page(rows));
  answer("GET /api/v1/entities/counts", {
    types: [
      { type: PROTEIN.name, label: "Protein", count: 2 },
      { type: STRUCTURE.name, label: "Protein structure", count: 1 },
    ],
    total: 3,
  });
  answer("GET /api/v1/saved-searches", []);
}

const lastSearch = () => calls("POST", "/api/v1/entities/search").at(-1)?.body;

describe("all entities", () => {
  it("lists every record its reader can read, each with its projects", async () => {
    everywhere();
    await renderApp("/entities/");
    expect(screen.getByRole("heading", { level: 1, name: "All entities" })).toBeVisible();
    // Kinase Atlas and Protein Core: not Vector Reference QC, which they only see.
    expect(screen.getByText(/in the 2 projects you belong to/)).toBeVisible();
    const grid = await screen.findByRole("table", { name: "2 entities" });
    const headings = within(grid)
      .getAllByRole("columnheader")
      .map((c) => c.textContent);
    expect(headings).toEqual([
      "",
      "ID",
      "Name",
      "Type",
      "Project",
      "Status",
      "Made by",
      "Last changed",
    ]);
    const rows = within(grid).getAllByRole("row");
    expect(within(nth(rows, 1)).getAllByRole("cell")[4]).toHaveTextContent(
      "Kinase Atlas+1, also in Protein Core",
    );
    expect(within(nth(rows, 2)).getAllByRole("cell")[4]).toHaveTextContent("Protein Core");
    // Across projects, the search names none.
    expect(lastSearch()).not.toHaveProperty("project");
    expect(document.title).toBe("All entities · Example Bio");
  });

  it("counts each type as a pill, which picks it", async () => {
    everywhere();
    const { router, user } = await renderApp("/entities/");
    const pills = await screen.findByRole("group", { name: "Type" });
    await within(pills).findByRole("button", { name: "Protein 2 records" });
    expect(within(pills).getByRole("button", { name: "All types 3 records" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await user.click(within(pills).getByRole("button", { name: "Protein 2 records" }));
    await waitFor(() => {
      expect(router.state.location.search).toEqual({ type: PROTEIN.name });
    });
    expect(within(pills).getByRole("button", { name: "Protein 2 records" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(lastSearch()).toMatchObject({ type: PROTEIN.name });
    expect(calls("GET", "/api/v1/entities/counts").at(-1)?.query.get("project")).toBeNull();
  });

  it("leaves out a count that would be slow, and still offers its type", async () => {
    everywhere();
    answer("GET /api/v1/entities/counts", {
      types: [{ type: PROTEIN.name, label: "Protein", count: null }],
      total: null,
    });
    await renderApp("/entities/");
    const pills = await screen.findByRole("group", { name: "Type" });
    expect(await within(pills).findByRole("button", { name: "Protein" })).toBeVisible();
    expect(within(pills).getByRole("button", { name: "All types" })).toBeVisible();
  });

  it("holds to a project by its slug, and counts that project's", async () => {
    everywhere();
    const { router, user } = await renderApp("/entities/");
    await screen.findByRole("table", { name: "2 entities" });
    const select = screen.getByRole("combobox", { name: "Project" });
    expect(
      within(select)
        .getAllByRole("option")
        .map((o) => o.textContent),
    ).toEqual(["Any project", "Kinase Atlas", "Protein Core"]);
    await user.selectOptions(select, "Protein Core");
    await waitFor(() => {
      expect(router.state.location.search).toEqual({ project: "protein-core" });
    });
    await waitFor(() => {
      expect(lastSearch()).toMatchObject({ project: CORE.id });
    });
    await waitFor(() => {
      expect(calls("GET", "/api/v1/entities/counts").at(-1)?.query.get("project")).toBe(CORE.id);
    });
  });

  it("finds nothing in a project its reader can't read, rather than everything", async () => {
    everywhere();
    const { router, user } = await renderApp("/entities/?project=vector-qc");
    expect(await screen.findByText("There’s no project vector-qc you can read.")).toBeVisible();
    expect(calls("POST", "/api/v1/entities/search")).toHaveLength(0);
    expect(calls("GET", "/api/v1/entities/counts")).toHaveLength(0);
    await user.click(screen.getByRole("button", { name: "Show every project’s" }));
    await waitFor(() => {
      expect(router.state.location.search).toEqual({});
    });
    expect(await screen.findByRole("table", { name: "2 entities" })).toBeVisible();
  });

  it("runs an action on what's picked in a project every one is in", async () => {
    everywhere();
    const { user } = await renderApp("/entities/");
    await screen.findByRole("table", { name: "2 entities" });
    await user.click(screen.getByRole("checkbox", { name: `Select ${ABL1.hrid}` }));
    // ABL1 is in Kinase Atlas, where the reader runs actions.
    expect(screen.getByRole("link", { name: "Run action in Kinase Atlas" })).toHaveAttribute(
      "href",
      `/projects/kinase-atlas/runs/new/?entities=${ABL1.hrid}`,
    );
    // Insulin is only in Protein Core, where they don't: the two share no such project.
    await user.click(screen.getByRole("checkbox", { name: `Select ${INSULIN.hrid}` }));
    expect(screen.queryByRole("link", { name: /^Run action/ })).not.toBeInTheDocument();
    expect(
      screen.getByText(
        "2 selected: an action runs in one project, and these aren’t all in one you run actions in",
      ),
    ).toHaveAttribute("role", "status");
  });

  it("hides a column, and remembers it in this browser", async () => {
    everywhere();
    const { user } = await renderApp("/entities/");
    const grid = await screen.findByRole("table", { name: "2 entities" });
    await user.click(screen.getByRole("button", { name: "Columns" }));
    const choice = await screen.findByRole("group", { name: "Columns to show" });
    await user.click(within(choice).getByRole("checkbox", { name: "Made by" }));
    await user.keyboard("{Escape}");
    expect(within(grid).queryByRole("columnheader", { name: "Made by" })).not.toBeInTheDocument();
    // The download has the columns shown.
    const link = screen.getByRole("link", { name: "Download CSV" });
    const columns = new URL(link.getAttribute("href") ?? "", "https://x.test").searchParams.getAll(
      "columns",
    );
    expect(columns).toContain("project");
    expect(columns).not.toContain("created_by");
    cleanup();
    await renderApp("/entities/");
    const again = await screen.findByRole("table", { name: "2 entities" });
    expect(within(again).queryByRole("columnheader", { name: "Made by" })).not.toBeInTheDocument();
  });

  it("makes a record in a project its maker edits", async () => {
    everywhere();
    const { router, user } = await renderApp("/entities/");
    await user.click(screen.getByRole("button", { name: "New" }));
    const menu = await screen.findByRole("menu", { name: "New" });
    // Protein Core's Viewers make nothing there.
    expect(
      within(menu)
        .getAllByRole("menuitem")
        .map((m) => m.textContent),
    ).toEqual(["In Kinase Atlas"]);
    await user.click(within(menu).getByRole("menuitem", { name: "In Kinase Atlas" }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/projects/kinase-atlas/entities/new/");
    });
  });

  it("says when there's nothing to read yet", async () => {
    everywhere([]);
    await renderApp("/entities/");
    expect(await screen.findByText("No entities you can read yet.")).toBeVisible();
  });
});

describe("a type's table", () => {
  const VIEW: Schemas["SavedSearchOut"] = {
    id: "00000000-0000-7000-8000-0000000000s1",
    project: null,
    name: "Insulins",
    query: { type: PROTEIN.name, q: "insulin" },
    shared: false,
    created_by: { id: "00000000-0000-7000-8000-000000000001", name: "Rosalind Franklin" },
    created_at: "2026-03-14T09:00:00Z",
    updated_at: "2026-03-14T09:00:00Z",
    can: { change: true, delete: true },
  };

  it("is the type's records across projects, its fields as columns", async () => {
    everywhere();
    const { router } = await renderApp(`/entities/types/${PROTEIN.name}/`);
    expect(await screen.findByRole("heading", { level: 1, name: "Proteins" })).toBeVisible();
    const crumbs = screen.getByRole("navigation", { name: "Breadcrumb" });
    expect(within(crumbs).getByRole("link", { name: "Entities" })).toHaveAttribute(
      "href",
      "/entities/",
    );
    const grid = await screen.findByRole("table", { name: "2 proteins" });
    const headings = within(grid)
      .getAllByRole("columnheader")
      .map((c) => c.textContent);
    expect(headings).toContain("Sequence");
    expect(headings).toContain("Project");
    expect(lastSearch()).toMatchObject({ type: PROTEIN.name });
    // The type is the page's: it isn't in the address, and offers no type to pick.
    expect(router.state.location.search).toEqual({});
    expect(screen.queryByRole("combobox", { name: "Type" })).not.toBeInTheDocument();
  });

  it("filters by the type's fields, a sequence by motif", async () => {
    everywhere();
    const { router, user } = await renderApp(`/entities/types/${PROTEIN.name}/`);
    await screen.findByRole("table", { name: "2 proteins" });
    const editor = await addFilter(user, "Sequence");
    expect(within(editor).getByLabelText("Filter 1: condition")).toHaveDisplayValue(
      "has the motif",
    );
    await user.type(within(editor).getByLabelText("Filter 1: value"), "CC");
    const want = [{ field: "sequence", op: "motif", value: "CC" }];
    await waitFor(() => {
      expect(router.state.location.search).toEqual({ filters: want });
    });
    expect(lastSearch()).toMatchObject({ type: PROTEIN.name, where: { all: want } });
  });

  it("shows its saved views as tabs, the one showing current", async () => {
    everywhere();
    answer("GET /api/v1/saved-searches", [VIEW]);
    const { user } = await renderApp(`/entities/types/${PROTEIN.name}/`);
    const tabs = await screen.findByRole("navigation", { name: "Views" });
    expect(within(tabs).getByRole("link", { name: "All proteins" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    await user.click(await within(tabs).findByRole("link", { name: "Insulins" }));
    await waitFor(() => {
      expect(within(tabs).getByRole("link", { name: "Insulins" })).toHaveAttribute(
        "aria-current",
        "page",
      );
    });
    expect(within(tabs).getByRole("link", { name: "All proteins" })).not.toHaveAttribute(
      "aria-current",
    );
    expect(lastSearch()).toMatchObject({ type: PROTEIN.name, q: "insulin" });
  });

  it("saves a view of every project as its saver's own", async () => {
    everywhere();
    answer("POST /api/v1/saved-searches", { ...VIEW, name: "Kinases", query: {} });
    const { user } = await renderApp(`/entities/types/${PROTEIN.name}/?q=kinase`);
    const tabs = await screen.findByRole("navigation", { name: "Views" });
    await user.click(within(tabs).getByRole("button", { name: "Save view" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Save this view" });
    expect(within(dialog).getByText(/Only you see it/)).toBeVisible();
    expect(within(dialog).queryByRole("checkbox")).not.toBeInTheDocument();
    await user.type(within(dialog).getByRole("textbox", { name: "Name" }), "Kinases");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => {
      expect(calls("POST", "/api/v1/saved-searches").at(-1)?.body).toMatchObject({
        name: "Kinases",
        query: { type: PROTEIN.name, q: "kinase" },
        shared: false,
      });
    });
    // Of no project: every one its saver reads.
    expect(calls("POST", "/api/v1/saved-searches").at(-1)?.body).not.toHaveProperty("project");
  });

  it("says when there's no such type", async () => {
    everywhere();
    await renderApp("/entities/types/acme/gone.Thing/");
    expect(await screen.findByText(/There’s no entity type acme\/gone.Thing/)).toBeVisible();
  });
});

describe("every request", () => {
  const ASK: Schemas["EntityTypeOut"] = {
    ...PROTEIN,
    name: "acme/core.AssayRequest",
    label: "Assay request",
    label_plural: "Assay requests",
    hrid_prefix: "ARQ",
    request: { service_fields: ["status"], sends: [] },
    fields: [field({ name: "title", label: "Title", type: "string" })],
  };

  it("is every request its reader can read, and no other record", async () => {
    everywhere([]);
    answer("GET /api/v1/entity-types", [PROTEIN, ASK]);
    await renderApp("/requests/");
    expect(await screen.findByRole("heading", { level: 1, name: "Requests" })).toBeVisible();
    await waitFor(() => {
      expect(lastSearch()).toMatchObject({
        where: { all: [{ field: "type", op: "in", value: [ASK.name] }] },
      });
    });
    expect(lastSearch()).not.toHaveProperty("project");
    expect(await screen.findByText("No requests you can read yet.")).toBeVisible();
  });

  it("says when nothing installed makes requests", async () => {
    everywhere([]);
    await renderApp("/requests/");
    expect(await screen.findByText(/No plugin here makes requests between projects/)).toBeVisible();
  });
});

describe("every run", () => {
  it("lists runs of every project the reader reads, each with its project", async () => {
    everywhere();
    answer("GET /api/v1/actions", [ACTION]);
    answer(
      "GET /api/v1/runs",
      page([
        run(),
        run({ id: "00000000-0000-7000-8000-0000000000r2", hrid: "RUN-000002", project: CORE.id }),
      ]),
    );
    const { router, user } = await renderApp("/runs/");
    expect(screen.getByRole("heading", { level: 1, name: "Runs" })).toBeVisible();
    const table = await screen.findByRole("table", { name: "Runs you can read" });
    const rows = within(table).getAllByRole("row");
    expect(within(nth(rows, 1)).getAllByRole("cell")[2]).toHaveTextContent("Kinase Atlas");
    expect(within(nth(rows, 2)).getAllByRole("cell")[2]).toHaveTextContent("Protein Core");
    expect(calls("GET", "/api/v1/runs").at(-1)?.query.get("project")).toBeNull();

    await user.selectOptions(screen.getByRole("combobox", { name: "Project" }), "Protein Core");
    await waitFor(() => {
      expect(router.state.location.search).toEqual({ project: "protein-core" });
    });
    await waitFor(() => {
      expect(calls("GET", "/api/v1/runs").at(-1)?.query.get("project")).toBe(CORE.id);
    });
    expect(await screen.findByRole("table", { name: "Runs in Protein Core" })).toBeVisible();
  });
});
