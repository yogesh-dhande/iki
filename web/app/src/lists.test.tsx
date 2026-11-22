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
  COLLECTION_TYPE,
  entity,
  field,
  KINASE,
  nth,
  PROTEIN,
  renderApp,
  STRUCTURE,
  withTypes,
} from "./entity-fixtures";
import { typeDisplayName, typeDisplayText } from "./entities";
import { countOf } from "./lists";
import { keyFieldsOf, sizeColumns } from "./pages/entity-grid";
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
      { type: PROTEIN.name, label: "Protein", count: rows.length },
      { type: STRUCTURE.name, label: "Protein structure", count: 1 },
    ],
    total: rows.length + 1,
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
    const grid = await screen.findByRole("table", { name: "3 entities" });
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
      // Sorted by it, newest first, as the lists are unless asked otherwise.
      "Changed▼",
    ]);
    const rows = within(grid).getAllByRole("row");
    expect(within(nth(rows, 1)).getAllByRole("cell")[4]).toHaveTextContent(
      "Kinase Atlas+1, also in Protein Core",
    );
    expect(within(nth(rows, 2)).getAllByRole("cell")[4]).toHaveTextContent("Protein Core");
    // Across projects, the search names none; newest changes first.
    expect(lastSearch()).not.toHaveProperty("project");
    expect(lastSearch()).toMatchObject({ sort: { field: "updated_at", descending: true } });
    expect(screen.getByRole("button", { name: "Sort: Changed, newest first" })).toBeVisible();
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
    await screen.findByRole("table", { name: "3 entities" });
    const select = screen.getByRole("combobox", { name: "Project" });
    expect(
      within(select)
        .getAllByRole("option")
        .map((o) => o.textContent),
    ).toEqual(["Any", "Kinase Atlas", "Protein Core"]);
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
    // Not counted there either; the sidebar's counts are of every project.
    expect(calls("GET", "/api/v1/entities/counts").filter((c) => c.query.has("project"))).toEqual(
      [],
    );
    await user.click(screen.getByRole("button", { name: "Show every project’s" }));
    await waitFor(() => {
      expect(router.state.location.search).toEqual({});
    });
    expect(await screen.findByRole("table", { name: "3 entities" })).toBeVisible();
  });

  it("runs an action on what's picked in a project every one is in", async () => {
    everywhere();
    const { user } = await renderApp("/entities/");
    await screen.findByRole("table", { name: "3 entities" });
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
    const grid = await screen.findByRole("table", { name: "3 entities" });
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
    const again = await screen.findByRole("table", { name: "3 entities" });
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

  it("says exactly how many there are, as the counts do, and how many of them match", async () => {
    everywhere([ABL1, INSULIN]);
    answer("POST /api/v1/entities/search", { items: [ABL1, INSULIN], next_cursor: "more" });
    answer("GET /api/v1/entities/counts", {
      types: [{ type: PROTEIN.name, label: "Protein", count: 1204 }],
      total: 1204,
    });
    const { user } = await renderApp("/entities/");
    // Not "2+": the counts know.
    expect(await screen.findByRole("table", { name: "1,204 entities" })).toBeVisible();
    await user.type(screen.getByRole("searchbox", { name: "Search" }), "abl");
    expect(await screen.findByRole("table", { name: "2+ of 1,204 entities" })).toBeVisible();
    // Archived ones aren't counted: then it says what it loaded.
    answer("POST /api/v1/entities/search", page([ABL1]));
    cleanup();
    await renderApp("/entities/?show=archived");
    expect(await screen.findByRole("table", { name: "1 entity, archived" })).toBeVisible();
  });

  it("sorts newest changes first unless the address says otherwise", async () => {
    everywhere();
    const { router, user } = await renderApp("/entities/");
    await screen.findByRole("table", { name: "3 entities" });
    await user.click(screen.getByRole("button", { name: "Sort: Changed, newest first" }));
    const menu = await screen.findByRole("menu", { name: /^Sort/ });
    expect(
      within(menu)
        .getAllByRole("menuitemradio")
        .map((m) => m.textContent)
        .slice(0, 4),
    ).toEqual([
      "Changed, newest first",
      "Changed, oldest first",
      "Created, newest first",
      "Created, oldest first",
    ]);
    await user.click(within(menu).getByRole("menuitemradio", { name: "Created, oldest first" }));
    await waitFor(() => {
      expect(router.state.location.search).toEqual({ sort: "created_at" });
    });
    await waitFor(() => {
      expect(lastSearch()).toMatchObject({ sort: { field: "created_at", descending: false } });
    });
    // Back to the default: the address names no sort.
    await user.click(screen.getByRole("button", { name: "Sort: Created, oldest first" }));
    await user.click(
      within(await screen.findByRole("menu", { name: /^Sort/ })).getByRole("menuitemradio", {
        name: "Changed, newest first",
      }),
    );
    await waitFor(() => {
      expect(router.state.location.search).toEqual({});
    });
    // The Changed heading, sorted by default, turns the order round on a click.
    const grid = screen.getByRole("table", { name: "3 entities" });
    expect(within(grid).getByRole("columnheader", { name: /Changed/ })).toHaveAttribute(
      "aria-sort",
      "descending",
    );
    await user.click(within(grid).getByRole("button", { name: /Changed/ }));
    await waitFor(() => {
      expect(router.state.location.search).toEqual({ sort: "updated_at" });
    });
  });

  it("shows archived records through the Filter menu, as a chip", async () => {
    everywhere();
    const { router, user } = await renderApp("/entities/");
    await screen.findByRole("table", { name: "3 entities" });
    await user.click(screen.getByRole("button", { name: "Filter" }));
    await user.click(await screen.findByRole("menuitem", { name: "Show archived only" }));
    await waitFor(() => {
      expect(router.state.location.search).toEqual({ show: "archived" });
    });
    const filters = screen.getByRole("group", { name: "Filters" });
    expect(within(filters).getByRole("button", { name: "Showing: Archived only" })).toBeVisible();
    await user.click(
      within(filters).getByRole("button", {
        name: "Show current ones only, not archived only",
      }),
    );
    await waitFor(() => {
      expect(router.state.location.search).toEqual({});
    });
  });

  it("tells two types with the same label apart by their plugins", async () => {
    const OTHER = { ...PROTEIN, name: "acme/lab.Protein", plugin: "acme/lab", subtypes: [] };
    everywhere();
    answer("GET /api/v1/entity-types", [COLLECTION_TYPE, PROTEIN, STRUCTURE, OTHER]);
    answer("GET /api/v1/entities/counts", {
      types: [
        { type: PROTEIN.name, label: "Protein", count: 2 },
        { type: OTHER.name, label: "Protein", count: 1 },
      ],
      total: 3,
    });
    await renderApp("/entities/");
    const pills = await screen.findByRole("group", { name: "Type" });
    expect(
      await within(pills).findByRole("button", { name: `Protein ${PROTEIN.plugin} 2 records` }),
    ).toBeVisible();
    expect(within(pills).getByRole("button", { name: "Protein acme/lab 1 record" })).toBeVisible();
    const grid = await screen.findByRole("table", { name: "3 entities" });
    const row = nth(within(grid).getAllByRole("row"), 1);
    expect(within(row).getAllByRole("cell")[3]).toHaveTextContent(`Protein ${PROTEIN.plugin}`);
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

describe("a list's columns", () => {
  const row = (hrid: string, title: string) => entity({ hrid, title, id: `id-${hrid}` });

  it("sizes an ID to the longest, cuts only long text short, and grows to fill", () => {
    const rows = [row("PRT-000001", "ABL1"), row("PRT-1234567890", "A".repeat(80))];
    const { template, widths } = sizeColumns(
      [
        { label: "Select", width: "calc(40rem / 13)" },
        { label: "ID", fit: "id", text: (e) => e.hrid },
        { label: "Name", fit: "long", text: (e) => e.title },
        { label: "Status", text: () => "Open", pills: true },
      ],
      rows,
    );
    const [select, id, name, status] = widths;
    expect(select).toBe(40);
    // Fourteen monospace characters, and the cell's padding: never cut short.
    expect(id).toBeGreaterThanOrEqual(14 * 7.3 + 22);
    expect(template).toMatch(
      new RegExp(`^calc\\(40rem / 13\\) calc\\(${String(Math.round(id ?? 0))}rem / 13\\) minmax`),
    );
    // The name's eighty capitals would take 700 pixels: it gives way, down to 180.
    expect(name).toBe(180);
    expect(template).toContain("minmax(calc(180rem / 13), 4.20fr)");
    // "Open" in a pill: wider than its letters alone.
    expect(status).toBeGreaterThan(4 * 7 + 22 + 30);
  });

  it("shows a type's key facts first: what makes it, then what's worked out from it", () => {
    const own = (
      name: string,
      type: string,
      extra: Partial<(typeof PROTEIN.fields)[number]> = {},
    ) => field({ name, label: name, type, ...extra });
    const fields = [
      own("name", "string"),
      own("description", "text"),
      own("cdr_h3", "string", { plugin: "iki/antibody", computed: true }),
      own("vendor", "string"),
      own("organism", "ref"),
      own("features", "json"),
      own("sequence", "string", { format: "protein_sequence" }),
      own("found_with", "string", { computed: true }),
      own("length", "quantity", { computed: true }),
      own("verified", "bool"),
    ];
    const type = { ...PROTEIN, identity: ["sequence"], fields };
    const named = fields[0];
    expect(keyFieldsOf(type, [type], fields, named).map((f) => f.name)).toEqual([
      "sequence",
      "length",
      "organism",
      "vendor",
      "verified",
      // Last: what another plugin adds, and text a plugin writes, in their order.
      "cdr_h3",
      "found_with",
    ]);
  });

  it("names a type with its plugin only when another has its label", () => {
    const sample = { name: "acme/example.Sample", label: "Sample", plugin: "acme/example" };
    const lims = { name: "iki/example_lims.Sample", label: "Sample", plugin: "iki/example_lims" };
    expect(typeDisplayName(sample, [sample, lims, PROTEIN])).toEqual({
      label: "Sample",
      plugin: "acme/example",
    });
    expect(typeDisplayText(lims, [sample, lims])).toBe("Sample (iki/example_lims)");
    expect(typeDisplayName(PROTEIN, [sample, lims, PROTEIN])).toEqual({ label: "Protein" });
  });

  it("counts what the counts know, and nothing when they don't", () => {
    const counts = {
      types: [
        { type: "a", label: "A", count: 3 },
        { type: "b", label: "B", count: 4 },
      ],
      total: 7,
    };
    expect(countOf(counts, null)).toBe(7);
    expect(countOf(counts, new Set(["b", "c"]))).toBe(4);
    expect(countOf({ types: [{ type: "a", label: "A", count: null }], total: null }, null)).toBe(
      null,
    );
    expect(countOf(undefined, null)).toBe(null);
  });
});
