/**
 * Creating, editing, archiving and restoring entities, against a fake API:
 * what people see, and the calls each action makes.
 */

import type { Schemas } from "@ikisci/sdk";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  entity,
  field,
  KINASE,
  PROTEIN,
  PROTEIN_ID,
  renderApp,
  STRUCTURE,
} from "./entity-fixtures";
import { answer, calls, page, problem, respond, signedIn } from "./test-server";
import { timing } from "./uploads";

const SAMPLE_ID = "00000000-0000-7000-8000-00000000a001";

const SAMPLE: Schemas["EntityTypeOut"] = {
  ...PROTEIN,
  name: "lab.Sample",
  plugin: "lab",
  label: "Sample",
  label_plural: "Samples",
  description: null,
  hrid_prefix: "SMP",
  identity: [],
  on_duplicate: null,
  values_schema: {
    properties: {
      name: { type: "string", minLength: 1, maxLength: 50, title: "Name" },
      code: { type: "string", minLength: 1, title: "Code", "x-iki-immutable": true },
      count: { anyOf: [{ type: "integer", minimum: 0 }, { type: "null" }], title: "Count" },
      volume: { anyOf: [{ type: "number" }, { type: "null" }], title: "Volume" },
      kind: {
        anyOf: [{ type: "string", enum: ["blood", "tissue"] }, { type: "null" }],
        default: "blood",
      },
      source: { anyOf: [{ type: "string", format: "uuid" }, { type: "null" }] },
      aliases: { type: "array", items: { type: "string" } },
    },
  },
  fields: [
    field({ name: "name", label: "Name", type: "string", required: true }),
    field({
      name: "code",
      label: "Code",
      type: "string",
      required: true,
      immutable: true,
      unique: true,
    }),
    field({ name: "count", label: "Count", type: "int" }),
    field({
      name: "volume",
      label: "Volume",
      type: "quantity",
      unit: "mL",
      unit_symbol: "mL",
      input_units: [
        { code: "mL", symbol: "mL" },
        { code: "L", symbol: "L" },
        { code: "uL", symbol: "μL" },
      ],
    }),
    field({ name: "kind", label: "Kind", type: "enum", values: ["blood", "tissue"] }),
    field({ name: "source", label: "Source", type: "ref", to: "protein.Protein" }),
    field({ name: "aliases", label: "Aliases", type: "list", items: "string" }),
    field({ name: "score", label: "Score", type: "float", computed: true }),
  ],
};

function sample(overrides: Partial<Schemas["EntityOut"]> = {}): Schemas["EntityOut"] {
  return entity({
    id: SAMPLE_ID,
    hrid: "SMP-000001",
    type: "lab.Sample",
    values: { name: "Plasma A", code: "P-1", count: 3, volume: 2, kind: "blood", aliases: [] },
    ...overrides,
  });
}

function onLab(): void {
  signedIn();
  answer("GET /api/v1/entity-types", [PROTEIN, SAMPLE]);
  answer("GET /api/v1/projects", page([KINASE]));
}

describe("creating an entity", () => {
  it("fills in a form built from its type, and sends what was typed", async () => {
    onLab();
    answer("GET /api/v1/entities", (call: { query: URLSearchParams }) =>
      page(
        call.query.get("q") === "abl"
          ? [entity({ values: { name: "ABL1 kinase", sequence: "MKV" } })]
          : [],
      ),
    );
    answer("POST /api/v1/entities", respond(201, { ...sample(), reused: false, linked: false }));
    answer("GET /api/v1/entities/SMP-000001", sample());
    answer("GET /api/v1/entities/SMP-000001/provenance", {});
    answer("GET /api/v1/entities/SMP-000001/projects", []);
    const { user, router } = await renderApp(
      "/projects/kinase-atlas/entities/new/?type=lab.Sample",
    );
    expect(screen.getByRole("heading", { level: 2, name: "New sample" })).toBeInTheDocument();
    // Defaults come in; computed fields aren't typed in.
    expect(screen.getByRole("combobox", { name: /^Kind/ })).toHaveValue("blood");
    expect(screen.queryByRole("textbox", { name: /Score/ })).not.toBeInTheDocument();
    expect(screen.getByText("Actions set score: they aren’t typed in.")).toBeInTheDocument();

    await user.type(screen.getByRole("textbox", { name: "Name" }), " Plasma A ");
    await user.type(screen.getByRole("textbox", { name: "Code" }), "P-1");
    await user.type(screen.getByRole("textbox", { name: /^Volume/ }), "0.25");
    await user.selectOptions(screen.getByRole("combobox", { name: "Unit for Volume" }), "L");
    const source = screen.getByRole("combobox", { name: /^Source/ });
    await user.type(source, "abl");
    await user.click(await screen.findByRole("option", { name: /ABL1 kinase/ }));
    expect(source).toHaveValue("ABL1 kinase (PRT-000001)");
    await user.click(screen.getByRole("button", { name: "Add Aliases" }));
    await user.type(screen.getByRole("textbox", { name: "Aliases, item 1" }), "PA");
    await user.type(screen.getByRole("textbox", { name: /^Reason/ }), "Intake");
    await user.click(screen.getByRole("button", { name: "Create sample" }));

    await screen.findByRole("heading", { level: 1, name: /^Plasma A/ });
    expect(router.state.location.pathname).toBe("/entities/SMP-000001/");
    expect(screen.getByText("Created SMP-000001.")).toBeInTheDocument();
    expect(calls("POST", "/api/v1/entities")[0]?.body).toEqual({
      type: "lab.Sample",
      project: KINASE.id,
      values: {
        name: "Plasma A",
        code: "P-1",
        volume: { value: 0.25, unit: "L" },
        kind: "blood",
        source: PROTEIN_ID,
        aliases: ["PA"],
      },
      reason: "Intake",
      allow_duplicate: false,
    });
    const searched = calls("GET", "/api/v1/entities").map((c) => c.query.get("type"));
    expect(new Set(searched)).toEqual(new Set(["protein.Protein"]));
  });

  it("checks what it can before sending, and focuses the first problem", async () => {
    onLab();
    answer("GET /api/v1/entities", page([]));
    const { user } = await renderApp("/projects/kinase-atlas/entities/new/?type=lab.Sample");
    await user.type(screen.getByRole("textbox", { name: /^Count/ }), "-2");
    await user.click(screen.getByRole("button", { name: "Create sample" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Check the highlighted fields.");
    const name = screen.getByRole("textbox", { name: "Name" });
    expect(name).toHaveAccessibleDescription("Enter name.");
    expect(screen.getByRole("textbox", { name: "Code" })).toHaveAccessibleDescription(
      /No two samples in a project have the same one\./,
    );
    expect(name).toHaveFocus();
    expect(screen.getByRole("textbox", { name: /^Count/ })).toHaveAccessibleDescription(
      "It must be at least 0.",
    );
    expect(calls("POST")).toEqual([]);
  });

  it("shows the API's answer next to the field it's about", async () => {
    onLab();
    answer("GET /api/v1/entities", page([]));
    answer(
      "POST /api/v1/entities",
      problem(422, "validation_failed", "Some values are invalid.", [
        { pointer: "#/values/code", detail: "is already used by SMP-000002." },
      ]),
    );
    const { user } = await renderApp("/projects/kinase-atlas/entities/new/?type=lab.Sample");
    await user.type(screen.getByRole("textbox", { name: "Name" }), "B");
    await user.type(screen.getByRole("textbox", { name: "Code" }), "P-2");
    await user.click(screen.getByRole("button", { name: "Create sample" }));
    const code = screen.getByRole("textbox", { name: "Code" });
    await waitFor(() => {
      expect(code).toHaveAccessibleDescription(/is already used by SMP-000002\./);
    });
    expect(code).toHaveFocus();
  });

  it("names the entity it would duplicate, and creates one only when asked", async () => {
    onLab();
    answer("GET /api/v1/entities", page([]));
    answer("POST /api/v1/entities", (call: { body: { allow_duplicate: boolean } }) =>
      call.body.allow_duplicate
        ? respond(201, { ...sample({ hrid: "SMP-000003" }), reused: false, linked: false })
        : problem(409, "duplicate", "A sample with the same identity exists.", [
            { pointer: "#/values", detail: "The same as SMP-000002.", entity: "e2" } as never,
          ]),
    );
    answer("GET /api/v1/entities/SMP-000003", sample({ hrid: "SMP-000003" }));
    answer("GET /api/v1/entities/SMP-000003/provenance", {});
    answer("GET /api/v1/entities/SMP-000003/projects", []);
    const { user } = await renderApp("/projects/kinase-atlas/entities/new/?type=lab.Sample");
    await user.type(screen.getByRole("textbox", { name: "Name" }), "B");
    await user.type(screen.getByRole("textbox", { name: "Code" }), "P-2");
    await user.click(screen.getByRole("button", { name: "Create sample" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "This is the same as SMP-000002. Use it, or create a duplicate anyway.",
    );
    expect(within(alert).getByRole("link", { name: "SMP-000002" })).toHaveAttribute(
      "href",
      "/entities/SMP-000002/",
    );
    await user.click(within(alert).getByRole("button", { name: "Create a duplicate" }));
    await screen.findByText("Created SMP-000003.");
    expect(
      calls("POST").map((c) => (c.body as { allow_duplicate: boolean }).allow_duplicate),
    ).toEqual([false, true]);
  });

  it("opens the existing entity when its type reuses duplicates", async () => {
    onLab();
    answer("GET /api/v1/entities", page([]));
    answer("POST /api/v1/entities", { ...sample(), reused: true, linked: true });
    answer("GET /api/v1/entities/SMP-000001", sample());
    answer("GET /api/v1/entities/SMP-000001/provenance", {});
    answer("GET /api/v1/entities/SMP-000001/projects", []);
    const { user } = await renderApp("/projects/kinase-atlas/entities/new/?type=lab.Sample");
    await user.type(screen.getByRole("textbox", { name: "Name" }), "Plasma A");
    await user.type(screen.getByRole("textbox", { name: "Code" }), "P-1");
    await user.click(screen.getByRole("button", { name: "Create sample" }));
    expect(
      await screen.findByText(
        "Plasma A was already in Iki, so it’s been added to Kinase Atlas instead of making a copy.",
      ),
    ).toBeInTheDocument();
  });

  it("asks for a type first when none is given", async () => {
    onLab();
    const { user, router } = await renderApp("/projects/kinase-atlas/entities/new/");
    await user.selectOptions(screen.getByRole("combobox", { name: "Type" }), "lab.Sample");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    await screen.findByRole("heading", { level: 2, name: "New sample" });
    expect(router.state.location.search).toEqual({ type: "lab.Sample" });
  });

  it("is offered from the project's grid to its Editors", async () => {
    onLab();
    answer("GET /api/v1/entities", page([]));
    await renderApp("/projects/kinase-atlas/entities/?type=lab.Sample");
    expect(await screen.findByRole("link", { name: "New sample" })).toHaveAttribute(
      "href",
      "/projects/kinase-atlas/entities/new/?type=lab.Sample",
    );
  });
});

describe("editing an entity", () => {
  function onSample(current = sample()): void {
    onLab();
    answer("GET /api/v1/entities/SMP-000001", current);
    answer("GET /api/v1/entities/SMP-000001/provenance", {});
    answer("GET /api/v1/entities/SMP-000001/projects", []);
    answer("GET /api/v1/entities", page([]));
  }

  it("sends only what changed, with the revision it started from", async () => {
    onSample();
    answer("PATCH /api/v1/entities/" + SAMPLE_ID, sample({ revision: 3 }));
    const { user, router } = await renderApp("/entities/SMP-000001/edit/");
    expect(screen.getByRole("heading", { level: 1, name: "Edit Plasma A" })).toBeInTheDocument();
    const code = screen.getByRole("textbox", { name: "Code" });
    expect(code).toBeDisabled();
    expect(code).toHaveAccessibleDescription(
      "Set once: it can’t change. No two samples in a project have the same one.",
    );
    const name = screen.getByRole("textbox", { name: "Name" });
    await user.clear(name);
    await user.type(name, "Plasma B");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await screen.findByText("Saved revision 3 of SMP-000001.");
    expect(router.state.location.pathname).toBe("/entities/SMP-000001/");
    const sent = calls("PATCH")[0];
    expect(sent?.body).toEqual({ values: { name: "Plasma B" }, reason: "" });
    expect(sent?.headers.get("If-Match")).toBe('"2"');
  });

  it("says so when nothing changed, without saving", async () => {
    onSample();
    const { user } = await renderApp("/entities/SMP-000001/edit/");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Nothing to save: you haven’t changed anything.",
    );
    expect(calls("PATCH")).toEqual([]);
  });

  it("keeps both people's changes when someone else saved first", async () => {
    onSample();
    const theirs = sample({
      revision: 3,
      values: { ...sample().values, count: 9, name: "Plasma (theirs)" },
    });
    answer("GET /api/v1/entities/" + SAMPLE_ID, theirs);
    answer("PATCH /api/v1/entities/" + SAMPLE_ID, (call: { headers: Headers }) =>
      call.headers.get("If-Match") === '"2"'
        ? problem(412, "stale", "Someone changed it since you read it.")
        : sample({ revision: 4 }),
    );
    const { user } = await renderApp("/entities/SMP-000001/edit/");
    const name = screen.getByRole("textbox", { name: "Name" });
    await user.clear(name);
    await user.type(name, "Plasma B");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "Someone else saved SMP-000001 while you were editing (it’s at revision 3 now)",
    );
    expect(alert).toHaveTextContent("Count now has its new value.");
    expect(alert).toHaveTextContent("You both changed Name: yours is kept.");
    expect(screen.getByRole("textbox", { name: /^Count/ })).toHaveValue("9");
    expect(name).toHaveValue("Plasma B");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await screen.findByText("Saved revision 4 of SMP-000001.");
    const last = calls("PATCH").at(-1);
    expect(last?.headers.get("If-Match")).toBe('"3"');
    expect(last?.body).toEqual({ values: { name: "Plasma B" }, reason: "" });
  });

  it("isn't offered for archived entities or to people who can't edit", async () => {
    onSample(sample({ can: { edit: false } }));
    await renderApp("/entities/SMP-000001/");
    expect(screen.queryByRole("link", { name: "Edit" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Archive" })).not.toBeInTheDocument();
  });
});

describe("archiving and restoring", () => {
  it("asks why, then archives it with the revision on the page", async () => {
    onLab();
    answer("GET /api/v1/entities/SMP-000001", sample());
    answer("GET /api/v1/entities/SMP-000001/provenance", {});
    answer("GET /api/v1/entities/SMP-000001/projects", []);
    answer(
      "PATCH /api/v1/entities/" + SAMPLE_ID,
      sample({ revision: 3, archived: true, archived_at: "2026-03-16T09:00:00Z" }),
    );
    const { user } = await renderApp("/entities/SMP-000001/");
    await user.click(screen.getByRole("button", { name: "Archive" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Archive SMP-000001?" });
    await user.type(within(dialog).getByRole("textbox", { name: /Reason/ }), "Used up");
    await user.click(within(dialog).getByRole("button", { name: "Archive" }));
    await screen.findByText("Archived SMP-000001.");
    const sent = calls("PATCH")[0];
    expect(sent?.body).toEqual({ archived: true, reason: "Used up" });
    expect(sent?.headers.get("If-Match")).toBe('"2"');
    expect(screen.getByRole("button", { name: "Restore" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Edit" })).not.toBeInTheDocument();
  });
});

describe("file fields", () => {
  const UPLOAD = "00000000-0000-7000-8000-0000000b0002";
  const MADE = "00000000-0000-7000-8000-0000000f0002";

  /** Storage accepts every part. */
  class Storage {
    static sent = 0;
    upload = { onprogress: null as ((event: { loaded: number }) => void) | null };
    onload: (() => void) | null = null;
    onloadend: (() => void) | null = null;
    onerror: (() => void) | null = null;
    status = 200;
    open(): void {
      /* the signed URL */
    }
    abort(): void {
      /* nothing in flight */
    }
    send(body: Blob): void {
      Storage.sent += body.size;
      setTimeout(() => {
        this.upload.onprogress?.({ loaded: body.size });
        this.onload?.();
        this.onloadend?.();
      }, 0);
    }
  }

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("uploads a file from this computer, and creates the entity with it", async () => {
    vi.stubGlobal("XMLHttpRequest", Storage);
    Object.assign(timing, { retry: 1, poll: 1, maxPoll: 1 });
    signedIn();
    answer("GET /api/v1/entity-types", [PROTEIN, STRUCTURE]);
    answer("GET /api/v1/projects", page([KINASE]));
    answer("GET /api/v1/entities", page([]));
    answer("GET /api/v1/files", page([]));
    const upload = {
      id: UPLOAD,
      project: KINASE.id,
      name: "1abc.pdb",
      media_type: "chemical/x-pdb",
      size: 12,
      sha256: "x".repeat(64),
      part_size: 64,
      part_count: 1,
      error: "",
      created_at: "2026-03-14T09:00:00Z",
      expires_at: "2026-03-15T09:00:00Z",
    };
    const made = {
      id: MADE,
      name: "1abc.pdb",
      media_type: "chemical/x-pdb",
      size: 12,
      sha256: "x".repeat(64),
      project: null,
      created_by: null,
      created_at: "2026-03-14T09:00:00Z",
      external: false,
    };
    answer(
      "POST /api/v1/uploads",
      respond(201, {
        ...upload,
        state: "pending",
        file: null,
        parts: [{ number: 1, size: 12, url: "https://storage.example/1" }],
      }),
    );
    answer(
      "POST /api/v1/uploads/" + UPLOAD + "/complete",
      respond(202, { ...upload, state: "checking", file: null }),
    );
    answer("GET /api/v1/uploads/" + UPLOAD, { ...upload, state: "ready", file: made });
    answer(
      "POST /api/v1/entities",
      respond(201, {
        ...entity({ hrid: "PST-000001", type: "protein.ProteinStructure", values: {} }),
        reused: false,
        linked: false,
      }),
    );
    answer(
      "GET /api/v1/entities/PST-000001",
      entity({ hrid: "PST-000001", type: "protein.ProteinStructure", values: {} }),
    );
    answer("GET /api/v1/entities/PST-000001/provenance", {});
    answer("GET /api/v1/entities/PST-000001/projects", []);
    const { user } = await renderApp(
      "/projects/kinase-atlas/entities/new/?type=protein.ProteinStructure",
    );
    const structure = screen.getByRole("group", { name: "Structure" });
    await user.upload(
      within(structure).getByLabelText("Upload a file"),
      // As browsers give it: they don't know the type.
      new File(["ATOM 1 N ALA"], "1abc.pdb"),
    );
    await waitFor(() => {
      expect(
        within(structure).getByRole("combobox", { name: /already in the project/ }),
      ).toHaveValue(MADE);
    });
    expect(Storage.sent).toBe(12);
    expect(calls("POST", "/api/v1/uploads")[0]?.body).toMatchObject({
      name: "1abc.pdb",
      media_type: "chemical/x-pdb",
    });
    await user.click(screen.getByRole("button", { name: "Create protein structure" }));
    await screen.findByText("Created PST-000001.");
    expect(calls("POST", "/api/v1/entities")[0]?.body).toMatchObject({
      values: { structure: MADE },
    });
  });

  it("refuses a file its field doesn't take, before uploading it", async () => {
    signedIn();
    const strict: typeof STRUCTURE = {
      ...STRUCTURE,
      values_schema: {
        properties: {
          structure: {
            type: "string",
            "x-iki-media-types": ["chemical/x-pdb"],
            "x-iki-max-size": 1024,
          },
        },
      },
    };
    answer("GET /api/v1/entity-types", [PROTEIN, strict]);
    answer("GET /api/v1/projects", page([KINASE]));
    answer("GET /api/v1/entities", page([]));
    answer("GET /api/v1/files", page([]));
    await renderApp("/projects/kinase-atlas/entities/new/?type=protein.ProteinStructure");
    // As if the browser's file chooser let it through: some do, for "All files".
    const user = userEvent.setup({ applyAccept: false });
    const structure = screen.getByRole("group", { name: "Structure" });
    expect(structure).toHaveAccessibleDescription("It takes chemical/x-pdb files, up to 1.0 KiB.");
    const input = within(structure).getByLabelText("Upload a file");
    expect(input).toHaveAttribute("accept", "chemical/x-pdb,.pdb,.ent");
    await user.upload(input, new File(["x"], "notes.txt", { type: "text/plain" }));
    expect(
      await within(structure).findByText("notes.txt is text/plain: choose chemical/x-pdb."),
    ).toBeInTheDocument();
    expect(calls("POST", "/api/v1/uploads")).toEqual([]);
  });
});
