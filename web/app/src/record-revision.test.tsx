/**
 * A record, or a registry entry, as it was at one of its revisions (ADR 0189): the page a
 * witness opens from the evidence they sign. It's read only, says it isn't the record as
 * it is now, and shows only what the revision API gives the person.
 */

import type { Schemas } from "@ikisci/sdk";
import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  ALICE,
  entity,
  field,
  KINASE,
  PROTEIN,
  PROTEIN_ID,
  renderApp,
  revision,
  withTypes,
} from "./entity-fixtures";
import { revisionNumber } from "./pages/record-revision";
import { answer, calls, MAIN_REGISTRY, page, problem, signedIn } from "./test-server";

const ADA = { ...ALICE, id: "00000000-0000-7000-8000-0000000000a2", name: "Ada Lovelace" };
const ENTRY_ID = "00000000-0000-7000-8000-00000000e0e1";

/** Revision 2 of PRT-000001: renamed, with a field its type has since dropped. */
const AT_2: Schemas["RevisionDetailOut"] = {
  ...revision({
    number: 2,
    kind: "edited",
    changed_fields: ["name"],
    user: ADA,
    reason: "Renamed to its HGNC symbol.",
    created_at: "2026-11-19T16:40:00Z",
  }),
  values: {
    name: "ABL1",
    sequence: "MLEICLKLVG",
    molecular_weight: null,
    lims_code: "L-17",
  },
  changes: { name: { before: "ABL1 kinase", after: "ABL1" } },
  refs: {},
  files: {},
  people: {},
};

function onRecord(current: Partial<Schemas["EntityOut"]> = {}): void {
  withTypes();
  answer("GET /api/v1/entities/PRT-000001", entity({ revision: 3, ...current }));
  answer("GET /api/v1/entities/PRT-000001/revisions/2", AT_2);
}

describe("a record at a revision", () => {
  it("shows it as it was, and says plainly it isn't the record as it is now", async () => {
    onRecord();
    await renderApp("/entities/PRT-000001/revisions/2/");
    expect(
      screen.getByRole("heading", { level: 1, name: "Revision 2 of PRT-000001" }),
    ).toBeVisible();
    const note = screen.getByRole("note", { name: "A past revision" });
    expect(await within(note).findByText(/saved on/)).toBeVisible();
    expect(note).toHaveTextContent(
      "You’re reading revision 2 of PRT-000001 as it was saved on 19 Nov 2026, 16:40 UTC. " +
        "Nothing on this page can be changed. It’s at revision 3 now: 1 revision later.",
    );
    expect(
      within(note).getByRole("link", { name: "Open PRT-000001 as it is now" }),
    ).toHaveAttribute("href", "/entities/PRT-000001/");
    expect(within(note).getByRole("link", { name: "Revision 2 as JSON" })).toHaveAttribute(
      "href",
      `/api/v1/entities/${PROTEIN_ID}/revisions/2`,
    );
    // The record's tabs and actions are the record as it is now: none of them is here.
    expect(screen.queryByRole("navigation", { name: "Record" })).toBeNull();
    expect(screen.queryByRole("link", { name: "Edit" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Edit" })).toBeNull();

    const about = screen.getByRole("region", { name: "Revision 2: Edited by Ada Lovelace" });
    expect(about).toHaveTextContent("19 Nov 2026, 16:40 UTC · in the web app · Name");
    expect(about).toHaveTextContent("“Renamed to its HGNC symbol.”");
    const values = screen.getByRole("region", { name: "Values at revision 2" });
    expect(
      within(values)
        .getAllByRole("term")
        .map((t) => t.textContent),
    ).toEqual(["Name", "Sequence", "lims_codeNo longer one of its type’s fields"]);
    expect(values).toHaveTextContent("ABL1");
    expect(values).not.toHaveTextContent("ABL1 kinase");
    expect(values).toHaveTextContent("Not set: Molecular weight.");
    const changed = screen.getByRole("region", { name: "What revision 2 changed" });
    expect(within(changed).getByRole("table")).toHaveTextContent(
      "FieldBeforeAfterNameABL1 kinaseABL1",
    );
    // Only what the revision API gives the person, read once.
    expect(calls("GET", "/api/v1/entities/PRT-000001/revisions/2")).toHaveLength(1);
  });

  it("at its latest revision says so", async () => {
    onRecord({ revision: 2 });
    await renderApp("/entities/PRT-000001/revisions/2/");
    const note = screen.getByRole("note", { name: "A past revision" });
    expect(await within(note).findByText(/saved on/)).toBeVisible();
    expect(note).toHaveTextContent(
      "You’re reading revision 2 of PRT-000001, its latest, as it was saved on 19 Nov 2026, " +
        "16:40 UTC. Nothing on this page can be changed.",
    );
    expect(note).not.toHaveTextContent("later");
  });

  it("is only a whole number from 1, and one it has", async () => {
    expect(["1", "42", "999999999999999"].map(revisionNumber)).toEqual([1, 42, 999999999999999]);
    for (const bad of ["0", "-1", "01", "2.5", "1e3", "abc", "", "9999999999999999"]) {
      expect(revisionNumber(bad)).toBeNull();
    }
    onRecord();
    await renderApp("/entities/PRT-000001/revisions/abc/");
    expect(screen.getByRole("heading", { level: 1, name: "Not found" })).toBeVisible();
    expect(calls("GET", "/api/v1/entities/PRT-000001/revisions/abc")).toEqual([]);

    answer(
      "GET /api/v1/entities/PRT-000001/revisions/9",
      problem(404, "not_found", "PRT-000001 has no revision 9."),
    );
    await renderApp("/entities/PRT-000001/revisions/9/");
    expect(await screen.findByText("PRT-000001 has no revision 9.")).toBeVisible();
    expect(screen.queryByRole("region", { name: "Values at revision 9" })).toBeNull();
  });

  it("of a record the person can't read shows nothing of it", async () => {
    withTypes();
    answer(
      "GET /api/v1/entities/PRT-000001",
      problem(404, "not_found", "No such entity, or you can’t read it."),
    );
    answer("GET /api/v1/entities/PRT-000001/revisions/2", AT_2);
    await renderApp("/entities/PRT-000001/revisions/2/");
    expect(screen.getByRole("heading", { level: 1, name: "Not found" })).toBeVisible();
    expect(screen.queryByText("ABL1")).toBeNull();
    expect(calls("GET", "/api/v1/entities/PRT-000001/revisions/2")).toEqual([]);
  });

  it("of a project's record of a registry entry is that record's, by its ID", async () => {
    answer("GET /api/v1/projects", page([KINASE]));
    onRecord({
      entry: {
        id: ENTRY_ID,
        hrid: "PRT-000001",
        registry: MAIN_REGISTRY,
        revision: 4,
        superseded: null,
      },
    });
    answer(`GET /api/v1/entities/${PROTEIN_ID}/revisions/2`, AT_2);
    await renderApp("/entities/PRT-000001/revisions/2/?project=kinase-atlas");
    const note = screen.getByRole("note", { name: "A past revision" });
    expect(
      within(note).getByRole("link", { name: "Open PRT-000001 as it is now" }),
    ).toHaveAttribute("href", "/entities/PRT-000001/?project=kinase-atlas");
    const values = await screen.findByRole("region", { name: "Values at revision 2" });
    expect(values).toHaveTextContent(
      "These are Kinase Atlas’s record’s values. The values every project with it shares are " +
        "registry entry PRT-000001’s, in the entry’s own revisions.",
    );
    expect(within(values).getByRole("link", { name: "the entry’s own revisions" })).toHaveAttribute(
      "href",
      "/entries/PRT-000001/",
    );
    expect(calls("GET", `/api/v1/entities/${PROTEIN_ID}/revisions/2`)).toHaveLength(1);
  });
});

/** A protein whose registry entry has a curated fact. */
const PROTEIN_R: Schemas["EntityTypeOut"] = {
  ...PROTEIN,
  fields: [
    ...PROTEIN.fields,
    field({ name: "uniprot", label: "UniProt ID", type: "string", registry: "fact" }),
  ],
};

function onEntry(): void {
  signedIn();
  answer("GET /api/v1/entity-types", page([PROTEIN_R]));
  answer("GET /api/v1/components", page([]));
  answer(`GET /api/v1/entries/${ENTRY_ID}`, {
    id: ENTRY_ID,
    hrid: "PRT-000001",
    type: "iki/protein.Protein",
    registry: MAIN_REGISTRY,
    revision: 3,
    created_at: "2026-03-14T09:00:00Z",
    values: { sequence: "MLEICLKLVG", molecular_weight: 52341.5, uniprot: "P00519" },
    refs: {},
    files: {},
    superseded: null,
    records: [],
    holders: null,
    add_to: [],
    can: { curate: false },
  } satisfies Schemas["EntryOut"]);
}

describe("a registry entry at a revision", () => {
  it("shows its values then, and of a change through a project the person can't read, only what it set", async () => {
    onEntry();
    answer(`GET /api/v1/entries/${ENTRY_ID}/revisions/2`, {
      ...revision({
        number: 2,
        kind: "curated",
        entry: true,
        changed_fields: ["uniprot"],
        user: null,
        created_at: null,
        via: "",
      }),
      values: { sequence: "MLEICLKLVG", molecular_weight: 52341.5, uniprot: "P00518" },
      changes: { uniprot: { before: null, after: "P00518" } },
      refs: {},
      files: {},
      people: {},
    });
    await renderApp(`/entries/${ENTRY_ID}/revisions/2/`);
    expect(
      screen.getByRole("heading", { level: 1, name: "Revision 2 of PRT-000001" }),
    ).toBeVisible();
    const note = screen.getByRole("note", { name: "A past revision" });
    expect(note).toHaveTextContent(
      "You’re reading revision 2 of PRT-000001 as it was. Nothing on this page can be changed. " +
        "It’s at revision 3 now: 1 revision later.",
    );
    expect(
      within(note).getByRole("link", { name: "Open PRT-000001 as it is now" }),
    ).toHaveAttribute("href", "/entries/PRT-000001/");
    expect(within(note).getByRole("link", { name: "Revision 2 as JSON" })).toHaveAttribute(
      "href",
      `/api/v1/entries/${ENTRY_ID}/revisions/2`,
    );
    const about = await screen.findByRole("region", { name: "Revision 2: Set UniProt ID" });
    expect(about).toHaveTextContent("Through a project you can’t read · UniProt ID");
    expect(about).not.toHaveTextContent(/Ada|UTC/);
    const values = screen.getByRole("region", { name: "Values at revision 2" });
    expect(values).toHaveTextContent("P00518");
    expect(values).not.toHaveTextContent("P00519");
    expect(
      within(screen.getByRole("region", { name: "What revision 2 changed" })).getByRole("table"),
    ).toHaveTextContent("UniProt IDNot setP00518");
  });

  it("is a link away from each revision on the entry's page", async () => {
    onEntry();
    answer(
      `GET /api/v1/entries/${ENTRY_ID}/revisions`,
      page([revision({ number: 2, kind: "curated", entry: true, changed_fields: ["uniprot"] })]),
    );
    await renderApp(`/entries/${ENTRY_ID}/`);
    expect(
      await screen.findByRole("link", { name: "Open entry revision 2 as it was" }),
    ).toHaveAttribute("href", `/entries/${ENTRY_ID}/revisions/2/`);
  });
});
