/**
 * A record's page as ADR 0176 lays it out: its header's actions and tabs, its rail, and
 * its Audit trail tab, narrowed by person and field, by field, and downloaded whole.
 */

import type { Schemas } from "@ikisci/sdk";
import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ALICE, entity, field, PROTEIN, renderApp, revision, withTypes } from "./entity-fixtures";
import { answer, calls, page } from "./test-server";

const ADA = { ...ALICE, id: "00000000-0000-7000-8000-0000000000a2", name: "Ada Lovelace" };

const PLASMID: Schemas["EntityTypeOut"] = {
  ...PROTEIN,
  name: "iki/dna.Plasmid",
  plugin: "iki/dna",
  label: "Plasmid",
  label_plural: "Plasmids",
  hrid_prefix: "PLS",
  identity: [],
  fields: [
    field({ name: "name", label: "Name", type: "string" }),
    field({ name: "bases", label: "Bases", type: "string" }),
    field({ name: "topology", label: "Topology", type: "string" }),
    field({ name: "resistance", label: "Resistance", type: "string" }),
    field({ name: "features", label: "Features", type: "json" }),
  ],
  viewers: [
    {
      viewer: "sequence" as const,
      label: "Sequence",
      sequence: "bases",
      alphabet: "dna" as const,
      strands: null,
      topology: "topology",
      features: "features",
      tracks: [],
    },
  ],
};

const PLS_ID = "00000000-0000-7000-8000-00000000e0a1";

const APPROVAL: Schemas["SignatureOut"] = {
  id: "00000000-0000-7000-8000-0000000051a1",
  manifestation: "Approved by Ada Lovelace on 2026-11-19 at 16:00:00 UTC. Meaning: Approval.",
  record: PLS_ID,
  hrid: "PLS-000001",
  revision: 1,
  entry_revision: null,
  change: {
    field: "topology",
    field_label: "Topology",
    from: "linear",
    from_label: "Linear",
    to: "circular",
    to_label: "Circular",
  },
  index: 0,
  of: 1,
  meaning: "approval",
  meaning_label: "Approval",
  label: "Approval",
  statement: "I approve this record.",
  roles: [],
  signer: { id: ADA.id, name: ADA.name },
  signer_name: ADA.name,
  signer_email: ADA.email,
  signed_at: "2026-11-19T16:00:00Z",
  reason: "Map matches GenBank L09137.2.",
  authentication: {
    protocol: "oidc",
    issuer: "https://idp.example.com",
    subject: "uid-2",
    authenticated_at: "2026-11-19T15:59:50Z",
    methods: ["pwd", "otp"],
    context: "",
  },
  deployment: "iki.example.com",
  key: "ed25519:abc",
  status: "carried",
  status_detail: "",
  carried_by: 2,
  document: "{}",
  signature: "x",
};

function onPlasmid(): void {
  withTypes();
  answer("GET /api/v1/entity-types", [PROTEIN, PLASMID]);
  answer(
    "GET /api/v1/entities/PLS-000001",
    entity({
      id: PLS_ID,
      hrid: "PLS-000001",
      type: "iki/dna.Plasmid",
      revision: 3,
      values: {
        name: "pUC19c",
        bases: "ACGT".repeat(100),
        topology: "circular",
        resistance: "Ampicillin",
        features: [
          { type: "promoter", name: "lac promoter", start: 10, end: 90, strand: "reverse" },
          { type: "CDS", name: "AmpR", start: 200, end: 380, strand: "reverse" },
        ],
      },
    }),
  );
  answer(`GET /api/v1/entities/PLS-000001/provenance`, {
    name: revision({ number: 1, changed_fields: ["name", "bases"] }),
    bases: revision({ number: 1, changed_fields: ["name", "bases"] }),
    resistance: revision({
      number: 3,
      kind: "edited",
      changed_fields: ["resistance"],
      user: ADA,
      reason: "Confirmed from the GenBank record.",
      created_at: "2026-11-19T16:40:00Z",
    }),
  });
  answer("GET /api/v1/entities/PLS-000001/contributors", [ADA, ALICE]);
  answer("GET /api/v1/entities/PLS-000001/revisions", ({ query }: { query: URLSearchParams }) => {
    const all = [
      revision({
        number: 3,
        kind: "edited",
        changed_fields: ["resistance"],
        user: ADA,
        reason: "Confirmed from the GenBank record.",
        created_at: "2026-11-19T16:40:00Z",
      }),
      revision({
        number: 2,
        kind: "state_changed",
        changed_fields: ["topology"],
        state: {
          field: { name: "topology", label: "Topology" },
          from: { name: "linear", label: "Linear" },
          to: { name: "circular", label: "Circular" },
          signatures: [
            {
              id: APPROVAL.id,
              manifestation:
                "Approved by Ada Lovelace on 2026-11-19 at 16:00:00 UTC. Meaning: Approval.",
              present: true,
            },
          ],
        },
        user: ADA,
        created_at: "2026-11-19T16:00:00Z",
      }),
      revision({ number: 1, changed_fields: ["name", "bases"] }),
    ];
    const person = query.get("person");
    const name = query.get("field");
    return page(
      all.filter(
        (r) => (!person || r.user?.id === person) && (!name || r.changed_fields.includes(name)),
      ),
    );
  });
  answer("GET /api/v1/entities/PLS-000001/signatures", { items: [APPROVAL], missing: [] });
  answer(`GET /api/v1/entities/PLS-000001/graph`, {
    root: PLS_ID,
    entities: [
      { id: PLS_ID, hrid: "PLS-000001", title: "pUC19c", type: "iki/dna.Plasmid", at: 0 },
      { id: "e-up", hrid: "DNA-000007", title: "EcoRI fragment", type: "iki/dna.Sequence", at: -1 },
      { id: "e-down", hrid: "PRM-000002", title: "M13 forward", type: "iki/dna.Primer", at: 1 },
    ],
    runs: [],
    edges: [],
    hidden: 0,
    earlier: false,
    later: false,
    capped: false,
  });
}

describe("a record's header", () => {
  it("has its actions, its ⋯ menu, and tabs that count its links and revisions", async () => {
    onPlasmid();
    const { user } = await renderApp("/entities/PLS-000001/");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(/^pUC19c$/);
    expect(screen.getByRole("link", { name: "Edit" })).toHaveAttribute(
      "href",
      "/entities/PLS-000001/edit/",
    );
    expect(screen.getByRole("link", { name: "Run action" })).toBeInTheDocument();
    const tabs = screen.getByRole("navigation", { name: "Record" });
    expect(
      await within(tabs).findByRole("link", { name: /^Lineage,\s*2\s*linked records$/ }),
    ).toBeInTheDocument();
    expect(
      within(tabs).getByRole("link", { name: /^Audit trail,\s*3\s*revisions$/ }),
    ).toBeInTheDocument();
    // Signed, as its state was set by signing (ADR 0126).
    const title = screen.getByRole("heading", { level: 1 }).closest<HTMLElement>(".record-title");
    expect(title).not.toBeNull();
    expect(await within(title ?? document.body).findByText("Signed")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "More actions for PLS-000001" }));
    const menu = await screen.findByRole("menu");
    expect(
      within(menu)
        .getAllByRole("menuitem")
        .map((m) => m.textContent),
    ).toEqual(["Download audit trail (CSV)", "Archive…"]);
    expect(within(menu).getByRole("menuitem", { name: /audit trail/ })).toHaveAttribute(
      "href",
      `/api/v1/entities/${PLS_ID}/revisions.csv`,
    );
  });

  it("has a rail with its map, its details and what it's linked to", async () => {
    onPlasmid();
    await renderApp("/entities/PLS-000001/");
    const rail = screen.getByRole("complementary", { name: "About this record" });
    expect(within(rail).getByRole("img")).toHaveAccessibleName(
      "Circular map of pUC19c, 400 bp: lac promoter, AmpR",
    );
    expect(within(rail).getByRole("link", { name: "Open the sequence" })).toHaveAttribute(
      "href",
      "/entities/PLS-000001/viewers/sequence/",
    );
    const details = within(rail).getByRole("region", { name: "Details" });
    expect(await within(details).findByText(/Approval by Ada Lovelace/)).toBeInTheDocument();
    const lineage = within(rail).getByRole("region", { name: "Lineage" });
    expect(await within(lineage).findByRole("link", { name: /DNA-000007/ })).toHaveAttribute(
      "href",
      "/entities/DNA-000007/",
    );
    expect(within(lineage).getByText("Derived from")).toBeInTheDocument();
    expect(within(lineage).getByText("Used in")).toBeInTheDocument();
    // Its features are drawn, in the map and the viewer: its values count them.
    const values = screen.getByRole("region", { name: "Values" });
    const features = within(values).getByText(/^2 features\./);
    expect(within(features).getByRole("link", { name: "Open in Sequence" })).toHaveAttribute(
      "href",
      "/entities/PLS-000001/viewers/sequence/",
    );
    expect(values).not.toHaveTextContent("lac promoter");
    // Its features' table: what they are and where they lie.
    const table = screen.getByRole("table", { name: "Features of pUC19c" });
    expect(
      within(table)
        .getAllByRole("row")
        .map((r) => r.textContent),
    ).toEqual([
      "FeatureKindStartEndStrand",
      "lac promoterpromoter1090reverse",
      "AmpRCDS200380reverse",
    ]);
  });

  it("lists ten features until asked for all of them", async () => {
    onPlasmid();
    answer(
      "GET /api/v1/entities/PLS-000001",
      entity({
        id: PLS_ID,
        hrid: "PLS-000001",
        type: "iki/dna.Plasmid",
        values: {
          name: "pUC19c",
          bases: "ACGT".repeat(100),
          topology: "circular",
          features: Array.from({ length: 12 }, (_, i) => ({
            type: "misc_binding",
            name: `site ${String(i + 1)}`,
            start: i * 20 + 1,
            end: i * 20 + 10,
            strand: "forward",
          })),
        },
      }),
    );
    const { user } = await renderApp("/entities/PLS-000001/");
    const table = screen.getByRole("table", { name: "Features of pUC19c" });
    expect(within(table).getAllByRole("row")).toHaveLength(11);
    const more = screen.getByRole("button", { name: "Show all 12" });
    expect(more).toHaveAttribute("aria-expanded", "false");
    await user.click(more);
    expect(within(table).getAllByRole("row")).toHaveLength(13);
    expect(screen.getByRole("button", { name: "Show fewer" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
  });
});

describe("a record's audit trail", () => {
  it("shows each revision's signatures with their meaning and reason", async () => {
    onPlasmid();
    await renderApp("/entities/PLS-000001/audit/");
    const signed = await screen.findByRole("list", { name: "Signatures of revision 2" });
    expect(signed).toHaveTextContent(
      "Approved by Ada Lovelace on 2026-11-19 at 16:00:00 UTC. Meaning: Approval.",
    );
    expect(
      await within(signed).findByText(/Reason: “Map matches GenBank L09137.2.”/),
    ).toBeVisible();
    expect(screen.getByText("Revision 3: Edited by Ada Lovelace")).toBeInTheDocument();
    expect(screen.getByText(/19 Nov 2026, 16:40 UTC/)).toBeInTheDocument();
  });

  it("narrows to a person and a field, in its address", async () => {
    onPlasmid();
    const { router, user } = await renderApp("/entities/PLS-000001/audit/");
    await screen.findByText("Revision 1: Created by Rosalind Franklin");
    const person = screen.getByRole("combobox", { name: "Person" });
    await waitFor(() => {
      expect(
        within(person)
          .getAllByRole("option")
          .map((o) => o.textContent),
      ).toEqual(["Anyone", "Ada Lovelace", "Rosalind Franklin"]);
    });
    await user.selectOptions(person, ADA.id);
    await waitFor(() => {
      expect(screen.queryByText("Revision 1: Created by Rosalind Franklin")).toBeNull();
    });
    expect(router.state.location.search).toEqual({ person: ADA.id });
    expect(calls("GET", "/api/v1/entities/PLS-000001/revisions").at(-1)?.query.get("person")).toBe(
      ADA.id,
    );
    await user.selectOptions(screen.getByRole("combobox", { name: "Field" }), "resistance");
    await waitFor(() => {
      expect(screen.queryByText(/^Revision 2:/)).toBeNull();
    });
    expect(router.state.location.search).toEqual({ person: ADA.id, field: "resistance" });
    expect(screen.getByText("Revision 3: Edited by Ada Lovelace")).toBeInTheDocument();
  });

  it("opens on a field from that field's history on the overview", async () => {
    onPlasmid();
    const { router, user } = await renderApp("/entities/PLS-000001/");
    await user.click(await screen.findByRole("button", { name: "History of Resistance" }));
    const history = await screen.findByRole("dialog", { name: "History of Resistance" });
    expect(history).toHaveTextContent("WhoAda Lovelace");
    expect(history).toHaveTextContent("When19 Nov 2026, 16:40 UTC");
    expect(history).toHaveTextContent("“Confirmed from the GenBank record.”");
    await user.click(within(history).getByRole("link", { name: "Resistance in the audit trail" }));
    await screen.findByText("Revision 3: Edited by Ada Lovelace");
    expect(router.state.location.pathname).toBe("/entities/PLS-000001/audit/");
    expect(screen.getByRole("combobox", { name: "Field" })).toHaveValue("resistance");
  });

  it("shows each field's last change by field", async () => {
    onPlasmid();
    const { user } = await renderApp("/entities/PLS-000001/audit/");
    await user.click(screen.getByRole("button", { name: "By field" }));
    const table = await screen.findByRole("table", { name: "Each field’s last change" });
    const rows = within(table)
      .getAllByRole("row")
      .map((r) => r.textContent);
    expect(rows[0]).toBe("FieldCurrent valueLast set byHowWhen (UTC)Revision");
    expect(rows).toContain("ResistanceAmpicillinAda Lovelacein the web app19 Nov 2026, 16:40 UTC3");
    // A long value is cut short, with its length.
    expect(rows.find((r) => r.startsWith("Bases"))).toMatch(/\(400 bases\)/);
    expect(screen.getByRole("button", { name: "By field" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("downloads whole, for inspection", async () => {
    onPlasmid();
    await renderApp("/entities/PLS-000001/audit/");
    const download = screen.getByRole("link", { name: "Export for inspection" });
    expect(download).toHaveAttribute("href", `/api/v1/entities/${PLS_ID}/revisions.csv`);
    expect(download).toHaveAttribute("download");
    expect(download).toHaveAccessibleDescription(
      "The whole trail as CSV, whatever it’s narrowed to here. Each download is in the audit log.",
    );
  });
});
