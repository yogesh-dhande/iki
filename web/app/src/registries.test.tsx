/**
 * Registries (ADR 0115) against a fake API: a project's record of a registry
 * entry, superseded entries and moving records, choosing whose record an HRID
 * means, the entry's history, a project's registry, and registrars' pages.
 */

import type { Schemas } from "@ikisci/sdk";
import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  ALICE,
  entity,
  field,
  fieldHistory,
  KINASE,
  PROTEIN,
  PROTEIN_ID,
  renderApp,
  revision,
} from "./entity-fixtures";
import {
  answer,
  calls,
  MAIN_REGISTRY,
  ME,
  page,
  problem,
  project,
  respond,
  signedIn,
  TEAM,
} from "./test-server";

/** A protein whose registry entry has a curated fact and a derived length. */
const PROTEIN_R: Schemas["EntityTypeOut"] = {
  ...PROTEIN,
  fields: [
    ...PROTEIN.fields,
    field({ name: "length", label: "Length", type: "int", registry: "derived" }),
    field({ name: "uniprot", label: "UniProt ID", type: "string", registry: "fact" }),
  ],
};

const ENTRY_ID = "00000000-0000-7000-8000-00000000e0e1";
const NEW_ENTRY_ID = "00000000-0000-7000-8000-00000000e0e2";
const CLIENT_REGISTRY = {
  id: "00000000-0000-7000-8000-0000000000r2",
  name: "Client X",
  slug: "client-x",
};
const OTHER = project({
  id: "00000000-0000-7000-8000-0000000000p2",
  name: "Antibody Lab",
  slug: "antibody-lab",
});

function entryRef(overrides: Partial<Schemas["EntryRefOut"]> = {}): Schemas["EntryRefOut"] {
  return {
    id: ENTRY_ID,
    hrid: "PRT-000001",
    registry: MAIN_REGISTRY,
    revision: 3,
    superseded: null,
    ...overrides,
  };
}

const VALUES = {
  name: "ABL1 kinase",
  sequence: "MLEICLKLVG",
  molecular_weight: 52341.5,
  length: 10,
  uniprot: "P00519",
};

function record(overrides: Partial<Schemas["EntityOut"]> = {}): Schemas["EntityOut"] {
  return entity({ entry: entryRef(), values: VALUES, ...overrides });
}

function registry(overrides: Partial<Schemas["RegistryOut"]> = {}): Schemas["RegistryOut"] {
  return {
    ...MAIN_REGISTRY,
    description: "",
    default: true,
    all_teams: true,
    teams: [],
    created_at: "2026-01-01T00:00:00Z",
    can: { manage: false, curate: false, curate_lists: false },
    ...overrides,
  };
}

/** The projects the reader can read; by slug, the one with it. */
function onProjects(...projects: Schemas["ProjectOut"][]): void {
  answer("GET /api/v1/projects", ({ query }: { query: URLSearchParams }) =>
    page(query.has("slug") ? projects.filter((p) => p.slug === query.get("slug")) : projects),
  );
}

/** A record's page in Kinase Atlas, by its HRID and project. */
function onRecord(current = record()): void {
  signedIn();
  answer("GET /api/v1/entity-types", [PROTEIN_R]);
  answer("GET /api/v1/components", []);
  onProjects(KINASE, OTHER);
  answer("GET /api/v1/entities/PRT-000001", current);
  answer(`GET /api/v1/entities/${PROTEIN_ID}/provenance`, {
    uniprot: revision({ number: 2, entry: true, user: null, created_at: null, via: "" }),
    name: revision(),
  });
  answer(`GET /api/v1/entities/${PROTEIN_ID}/projects`, []);
}

describe("a project's record of a registry entry", () => {
  it("shows its registry ID, and which values are the entry's and which the project's", async () => {
    onRecord();
    const { user } = await renderApp("/entities/PRT-000001/?project=kinase-atlas");
    expect(calls("GET", "/api/v1/entities/PRT-000001")[0]?.query.get("project")).toBe(KINASE.id);
    // Everything else it reads, it reads by the record's ID: other projects have its HRID.
    expect(calls("GET", `/api/v1/entities/${PROTEIN_ID}/provenance`)).toHaveLength(1);

    const entry = screen.getByRole("region", { name: "Registry entry PRT-000001" });
    expect(entry).toHaveTextContent(
      "In the Main registry. These values are the entry’s: every project with PRT-000001 has the same ones.",
    );
    // Its trail counts its entry's revisions too: its tab and its details say so.
    const tabs = await screen.findByRole("navigation", { name: "Record" });
    const trailTab = within(tabs).getByRole("link", {
      name: /^Audit trail,\s*5\s*revisions: 2 of this record, 3 of its entry$/,
    });
    expect(trailTab.querySelector(".tab-count")).toHaveAttribute(
      "title",
      "5 revisions: 2 of this record, 3 of its entry",
    );
    expect(screen.getByRole("region", { name: "Details" })).toHaveTextContent(
      "Its audit trail has 5: 2 of this record, 3 of its entry",
    );
    const terms = within(entry)
      .getAllByRole("term")
      .map((t) => t.textContent);
    expect(terms).toEqual([
      "SequenceIdentity",
      "Molecular weightDerived from its identity",
      "LengthDerived from its identity",
      "UniProt IDRegistry fact",
    ]);
    // Set through a project the reader can't read: what, never who or when.
    answer(`GET /api/v1/entries/${ENTRY_ID}/revisions/2`, {
      ...revision({ number: 2, entry: true, user: null, created_at: null, via: "" }),
      values: VALUES,
      changes: { uniprot: { before: null, after: "P00519" } },
      refs: {},
      files: {},
    });
    const fact = await fieldHistory(user, "UniProt ID");
    expect(fact).toHaveTextContent(/BeforeNot setAfterP00519/);
    expect(fact).toHaveTextContent("Revision2 of its registry entry");
    expect(fact).toHaveTextContent("through a project you can’t read");
    expect(fact).not.toHaveTextContent(/Who|When|Rosalind/);
    await user.keyboard("{Escape}");
    expect(within(entry).getByRole("link", { name: "Open the entry" })).toHaveAttribute(
      "href",
      "/entries/PRT-000001/",
    );

    const own = screen.getByRole("region", { name: "Project values" });
    expect(own).toHaveTextContent(
      "Kinase Atlas’s own record of PRT-000001. Other projects with it keep their own.",
    );
    expect(
      within(own)
        .getAllByRole("term")
        .map((t) => t.textContent),
    ).toEqual(["Name"]);

    const details = screen.getByRole("region", { name: "Details" });
    expect(details).toHaveTextContent("Its registry entry’s ID");
    expect(details).toHaveTextContent("RegistryMain");
  });

  it("keeps its project on every tab", async () => {
    onRecord();
    await renderApp("/entities/PRT-000001/?project=kinase-atlas");
    const tabs = await screen.findByRole("navigation", { name: "Record" });
    expect(within(tabs).getByRole("link", { name: /^Audit trail/ })).toHaveAttribute(
      "href",
      "/entities/PRT-000001/audit/?project=kinase-atlas",
    );
    expect(within(tabs).getByRole("link", { name: "Overview" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.getByRole("link", { name: "Edit" })).toHaveAttribute(
      "href",
      "/entities/PRT-000001/edit/?project=kinase-atlas",
    );
  });

  it("switches to another of the reader's projects' records of it", async () => {
    onRecord();
    answer(`GET /api/v1/entries/${ENTRY_ID}`, {
      id: ENTRY_ID,
      hrid: "PRT-000001",
      type: "iki/protein.Protein",
      registry: MAIN_REGISTRY,
      revision: 3,
      created_at: "2026-03-14T09:00:00Z",
      values: {},
      refs: {},
      files: {},
      superseded: null,
      records: [
        { id: PROTEIN_ID, project: { id: KINASE.id, name: KINASE.name, slug: KINASE.slug } },
        { id: "r2", project: { id: OTHER.id, name: OTHER.name, slug: OTHER.slug } },
      ],
      holders: null,
      add_to: [],
      can: { curate: false },
    });
    const { router, user } = await renderApp("/entities/PRT-000001/?project=kinase-atlas");
    const own = screen.getByRole("region", { name: "Project values" });
    const picker = await within(own).findByRole("combobox", { name: "Project" });
    expect(picker).toHaveValue("kinase-atlas");
    expect(
      within(picker)
        .getAllByRole("option")
        .map((o) => o.textContent),
    ).toEqual(["Antibody Lab", "Kinase Atlas"]);
    await user.selectOptions(picker, "antibody-lab");
    await waitFor(() => {
      expect(router.state.location.search).toEqual({ project: "antibody-lab" });
    });
  });

  it("links a reference to an entry to its project's record of it", async () => {
    const structure = {
      ...PROTEIN,
      name: "iki/protein.ProteinStructure",
      label: "Protein structure",
      identity: [],
      fields: [field({ name: "chain", label: "Chain", type: "ref", to: ["iki/protein.Protein"] })],
    };
    signedIn();
    answer("GET /api/v1/entity-types", [PROTEIN_R, structure]);
    answer("GET /api/v1/components", []);
    onProjects(KINASE);
    answer(
      "GET /api/v1/entities/PST-000001",
      entity({
        hrid: "PST-000001",
        type: "iki/protein.ProteinStructure",
        values: { chain: ENTRY_ID },
        refs: {
          [ENTRY_ID]: {
            hrid: "PRT-000001",
            title: "PRT-000001",
            type: "iki/protein.Protein",
            archived: false,
            purged: false,
          },
        },
      }),
    );
    answer("GET /api/v1/entities/PST-000001/provenance", {});
    answer("GET /api/v1/entities/PST-000001/projects", []);
    await renderApp("/entities/PST-000001/");
    expect(screen.getByRole("link", { name: "PRT-000001" })).toHaveAttribute(
      "href",
      "/entities/PRT-000001/?project=kinase-atlas",
    );
  });

  it("asks which project's record, when the HRID is in several of the reader's projects", async () => {
    onRecord();
    answer("GET /api/v1/entities/PRT-000001", ({ query }: { query: URLSearchParams }) =>
      query.get("project") === KINASE.id
        ? record()
        : problem(
            409,
            "choose_project",
            "PRT-000001 is in several projects you can read: say which.",
            undefined,
            {
              records: [
                { entity: "r2", project: { id: OTHER.id, name: OTHER.name, slug: OTHER.slug } },
                {
                  entity: PROTEIN_ID,
                  project: { id: KINASE.id, name: KINASE.name, slug: KINASE.slug },
                },
              ],
            },
          ),
    );
    const { router, user } = await renderApp("/entities/PRT-000001/audit/");
    expect(screen.getByRole("heading", { level: 1, name: "PRT-000001" })).toBeVisible();
    expect(
      screen.getByText(
        "PRT-000001 is in 2 of your projects. Each has its own record of it, with its own values and history.",
      ),
    ).toBeVisible();
    const choices = within(
      screen.getByRole("region", { name: "Which project’s record?" }),
    ).getAllByRole("link");
    expect(choices.map((c) => c.textContent)).toEqual(["Antibody Lab", "Kinase Atlas"]);
    expect(choices[1]).toHaveAttribute("href", "/entities/PRT-000001/audit/?project=kinase-atlas");
    answer(`GET /api/v1/entities/${PROTEIN_ID}/revisions`, page([revision()]));
    answer(`GET /api/v1/entries/${ENTRY_ID}/revisions`, page([]));
    await user.click(screen.getByRole("link", { name: "Kinase Atlas" }));
    expect(
      await screen.findByRole("heading", { level: 1, name: /^ABL1 kinase/ }),
    ).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/entities/PRT-000001/audit/");
    expect(router.state.location.search).toEqual({ project: "kinase-atlas" });
  });
});

describe("a superseded entry", () => {
  const superseded = () =>
    record({
      entry: entryRef({
        superseded: {
          by: {
            hrid: "PRT-000002",
            title: "PRT-000002",
            type: "iki/protein.Protein",
            archived: false,
            purged: false,
          },
          by_id: NEW_ENTRY_ID,
          at: "2026-11-10T09:00:00Z",
          reason: "Registered twice: the same sequence, cleaned up.",
        },
      }),
    });

  it("names its replacement, and moves the record there with a reason", async () => {
    onRecord(superseded());
    const moved = record({
      hrid: "PRT-000002",
      entry: entryRef({ id: NEW_ENTRY_ID, hrid: "PRT-000002" }),
      revision: 3,
    });
    answer(`POST /api/v1/entities/${PROTEIN_ID}/move`, {
      ...moved,
      repointed: [
        {
          hrid: "BAT-000004",
          title: "Batch 4",
          type: "acme/lab.Batch",
          archived: false,
          purged: false,
        },
      ],
    });
    answer("GET /api/v1/entities/PRT-000002", moved);
    const { router, user } = await renderApp("/entities/PRT-000001/?project=kinase-atlas");
    const banner = screen.getByRole("note", { name: "PRT-000001 was superseded by PRT-000002" });
    expect(banner).toHaveTextContent(
      "A registrar of the Main registry replaced it on 10 Nov 2026: “Registered twice: the same sequence, cleaned up.”",
    );
    expect(banner).toHaveTextContent(
      "Move Kinase Atlas’s record to PRT-000002, so its data is under the right ID.",
    );
    expect(screen.getByText("Superseded")).toBeVisible();

    await user.click(within(banner).getByRole("button", { name: "Move to PRT-000002" }));
    const dialog = screen.getByRole("alertdialog", { name: "Move to PRT-000002?" });
    expect(dialog).toHaveTextContent(
      "Kinase Atlas’s own records that refer to PRT-000001 will refer to PRT-000002, each as a new revision with your reason",
    );
    expect(dialog).toHaveTextContent(
      "This can’t be undone. Other projects move their own records.",
    );
    // A reason is needed, for the record.
    await user.click(within(dialog).getByRole("button", { name: "Move to PRT-000002" }));
    expect(within(dialog).getByRole("textbox", { name: /Reason/ })).toHaveAccessibleDescription(
      /Say why, for the record\./,
    );
    expect(calls("POST")).toEqual([]);
    await user.type(within(dialog).getByRole("textbox", { name: /Reason/ }), "Registrar’s notice");
    await user.click(within(dialog).getByRole("button", { name: "Move to PRT-000002" }));

    const done = await screen.findByText(
      "Moved to PRT-000002: Kinase Atlas’s record holds it now instead of PRT-000001.",
    );
    expect(router.state.location.pathname).toBe("/entities/PRT-000002/");
    expect(router.state.location.search).toEqual({ project: "kinase-atlas" });
    expect(calls("POST")[0]?.body).toEqual({ reason: "Registrar’s notice" });
    const note = done.closest("[role=status]");
    if (!(note instanceof HTMLElement)) throw new Error("no note");
    expect(note).toHaveTextContent("This record refers to PRT-000002 now:");
    expect(within(note).getByRole("link", { name: "Batch 4" })).toHaveAttribute(
      "href",
      "/entities/BAT-000004/",
    );
  });

  it("tells people who can't move it who can", async () => {
    onRecord({
      ...superseded(),
      can: {
        edit: false,
        correct: false,
        edit_amount: false,
        draw: false,
        states: {},
        sign: [],
      },
    });
    await renderApp("/entities/PRT-000001/?project=kinase-atlas");
    const banner = screen.getByRole("note", { name: /was superseded/ });
    expect(banner).toHaveTextContent(
      "An Editor in Kinase Atlas can move this record to PRT-000002.",
    );
    expect(within(banner).queryByRole("button")).not.toBeInTheDocument();
  });
});

describe("a record's audit trail", () => {
  it("shows its entry's revisions, and of those it can't see the source of, only what they set", async () => {
    onRecord();
    answer(`GET /api/v1/entities/${PROTEIN_ID}/revisions`, page([revision()]));
    answer(
      `GET /api/v1/entries/${ENTRY_ID}/revisions`,
      page([
        revision({
          number: 3,
          kind: "curated",
          entry: true,
          changed_fields: ["uniprot"],
          user: { ...ALICE, name: "Maurice Wilkins" },
          reason: "Curated from UniProt.",
          created_at: "2026-11-12T09:00:00Z",
        }),
        revision({
          number: 2,
          kind: "edited",
          entry: true,
          changed_fields: ["length"],
          user: null,
          via: "",
          created_at: null,
        }),
      ]),
    );
    await renderApp("/entities/PRT-000001/audit/?project=kinase-atlas");
    expect(screen.getByRole("heading", { level: 2, name: "Kinase Atlas’s record" })).toBeVisible();
    const section = screen.getByRole("region", { name: "Registry entry PRT-000001" });
    expect(section).toHaveTextContent("Its values are every holding project’s");
    const rows = await within(section).findAllByRole("listitem");
    expect(rows[0]).toHaveTextContent("Revision 3: Facts changed by Maurice Wilkins");
    expect(rows[0]).toHaveTextContent("“Curated from UniProt.”");
    expect(rows[1]).toHaveTextContent("Revision 2: Set Length");
    expect(rows[1]).toHaveTextContent("Through a project you can’t read");
    expect(rows[1]).not.toHaveTextContent(/Someone|ago|web app|by /);
  });

  it("shows what an entry's revision changed, before and after", async () => {
    onRecord();
    answer(`GET /api/v1/entities/${PROTEIN_ID}/revisions`, page([]));
    answer(
      `GET /api/v1/entries/${ENTRY_ID}/revisions`,
      page([
        revision({
          number: 3,
          kind: "curated",
          entry: true,
          changed_fields: ["uniprot"],
          reason: "UniProt merged it.",
          created_at: "2026-11-12T09:00:00Z",
        }),
      ]),
    );
    answer(`GET /api/v1/entries/${ENTRY_ID}/revisions/3`, {
      ...revision({
        number: 3,
        kind: "curated",
        entry: true,
        changed_fields: ["uniprot"],
        reason: "UniProt merged it.",
        created_at: "2026-11-12T09:00:00Z",
      }),
      values: { ...VALUES, uniprot: "P00520" },
      changes: { uniprot: { before: "P00519", after: "P00520" } },
      refs: {},
      files: {},
    });
    const { user } = await renderApp("/entities/PRT-000001/audit/?project=kinase-atlas");
    const section = screen.getByRole("region", { name: "Registry entry PRT-000001" });
    await user.click(
      await within(section).findByRole("button", { name: "Show changes in entry revision 3" }),
    );
    const table = await within(section).findByRole("table");
    expect(
      within(table)
        .getAllByRole("row")
        .map((r) => r.textContent),
    ).toEqual(["FieldBeforeAfter", "UniProt IDP00519P00520"]);
  });
});

describe("editing a record", () => {
  function onEdit(values: Record<string, unknown>): void {
    onRecord(record({ values }));
    answer("GET /api/v1/entities", page([]));
  }

  it("can't change the entry's values once they're set", async () => {
    onEdit(VALUES);
    await renderApp("/entities/PRT-000001/edit/?project=kinase-atlas");
    const fact = screen.getByRole("textbox", { name: /^UniProt ID/ });
    expect(fact).toBeDisabled();
    expect(fact).toHaveAccessibleDescription(
      "A registry fact: only the Main registry’s registrars change it.",
    );
    const length = screen.getByRole("textbox", { name: /^Length/ });
    expect(length).toBeDisabled();
    expect(length).toHaveAccessibleDescription(
      "Derived from its identity, in its registry entry: it can’t change.",
    );
    expect(screen.getByRole("textbox", { name: /^Name/ })).toBeEnabled();
  });

  it("sets an empty fact, saying who changes it after", async () => {
    onEdit({ name: "ABL1 kinase", sequence: "MLEICLKLVG" });
    answer("PATCH /api/v1/entities/" + PROTEIN_ID, record());
    const { user } = await renderApp("/entities/PRT-000001/edit/?project=kinase-atlas");
    const fact = screen.getByRole("textbox", { name: /^UniProt ID/ });
    expect(fact).toBeEnabled();
    expect(fact).toHaveAccessibleDescription(
      "A registry fact: every project with this protein has the same one. Once it’s set, only the Main registry’s registrars change it.",
    );
    await user.type(fact, "P00519");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await screen.findByText("Saved revision 2 of PRT-000001.");
    expect(calls("PATCH")[0]?.body).toEqual({ values: { uniprot: "P00519" }, reason: "" });
  });
});

describe("a type's fields", () => {
  it("says where each is kept", async () => {
    signedIn();
    answer("GET /api/v1/entity-types", [PROTEIN_R]);
    await renderApp("/types/iki/protein.Protein/");
    const rows = within(screen.getByRole("table")).getAllByRole("row").slice(1);
    expect(rows.map((r) => r.querySelector("td:last-child .badge")?.textContent)).toEqual([
      "Project record",
      "Identity",
      "Registry: derived",
      "Registry: derived",
      "Registry: fact",
    ]);
    expect(rows[4]).toHaveTextContent("Once set, only the registry’s registrars change it.");
  });
});

describe("a project's registry", () => {
  it("is chosen when the project is created, from those its team may use", async () => {
    signedIn();
    answer("GET /api/v1/teams", page([TEAM]));
    answer(
      "GET /api/v1/registries",
      page([registry(), registry({ ...CLIENT_REGISTRY, default: false })]),
    );
    answer("POST /api/v1/projects", respond(201, project({ registry: CLIENT_REGISTRY })));
    onProjects(project({ registry: CLIENT_REGISTRY }));
    const { user } = await renderApp("/projects/new/");
    const picker = await screen.findByRole("combobox", { name: "Registry" });
    expect(picker).toHaveValue(MAIN_REGISTRY.id);
    expect(
      within(picker)
        .getAllByRole("option")
        .map((o) => o.textContent),
    ).toEqual(["Main (the default)", "Client X"]);
    expect(picker).toHaveAccessibleDescription(/has one ID in its registry/);
    expect(calls("GET", "/api/v1/registries")[0]?.query.get("team")).toBe(TEAM.id);
    await user.selectOptions(picker, CLIENT_REGISTRY.id);
    await user.type(screen.getByRole("textbox", { name: "Project name" }), "Client work");
    await user.click(screen.getByRole("button", { name: "Create project" }));
    await waitFor(() => {
      expect(calls("POST", "/api/v1/projects")[0]?.body).toEqual({
        team: TEAM.id,
        name: "Client work",
        description: "",
        registry: CLIENT_REGISTRY.id,
      });
    });
  });

  it("isn't asked for when the team may use only one", async () => {
    signedIn();
    answer("GET /api/v1/teams", page([TEAM]));
    answer("GET /api/v1/registries", page([registry()]));
    await renderApp("/projects/new/");
    await waitFor(() => {
      expect(calls("GET", "/api/v1/registries")).toHaveLength(1);
    });
    expect(screen.queryByRole("combobox", { name: "Registry" })).not.toBeInTheDocument();
  });

  it("can be changed by an Org Admin while the project has no entities", async () => {
    const orgAdmin = { ...ME.can, manage_org: true };
    signedIn({ can: orgAdmin });
    const fresh = project({
      my_role: null,
      can: { ...KINASE.can, manage: false, change_registry: true },
    });
    onProjects(fresh);
    answer(
      "GET /api/v1/registries",
      page([registry(), registry({ ...CLIENT_REGISTRY, default: false })]),
    );
    answer(`PATCH /api/v1/projects/${KINASE.id}`, project({ registry: CLIENT_REGISTRY }));
    const { user } = await renderApp("/projects/kinase-atlas/settings/");
    const card = screen.getByRole("heading", { name: "Registry" }).closest("section");
    if (!card) throw new Error("no Registry card");
    expect(card).toHaveTextContent("Kinase Atlas is in the Main registry.");
    expect(within(card).getByRole("link", { name: "Manage registries" })).toHaveAttribute(
      "href",
      "/admin/registries/",
    );
    await user.selectOptions(
      await within(card).findByRole("combobox", { name: "Registry" }),
      CLIENT_REGISTRY.id,
    );
    await user.click(within(card).getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Kinase Atlas is in the Client X registry now.")).toBeVisible();
    expect(calls("PATCH")[0]?.body).toEqual({ registry: CLIENT_REGISTRY.id });
  });

  it("is fixed once the project has entities", async () => {
    signedIn({ can: { ...ME.can, manage_org: true } });
    onProjects(KINASE);
    answer(`GET /api/v1/projects/${KINASE.id}/dashboard`, {
      app: null,
      set_by: null,
      set_at: null,
    });
    answer(`GET /api/v1/projects/${KINASE.id}/dashboard/choices`, []);
    await renderApp("/projects/kinase-atlas/settings/");
    const card = screen.getByRole("heading", { name: "Registry" }).closest("section");
    if (!card) throw new Error("no Registry card");
    expect(card).toHaveTextContent(
      "It’s fixed now: a project’s registry can’t change once it has entities.",
    );
    expect(within(card).queryByRole("combobox")).not.toBeInTheDocument();
    expect(calls("GET", "/api/v1/registries")).toEqual([]);
  });
});

// --- Registrars --------------------------------------------------------------------

function entryOut(overrides: Partial<Schemas["EntryOut"]> = {}): Schemas["EntryOut"] {
  return {
    id: ENTRY_ID,
    hrid: "PRT-000001",
    type: "iki/protein.Protein",
    registry: MAIN_REGISTRY,
    revision: 3,
    created_at: "2026-03-14T09:00:00Z",
    values: { sequence: "MLEICLKLVG", molecular_weight: 52341.5, length: 10, uniprot: "P00519" },
    refs: {},
    files: {},
    superseded: null,
    records: [{ id: PROTEIN_ID, project: { id: KINASE.id, name: KINASE.name, slug: KINASE.slug } }],
    holders: null,
    add_to: [],

    can: { curate: false },
    ...overrides,
  };
}

function onEntry(current = entryOut()): void {
  signedIn();
  answer("GET /api/v1/entity-types", [PROTEIN_R]);
  onProjects(KINASE, OTHER);
  answer("GET /api/v1/entries/PRT-000001", current);
  answer(`GET /api/v1/entries/${ENTRY_ID}/revisions`, page([]));
}

describe("a registry's entries", () => {
  it("lists them for its registrars, with how many projects have each", async () => {
    signedIn();
    answer("GET /api/v1/entity-types", [PROTEIN_R]);
    answer(
      `GET /api/v1/registries/${MAIN_REGISTRY.id}`,
      registry({ can: { manage: false, curate: true, curate_lists: false } }),
    );
    const listed = (superseded: boolean) => ({
      id: superseded ? ENTRY_ID : NEW_ENTRY_ID,
      hrid: superseded ? "PRT-000001" : "PRT-000002",
      type: "iki/protein.Protein",
      revision: 1,
      created_at: "2026-03-14T09:00:00Z",
      holders: superseded ? 1 : 3,
      superseded,
    });
    answer(
      `GET /api/v1/registries/${MAIN_REGISTRY.id}/entries`,
      page([listed(false), listed(true)]),
    );
    const { user } = await renderApp(`/registries/${MAIN_REGISTRY.id}/entries/`);
    const table = await screen.findByRole("table");
    expect(
      within(table)
        .getAllByRole("row")
        .map((r) => r.textContent),
    ).toEqual([
      "IDTypeProjects with itRegistered",
      "PRT-000002Protein314 Mar 2026",
      "PRT-000001 SupersededProtein114 Mar 2026",
    ]);
    expect(within(table).getByRole("link", { name: "PRT-000002" })).toHaveAttribute(
      "href",
      "/entries/PRT-000002/",
    );
    await user.selectOptions(screen.getByRole("combobox", { name: "Show" }), "superseded");
    await user.type(screen.getByRole("textbox", { name: /^ID/ }), "prt-1");
    await waitFor(() => {
      const last = calls("GET", `/api/v1/registries/${MAIN_REGISTRY.id}/entries`).at(-1);
      expect(last?.query.get("q")).toBe("prt-1");
      expect(last?.query.get("superseded")).toBe("true");
    });
  });

  it("are only its registrars' to list", async () => {
    signedIn();
    answer("GET /api/v1/entity-types", [PROTEIN_R]);
    answer(`GET /api/v1/registries/${MAIN_REGISTRY.id}`, registry());
    await renderApp(`/registries/${MAIN_REGISTRY.id}/entries/`);
    expect(
      await screen.findByText(/Only the Main registry’s registrars see all its entries\./),
    ).toBeVisible();
    expect(calls("GET", `/api/v1/registries/${MAIN_REGISTRY.id}/entries`)).toEqual([]);
  });

  it("are a link away on a registrar's home page", async () => {
    signedIn();
    answer("GET /api/v1/projects", page([KINASE]));
    answer(
      "GET /api/v1/registries",
      page([registry({ can: { manage: false, curate: true, curate_lists: false } })]),
    );
    await renderApp("/");
    const curated = await screen.findByRole("region", { name: "Registries you curate" });
    expect(within(curated).getByRole("link", { name: "Main" })).toHaveAttribute(
      "href",
      `/registries/${MAIN_REGISTRY.id}/entries/`,
    );
  });
});

describe("an entry's page", () => {
  it("shows its values and the records of it the reader can read", async () => {
    onEntry();
    await renderApp("/entries/PRT-000001/");
    expect(screen.getByRole("heading", { level: 1, name: "PRT-000001" })).toBeVisible();
    const values = screen.getByRole("region", { name: "Values" });
    expect(
      within(values)
        .getAllByRole("term")
        .map((t) => t.textContent),
    ).toEqual([
      "SequenceIdentity",
      "Molecular weightDerived from its identity",
      "LengthDerived from its identity",
      "UniProt IDRegistry fact",
    ]);
    expect(within(values).queryByRole("button", { name: "Change facts" })).not.toBeInTheDocument();
    const records = screen.getByRole("region", { name: "Records you can read" });
    expect(within(records).getByRole("link", { name: "Kinase Atlas’s record" })).toHaveAttribute(
      "href",
      "/entities/PRT-000001/?project=kinase-atlas",
    );
    expect(screen.queryByRole("region", { name: "Supersede" })).not.toBeInTheDocument();
    // No project it could go to: nothing to offer.
    expect(
      screen.queryByRole("region", { name: "Add to another project" }),
    ).not.toBeInTheDocument();
  });

  it("adds it to another of the reader's projects in its registry", async () => {
    onEntry(entryOut({ add_to: [{ id: OTHER.id, name: OTHER.name, slug: OTHER.slug }] }));
    answer(
      `POST /api/v1/entries/${ENTRY_ID}/records`,
      respond(201, { ...record({ owner_project: OTHER }), reused: false, registered: false }),
    );
    answer("GET /api/v1/entities/PRT-000001", record({ owner_project: OTHER }));
    answer(`GET /api/v1/entities/${PROTEIN_ID}/provenance`, {});
    answer(`GET /api/v1/entities/${PROTEIN_ID}/projects`, []);
    answer("GET /api/v1/components", []);
    const { router, user } = await renderApp("/entries/PRT-000001/");
    const add = screen.getByRole("region", { name: "Add to another project" });
    // Only the projects the API says it may go to.
    expect(
      within(within(add).getByRole("combobox", { name: "Project" }))
        .getAllByRole("option")
        .map((o) => o.textContent),
    ).toEqual(["Antibody Lab"]);
    expect(within(add).getByRole("combobox", { name: "Project" })).toHaveValue(OTHER.id);
    await user.click(within(add).getByRole("button", { name: "Add to Antibody Lab" }));
    expect(
      await screen.findByText("Added PRT-000001 to Antibody Lab. This is its own record of it."),
    ).toBeVisible();
    expect(router.state.location.search).toEqual({ project: "antibody-lab" });
    expect(calls("POST")[0]?.body).toEqual({ project: OTHER.id, values: {}, reason: "" });
  });

  it("lets its registrars change its facts, with a reason, from the revision they read", async () => {
    onEntry(entryOut({ holders: 2, can: { curate: true } }));
    answer(
      `PATCH /api/v1/entries/${ENTRY_ID}`,
      entryOut({
        revision: 4,
        holders: 2,
        values: { ...entryOut().values, uniprot: "P00520" },
        can: { curate: true },
      }),
    );
    const { user } = await renderApp("/entries/PRT-000001/");
    expect(screen.getByRole("region", { name: "Details" })).toHaveTextContent(
      "Projects with it2As a registrar you see how many, not which.",
    );
    await user.click(screen.getByRole("button", { name: "Change facts" }));
    const form = screen.getByRole("form", { name: "Change facts" });
    const fact = within(form).getByRole("textbox", { name: /^UniProt ID/ });
    await user.clear(fact);
    await user.type(fact, "P00520");
    await user.click(within(form).getByRole("button", { name: "Save facts" }));
    expect(within(form).getByRole("textbox", { name: /^Reason/ })).toHaveAccessibleDescription(
      /Say why: every project with it sees it in the history\./,
    );
    expect(calls("PATCH")).toEqual([]);
    await user.type(within(form).getByRole("textbox", { name: /^Reason/ }), "UniProt merged it.");
    await user.click(within(form).getByRole("button", { name: "Save facts" }));
    expect(await screen.findByText("Saved revision 4 of PRT-000001.")).toBeVisible();
    const sent = calls("PATCH")[0];
    expect(sent?.body).toEqual({ values: { uniprot: "P00520" }, reason: "UniProt merged it." });
    expect(sent?.headers.get("If-Match")).toBe('"3"');
  });

  it("says when someone else changed it first, and saves nothing", async () => {
    onEntry(entryOut({ can: { curate: true } }));
    answer(
      `PATCH /api/v1/entries/${ENTRY_ID}`,
      problem(412, "stale", "It changed since you read it."),
    );
    const { user } = await renderApp("/entries/PRT-000001/");
    await user.click(screen.getByRole("button", { name: "Change facts" }));
    const form = screen.getByRole("form", { name: "Change facts" });
    await user.type(within(form).getByRole("textbox", { name: /^UniProt ID/ }), "0");
    await user.type(within(form).getByRole("textbox", { name: /^Reason/ }), "Typo");
    await user.click(within(form).getByRole("button", { name: "Save facts" }));
    expect(await within(form).findByRole("alert")).toHaveTextContent(
      "Someone changed PRT-000001 while you were editing, so nothing was saved.",
    );
  });

  it("lets its registrars supersede it, for good, after saying what that does", async () => {
    onEntry(entryOut({ holders: 2, can: { curate: true } }));
    const supersededEntry = entryOut({
      holders: 2,
      superseded: {
        by: {
          hrid: "PRT-000009",
          title: "PRT-000009",
          type: "iki/protein.Protein",
          archived: false,
          purged: false,
        },
        by_id: NEW_ENTRY_ID,
        at: "2026-11-17T09:00:00Z",
        reason: "Registered twice.",
      },
      can: { curate: false },
    });
    answer(`POST /api/v1/entries/${ENTRY_ID}/supersede`, supersededEntry);

    const { user } = await renderApp("/entries/PRT-000001/");
    const card = screen.getByRole("region", { name: "Supersede" });
    await user.click(within(card).getByRole("button", { name: "Supersede PRT-000001…" }));
    expect(within(card).getByRole("textbox", { name: "Replaced by" })).toHaveAccessibleDescription(
      /Enter the ID of the entry that replaces it\./,
    );
    await user.type(within(card).getByRole("textbox", { name: "Replaced by" }), "prt-000009");
    await user.type(within(card).getByRole("textbox", { name: "Reason" }), "Registered twice.");
    await user.click(within(card).getByRole("button", { name: "Supersede PRT-000001…" }));
    const dialog = screen.getByRole("alertdialog", {
      name: "Supersede PRT-000001 with PRT-000009?",
    });
    expect(dialog).toHaveTextContent("This can’t be undone.");
    expect(calls("POST")).toEqual([]);
    answer("GET /api/v1/entries/PRT-000001", supersededEntry);
    await user.click(within(dialog).getByRole("button", { name: "Supersede PRT-000001" }));
    expect(
      await screen.findByText("Superseded PRT-000001. Every project with it has been told."),
    ).toBeVisible();
    expect(calls("POST")[0]?.body).toEqual({
      replacement: "PRT-000009",
      reason: "Registered twice.",
    });
    expect(screen.getByRole("note")).toHaveTextContent("Superseded by PRT-000009 on 17 Nov 2026");
  });
});
