/** Saved views (core.View): saving what a viewer shows, and opening it on its revisions. */

import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { entity, field, PROTEIN, PROTEIN_ID, renderApp, withTypes } from "./entity-fixtures";
import { answer, calls } from "./test-server";

const PLASMID = {
  ...PROTEIN,
  name: "dna.DNASequence",
  plugin: "dna",
  label: "DNA sequence",
  label_plural: "DNA sequences",
  hrid_prefix: "DNA",
  fields: [
    field({ name: "bases", label: "Bases", type: "string" }),
    field({ name: "features", label: "Features", type: "json" }),
  ],
  viewers: [
    {
      viewer: "sequence" as const,
      label: "Sequence",
      sequence: "bases",
      alphabet: "dna" as const,
      features: "features",
      tracks: [],
    },
  ],
};

const SEQUENCED = {
  ...PROTEIN,
  viewers: [
    {
      viewer: "sequence" as const,
      label: "Sequence",
      sequence: "sequence",
      alphabet: "protein" as const,
      features: null,
      tracks: [],
    },
  ],
};

const VIEW = {
  ...PROTEIN,
  name: "core.View",
  plugin: "core",
  label: "View",
  label_plural: "Views",
  hrid_prefix: "VIEW",
  fields: [
    field({ name: "name", label: "Name", type: "string", plugin: "core" }),
    field({ name: "targets", label: "Entities", type: "list", plugin: "core", items: "ref" }),
    field({ name: "target_revisions", label: "Revisions", type: "list", plugin: "core" }),
    field({ name: "viewers", label: "Viewers", type: "list", plugin: "core" }),
    field({ name: "state", label: "State", type: "json", plugin: "core" }),
  ],
  viewers: [],
};

const PLASMID_ID = "0190f5c2-0000-7000-8000-00000000d0a1";
const VIEW_ID = "0190f5c2-0000-7000-8000-00000000f1e1";
const TET = { type: "CDS", name: "tet", start: 5, end: 130, strand: "forward" };
const BLA = { type: "CDS", name: "bla", start: 140, end: 160, strand: "reverse" };

function plasmid(revision: number, features: unknown[]) {
  return entity({
    id: PLASMID_ID,
    hrid: "DNA-000001",
    type: "dna.DNASequence",
    revision,
    values: { name: "pTest", bases: "ACGT".repeat(40), features },
  });
}

const PLASMID_REF = {
  hrid: "DNA-000001",
  title: "pTest",
  type: "dna.DNASequence",
  archived: false,
  purged: false,
};
const PROTEIN_REF = {
  hrid: "PRT-000001",
  title: "ABL1 kinase",
  type: "protein.Protein",
  archived: false,
  purged: false,
};

function onTypes() {
  withTypes();
  answer("GET /api/v1/entity-types", [SEQUENCED, PLASMID, VIEW]);
}

describe("saving a view", () => {
  it("saves what the viewer shows, on the revision it shows", async () => {
    onTypes();
    answer("GET /api/v1/entities/DNA-000001", plasmid(3, [TET, BLA]));
    answer("POST /api/v1/entities/search", { items: [], next_cursor: null });
    answer("POST /api/v1/entities", { id: "v1", hrid: "VIEW-000001", revision: 1 });
    answer("GET /api/v1/entities/VIEW-000001", entity({ hrid: "VIEW-000001", type: "core.View" }));
    const { user } = await renderApp("/entities/DNA-000001/viewers/sequence/");
    expect(await screen.findByText(/None yet\. A saved view keeps/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Show bla" }));
    await user.click(screen.getByRole("button", { name: "Save view" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Save this view" });
    expect(dialog).toHaveTextContent("on revision 3 of DNA-000001");
    expect(within(dialog).getByRole("textbox", { name: "Name" })).toHaveValue("Sequence of pTest");
    await user.click(within(dialog).getByRole("button", { name: "Save view" }));
    await waitFor(() => {
      expect(calls("POST", "/api/v1/entities")).toHaveLength(1);
    });
    const [saved] = calls("POST", "/api/v1/entities");
    expect(saved?.body).toMatchObject({
      type: "core.View",
      values: {
        name: "Sequence of pTest",
        targets: [PLASMID_ID],
        target_revisions: [3],
        viewers: ["sequence"],
        state: {
          version: 2,
          targets: [{ feature: { type: "CDS", name: "bla", start: 140, end: 160 } }],
        },
      },
    });
  });

  it("adds other entities to it, with the picker references use, each at its latest revision", async () => {
    onTypes();
    answer("GET /api/v1/entities/DNA-000001", plasmid(3, [TET]));
    answer("POST /api/v1/entities/search", { items: [], next_cursor: null });
    answer("GET /api/v1/entities", {
      items: [entity({ hrid: "PRT-000001" })],
      next_cursor: null,
    });
    answer("GET /api/v1/entities/PRT-000001", entity({ hrid: "PRT-000001", revision: 4 }));
    answer("POST /api/v1/entities", { id: "v1", hrid: "VIEW-000001", revision: 1 });
    answer("GET /api/v1/entities/VIEW-000001", entity({ hrid: "VIEW-000001", type: "core.View" }));
    const { user } = await renderApp("/entities/DNA-000001/viewers/sequence/");
    await screen.findByText(/None yet\. A saved view keeps/);
    await user.click(screen.getByRole("button", { name: "Save view" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Save this view" });
    await user.click(within(dialog).getByRole("combobox", { name: /^Add another entity/ }));
    await user.click(await screen.findByRole("option", { name: /ABL1 kinase/ }));
    expect(await within(dialog).findByText(/at its latest revision, 4/)).toBeInTheDocument();
    expect(within(dialog).getByText(/in the sequence viewer$/)).toBeInTheDocument();
    await user.type(
      within(dialog).getByRole("textbox", { name: /Label for PRT-000001/ }),
      "kinase",
    );
    await user.click(within(dialog).getByRole("button", { name: "Save view" }));
    await waitFor(() => {
      expect(calls("POST", "/api/v1/entities")).toHaveLength(1);
    });
    const [saved] = calls("POST", "/api/v1/entities");
    expect(saved?.body).toMatchObject({
      values: {
        targets: [PLASMID_ID, PROTEIN_ID],
        target_revisions: [3, 4],
        viewers: ["sequence", "sequence"],
        state: { version: 2, targets: [{}, { label: "kinase" }] },
      },
    });
  });

  it("lists every view it's in, saying what else a view shows", async () => {
    onTypes();
    answer("GET /api/v1/entities/DNA-000001", plasmid(3, [TET]));
    answer("POST /api/v1/entities/search", {
      items: [
        entity({
          hrid: "VIEW-000002",
          type: "core.View",
          values: {
            name: "tet for the report",
            targets: [PLASMID_ID],
            target_revisions: [2],
            viewers: ["sequence"],
          },
        }),
        entity({
          hrid: "VIEW-000003",
          type: "core.View",
          values: {
            name: "pTest with its protein",
            targets: [PROTEIN_ID, PLASMID_ID],
            target_revisions: [1, 3],
            viewers: ["sequence", "sequence"],
          },
          refs: { [PROTEIN_ID]: PROTEIN_REF, [PLASMID_ID]: PLASMID_REF },
        }),
        // It's in this one, but in another viewer: not a view of it here.
        entity({
          hrid: "VIEW-000004",
          type: "core.View",
          values: {
            name: "Elsewhere",
            targets: [PLASMID_ID, PROTEIN_ID],
            target_revisions: [1, 1],
            viewers: ["alignment", "sequence"],
          },
        }),
      ],
      next_cursor: null,
    });
    await renderApp("/entities/DNA-000001/viewers/sequence/");
    expect(await screen.findByRole("link", { name: "tet for the report" })).toHaveAttribute(
      "href",
      "/entities/VIEW-000002/",
    );
    expect(screen.getByText(/revision 2/)).toBeInTheDocument();
    const both = screen.getByRole("link", { name: "pTest with its protein" }).closest("li");
    expect(both).toHaveTextContent("revision 3 · with PRT-000001");
    expect(screen.queryByRole("link", { name: "Elsewhere" })).not.toBeInTheDocument();
    const [search] = calls("POST", "/api/v1/entities/search");
    expect(search?.body).toMatchObject({
      type: "core.View",
      where: {
        all: [
          { field: "targets", op: "has", value: PLASMID_ID },
          { field: "viewers", op: "has", value: "sequence" },
        ],
      },
    });
  });
});

describe("a saved view's page", () => {
  function onView(
    state: unknown = { version: 1, feature: { type: "CDS", name: "tet", start: 5, end: 130 } },
  ) {
    onTypes();
    answer(
      "GET /api/v1/entities/VIEW-000001",
      entity({
        id: VIEW_ID,
        hrid: "VIEW-000001",
        type: "core.View",
        revision: 1,
        values: {
          name: "tet",
          targets: [PLASMID_ID],
          target_revisions: [2],
          viewers: ["sequence"],
          state,
        },
        refs: { [PLASMID_ID]: PLASMID_REF },
      }),
    );
    // Revision 2 had only tet; now there's bla too.
    answer("GET /api/v1/entities/DNA-000001/revisions/2", {
      number: 2,
      kind: "edited",
      values: { name: "pTest", bases: "ACGT".repeat(40), features: [TET] },
      changes: {},
      refs: {},
      files: {},
    });
    answer("GET /api/v1/entities/DNA-000001", plasmid(5, [TET, BLA]));
    answer(`PATCH /api/v1/entities/${VIEW_ID}`, entity({ hrid: "VIEW-000001", revision: 2 }));
  }

  it("opens on its revision, showing what it saved, and says the entity has changed since", async () => {
    // Saved before views could show several entities: its state is version 1.
    onView();
    await renderApp("/entities/VIEW-000001/");
    expect(
      await screen.findByText("Revision 2 of DNA-000001, as the view saved it."),
    ).toBeVisible();
    expect(await screen.findByText("It’s changed since: it’s at revision 5 now.")).toBeVisible();
    expect(await screen.findByText("160 bp · 1 feature")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Hide tet" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("shows the latest revision, and saves the view on it", async () => {
    onView({
      version: 2,
      targets: [{ feature: { type: "CDS", name: "tet", start: 5, end: 130 } }],
    });
    const { user } = await renderApp("/entities/VIEW-000001/");
    await screen.findByText("160 bp · 1 feature");
    await user.click(screen.getByRole("switch", { name: "Show the latest revision" }));
    expect(await screen.findByText("160 bp · 2 features")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Hide tet" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await user.click(screen.getByRole("button", { name: "Save on the latest revision" }));
    await waitFor(() => {
      expect(calls("PATCH", `/api/v1/entities/${VIEW_ID}`)).toHaveLength(1);
    });
    const [saved] = calls("PATCH", `/api/v1/entities/${VIEW_ID}`);
    expect(saved?.headers.get("If-Match")).toBe('"1"');
    expect(saved?.body).toMatchObject({
      values: {
        target_revisions: [5],
        state: {
          version: 2,
          targets: [{ feature: { type: "CDS", name: "tet", start: 5, end: 130 } }],
        },
      },
    });
  });

  it("shows each of several entities in its viewer, at its revision, and saves them all", async () => {
    onTypes();
    answer(
      "GET /api/v1/entities/VIEW-000001",
      entity({
        id: VIEW_ID,
        hrid: "VIEW-000001",
        type: "core.View",
        revision: 3,
        values: {
          name: "pTest and its protein",
          targets: [PLASMID_ID, PROTEIN_ID],
          target_revisions: [2, 2],
          viewers: ["sequence", "sequence"],
          state: {
            version: 2,
            targets: [
              { feature: { type: "CDS", name: "tet", start: 5, end: 130 } },
              { label: "kinase" },
            ],
          },
        },
        refs: { [PLASMID_ID]: PLASMID_REF, [PROTEIN_ID]: PROTEIN_REF },
      }),
    );
    answer("GET /api/v1/entities/DNA-000001/revisions/2", {
      number: 2,
      kind: "edited",
      values: { name: "pTest", bases: "ACGT".repeat(40), features: [TET] },
      changes: {},
      refs: {},
      files: {},
    });
    answer("GET /api/v1/entities/DNA-000001", plasmid(5, [TET, BLA]));
    answer("GET /api/v1/entities/PRT-000001/revisions/2", {
      number: 2,
      kind: "edited",
      values: { name: "ABL1 kinase", sequence: "MLEICLKLVG" },
      changes: {},
      refs: {},
      files: {},
    });
    answer("GET /api/v1/entities/PRT-000001", entity({ hrid: "PRT-000001", revision: 2 }));
    answer(`PATCH /api/v1/entities/${VIEW_ID}`, entity({ hrid: "VIEW-000001", revision: 4 }));
    const { user } = await renderApp("/entities/VIEW-000001/");
    const shows = await screen.findByRole("list", { name: "What it shows" });
    const [first, second] = within(shows).getAllByRole("listitem");
    expect(first).toHaveTextContent(
      "pTest DNA-000001 in the sequence viewer · Revision 2 of DNA-000001, as the view saved it. It’s changed since: it’s at revision 5 now.",
    );
    expect(second).toHaveTextContent(
      "kinase PRT-000001 in the sequence viewer · Revision 2 of PRT-000001, as the view saved it.",
    );
    expect(await screen.findByRole("heading", { name: "Sequence of kinase" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Sequence of pTest" })).toBeVisible();
    expect(await screen.findByText("10 residues")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save the view" }));
    await waitFor(() => {
      expect(calls("PATCH", `/api/v1/entities/${VIEW_ID}`)).toHaveLength(1);
    });
    const [saved] = calls("PATCH", `/api/v1/entities/${VIEW_ID}`);
    expect(saved?.body).toMatchObject({
      values: {
        target_revisions: [2, 2],
        state: {
          version: 2,
          targets: [
            { feature: { type: "CDS", name: "tet", start: 5, end: 130 } },
            { label: "kinase" },
          ],
        },
      },
    });
  });
});
