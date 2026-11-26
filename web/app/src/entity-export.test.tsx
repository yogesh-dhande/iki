/** Exporting a record as a file (ADR 0171): from its page's ⋯ menu, or a revision from its
 * audit trail. */

import type { Schemas } from "@ikisci/sdk";
import { screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { iki } from "./api";
import {
  entity,
  field,
  moreAction,
  PROTEIN,
  renderApp,
  revision,
  withTypes,
} from "./entity-fixtures";
import { answer, calls, page, problem } from "./test-server";

const ID = "00000000-0000-7000-8000-00000000d001";

const SEQUENCE_TYPE: Schemas["EntityTypeOut"] = {
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
  exports: [
    {
      export: "sequence" as const,
      formats: [
        {
          format: "genbank" as const,
          label: "GenBank",
          extension: "gb",
          media_type: "chemical/seq-na-genbank",
          description: "The bases, topology and features, with their qualifiers.",
          many: "file" as const,
          plugin: null,
        },
        {
          format: "fasta" as const,
          label: "FASTA",
          extension: "fasta",
          media_type: "chemical/seq-na-fasta",
          description: "The bases, with the record's ID, name and topology in its header.",
          many: "file" as const,
          plugin: null,
        },
      ],
      sequence: "bases",
      features: null,
    },
  ],
};

function file(name: string, leftOut = 0): Response {
  return new Response("LOCUS       pTest\n//\n", {
    headers: {
      "Content-Type": "chemical/seq-na-genbank",
      "Content-Disposition": `attachment; filename="${name}"`,
      "Iki-Left-Out": String(leftOut),
    },
  });
}

let saved: { name: string; href: string }[] = [];

beforeEach(() => {
  saved = [];
  URL.createObjectURL = vi.fn(() => "blob:file");
  URL.revokeObjectURL = vi.fn();
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
    this: HTMLAnchorElement,
  ) {
    saved.push({ name: this.download, href: this.href });
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

function onSequence(types = [PROTEIN, SEQUENCE_TYPE]) {
  withTypes();
  answer("GET /api/v1/entity-types", types);
  answer(
    "GET /api/v1/entities/DNA-000001",
    entity({
      id: ID,
      hrid: "DNA-000001",
      type: "iki/dna.DNASequence",
      values: { name: "pTest", bases: "ACGT" },
    }),
  );
}

describe("exporting a record", () => {
  it("downloads the file chosen, by keyboard, and says so", async () => {
    onSequence();
    answer(`GET /api/v1/entities/${ID}/export`, file("pTest.fasta"));
    const { user } = await renderApp("/entities/DNA-000001/");
    // From its ⋯ menu (ADR 0176), by keyboard.
    const button = await screen.findByRole("button", { name: "More actions for DNA-000001" });
    button.focus();
    await user.keyboard("{Enter}");
    const item = await screen.findByRole("menuitem", { name: "Export…" });
    await waitFor(() => {
      expect(item).toHaveFocus();
    });
    await user.keyboard("{Enter}");
    const dialog = await screen.findByRole("dialog", { name: "Export pTest" });
    const genbank = within(dialog).getByRole("radio", { name: /^GenBank/ });
    expect(genbank).toBeChecked();
    await user.tab();
    expect(genbank).toHaveFocus();
    expect(dialog).toHaveTextContent("The bases, topology and features, with their qualifiers.");
    await user.keyboard("{ArrowDown}");
    expect(within(dialog).getByRole("radio", { name: /^FASTA/ })).toBeChecked();
    await user.tab();
    expect(within(dialog).getByRole("button", { name: "Download" })).toHaveFocus();
    // The person leaves after asking for the file but before the signed GET is dispatched.
    const leaving: Parameters<typeof iki.use>[0] = {
      onRequest({ request }) {
        if (new URL(request.url).pathname === `/api/v1/entities/${ID}/export`) {
          window.dispatchEvent(new PageTransitionEvent("pagehide", { persisted: true }));
        }
        return request;
      },
    };
    iki.use(leaving);
    try {
      await user.keyboard("{Enter}");
      expect(await screen.findByText("Downloaded pTest.fasta.")).toBeInTheDocument();
    } finally {
      iki.eject(leaving);
      window.dispatchEvent(new PageTransitionEvent("pageshow", { persisted: true }));
    }
    const [sent] = calls("GET", `/api/v1/entities/${ID}/export`);
    expect(sent?.query.get("format")).toBe("fasta");
    expect(sent?.query.has("revision")).toBe(false);
    expect(saved).toEqual([{ name: "pTest.fasta", href: "blob:file" }]);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await waitFor(() => {
      expect(button).toHaveFocus();
    });
  });

  it("says when the file couldn't hold all of it, and where it says what", async () => {
    onSequence();
    answer(`GET /api/v1/entities/${ID}/export`, file("pTest.gb", 2));
    const { user } = await renderApp("/entities/DNA-000001/");
    await moreAction(user, "DNA-000001", "Export…");
    await user.click(await screen.findByRole("button", { name: "Download" }));
    expect(
      await screen.findByText(
        "Downloaded pTest.gb. It couldn’t hold 2 things of the record: the file’s COMMENT says which.",
      ),
    ).toBeInTheDocument();
  });

  it("shows why it couldn't be written, and keeps the dialog open", async () => {
    onSequence();
    answer(
      `GET /api/v1/entities/${ID}/export`,
      problem(404, "sequence_unreadable", "Its sequence isn't a record you can read."),
    );
    const { user } = await renderApp("/entities/DNA-000001/");
    await moreAction(user, "DNA-000001", "Export…");
    await user.click(await screen.findByRole("button", { name: "Download" }));
    const dialog = await screen.findByRole("dialog", { name: "Export pTest" });
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "Its sequence isn't a record you can read.",
    );
    expect(saved).toEqual([]);
  });

  it("downloads a plugin's format, saying which plugin writes it", async () => {
    const worklist = {
      format: "acme/liquid_handling.tecan-worklist",
      label: "Tecan worklist",
      extension: "gwl",
      media_type: "text/x-tecan-gwl",
      description: "An aspirate, a dispense and a wash for each transfer.",
      many: "file" as const,
      plugin: "acme/liquid_handling",
    };
    onSequence([
      PROTEIN,
      {
        ...SEQUENCE_TYPE,
        exports: [
          ...(SEQUENCE_TYPE.exports ?? []),
          { export: "plugin" as const, writer: worklist.format, formats: [worklist], fields: {} },
        ],
      },
    ]);
    answer(`GET /api/v1/entities/${ID}/export`, file("pTest.gwl", 1));
    const { user } = await renderApp("/entities/DNA-000001/");
    await moreAction(user, "DNA-000001", "Export…");
    const dialog = await screen.findByRole("dialog", { name: "Export pTest" });
    expect(dialog).toHaveTextContent(
      "An aspirate, a dispense and a wash for each transfer. Written by the acme/liquid_handling plugin.",
    );
    await user.click(within(dialog).getByRole("radio", { name: /^Tecan worklist/ }));
    await user.click(within(dialog).getByRole("button", { name: "Download" }));
    expect(
      await screen.findByText(
        "Downloaded pTest.gwl. Its writer couldn’t hold 1 thing of the record as it is: the download’s audit event says which.",
      ),
    ).toBeInTheDocument();
    const [sent] = calls("GET", `/api/v1/entities/${ID}/export`);
    expect(sent?.query.get("format")).toBe("acme/liquid_handling.tecan-worklist");
    expect(saved.map((s) => s.name)).toEqual(["pTest.gwl"]);
  });

  it("says when the export sandbox is busy", async () => {
    onSequence();
    answer(
      `GET /api/v1/entities/${ID}/export`,
      problem(503, "sandbox_unavailable", "The export sandbox is busy. Try again in a moment."),
    );
    const { user } = await renderApp("/entities/DNA-000001/");
    await moreAction(user, "DNA-000001", "Export…");
    await user.click(await screen.findByRole("button", { name: "Download" }));
    const dialog = await screen.findByRole("dialog", { name: "Export pTest" });
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "The export sandbox is busy. Try again in a moment.",
    );
    expect(saved).toEqual([]);
  });

  it("isn't offered for a type that exports nothing", async () => {
    onSequence([PROTEIN, { ...SEQUENCE_TYPE, exports: [] }]);
    const { user } = await renderApp("/entities/DNA-000001/");
    expect(await screen.findByRole("heading", { name: "pTest" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "More actions for DNA-000001" }));
    expect(await screen.findByRole("menuitem", { name: /audit trail/ })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "Export…" })).not.toBeInTheDocument();
  });

  it("downloads a revision as it was, from the audit trail", async () => {
    onSequence();
    answer(
      "GET /api/v1/entities/DNA-000001/revisions",
      page([
        revision({ number: 2, kind: "edited", changed_fields: ["name"] }),
        revision({ number: 1, changed_fields: ["name", "bases"] }),
      ]),
    );
    answer(`GET /api/v1/entities/${ID}/export`, file("pOld (revision 1).gb"));
    const { user } = await renderApp("/entities/DNA-000001/audit/");
    // Every revision has one, the latest too: an icon button in its title's row.
    const latest = await screen.findByRole("button", { name: "Export revision 2" });
    expect(latest.closest(".trail-title")).not.toBeNull();
    await user.click(await screen.findByRole("button", { name: "Export revision 1" }));
    const dialog = await screen.findByRole("dialog", { name: "Export pTest, revision 1" });
    await user.click(within(dialog).getByRole("button", { name: "Download" }));
    expect(await screen.findByText("Downloaded pOld (revision 1).gb.")).toBeInTheDocument();
    const [sent] = calls("GET", `/api/v1/entities/${ID}/export`);
    expect(sent?.query.get("revision")).toBe("1");
    expect(saved[0]?.name).toBe("pOld (revision 1).gb");
  });
});
