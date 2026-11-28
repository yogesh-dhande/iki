/**
 * Plugins' rules (ADR 0127) and guards (ADR 0128) in the app, against a fake API: forms
 * check rules as they're typed with the real iki-wasm, run here without a worker; a save
 * a rule holds asks why; a held record is reviewed; and a guard's refusal is said.
 */

import type { Schemas } from "@ikisci/sdk";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";

import {
  addFilter,
  ALICE,
  entity,
  field,
  KINASE,
  nth,
  PROTEIN,
  renderApp,
} from "./entity-fixtures";
import { filterable, searchWords } from "./filters";
import { answer, calls, page, problem, respond, signedIn, stale } from "./test-server";

vi.mock("./apps", async (original) => {
  const real = await original<typeof import("./apps")>();
  // Built by scripts/build-wasm.sh, as the TypeScript job does before the tests.
  const here = join(process.cwd(), "wasm", "iki_wasm.wasm");
  const bytes = readFileSync(existsSync(here) ? here : join("web", "app", "wasm", "iki_wasm.wasm"));
  const { instance } = await WebAssembly.instantiate(bytes, {});
  const wasm = instance.exports as unknown as {
    memory: WebAssembly.Memory;
    iki_input: (len: number) => number;
    iki_evaluate_rules: () => void;
    iki_output: () => number;
    iki_output_len: () => number;
  };
  return {
    ...real,
    expressionRenderer: () => ({
      rules: (request: string) => {
        const sent = new TextEncoder().encode(request);
        const at = wasm.iki_input(sent.length);
        new Uint8Array(wasm.memory.buffer, at, sent.length).set(sent);
        wasm.iki_evaluate_rules();
        const out = new Uint8Array(wasm.memory.buffer, wasm.iki_output(), wasm.iki_output_len());
        const found = JSON.parse(new TextDecoder().decode(out)) as { results: unknown };
        return Promise.resolve(found.results);
      },
    }),
  };
});

const LOT_ID = "00000000-0000-7000-8000-00000000c001";
const HOLD_ID = "00000000-0000-7000-8000-00000000c101";
const OBSERVATION = {
  id: "00000000-0000-7000-8000-00000000c201",
  label: "Purity in specification",
  message: "Its purity, 88.5%, is under 90%.",
  held_by: ALICE,
  agent: null,
  held_at: "2026-11-18T09:00:00Z",
  reason: "Retested.",
  revision: 2,
  legacy_gap: false,
};

function rule(more: Partial<Schemas["RuleOut"]> & Pick<Schemas["RuleOut"], "name">) {
  return {
    plugin: "acme/lab",
    label: more.name,
    description: null,
    kind: "check" as const,
    check: null,
    when: null,
    message: "",
    fields: [],
    reads: [],
    hold: null,
    unique: [],
    no_overlap: null,
    while: null,
    ...more,
  } as Schemas["RuleOut"];
}

const LOT: Schemas["EntityTypeOut"] = {
  ...PROTEIN,
  name: "acme/lab.Lot",
  plugin: "acme/lab",
  label: "Lot",
  label_plural: "Lots",
  description: null,
  hrid_prefix: "LOT",
  identity: [],
  values_schema: {},
  fields: [
    field({ name: "name", label: "Name", type: "string", plugin: "acme/lab" }),
    field({ name: "purity", label: "Purity", type: "float", plugin: "acme/lab" }),
    field({ name: "made_on", label: "Made on", type: "date", plugin: "acme/lab" }),
    field({ name: "expires_on", label: "Expires on", type: "date", plugin: "acme/lab" }),
  ],
  rules: [
    rule({
      name: "purity_in_specification",
      label: "Purity in specification",
      check: "record.purity >= 90.0",
      message: "Its purity, {{ record.purity }}%, is under 90%.",
      fields: ["purity"],
      reads: ["purity"],
      hold: { reviewed_by: ["acme/lab:qa"] },
    }),
    rule({
      name: "expires_after_made",
      label: "Expires after it's made",
      check: "record.expires_on > record.made_on",
      message: "It expires on {{ record.expires_on }}, before it's made.",
      fields: ["expires_on"],
      reads: ["expires_on", "made_on"],
    }),
    rule({
      name: "one_per_name",
      label: "One per name",
      kind: "unique",
      message: "Another lot has this name.",
      fields: ["name"],
      reads: ["name"],
      unique: ["name"],
    }),
  ],
  rule_variables: {
    record: {
      type: "object",
      label: "Lot",
      fields: {
        id: { type: "string" },
        hrid: { type: "string" },
        name: { type: "string" },
        purity: { type: "double" },
        made_on: { type: "timestamp" },
        expires_on: { type: "timestamp" },
      },
    },
    now: { type: "timestamp" },
    today: { type: "timestamp" },
  },
};

function lot(overrides: Partial<Schemas["EntityOut"]> = {}): Schemas["EntityOut"] {
  return entity({
    id: LOT_ID,
    hrid: "LOT-000001",
    type: "acme/lab.Lot",
    values: { name: "Lot 7", purity: 97.5, made_on: "2026-11-01", expires_on: "2027-11-01" },
    holds: [],
    breaches: [],
    ...overrides,
  });
}

function onLab(): void {
  signedIn();
  answer("GET /api/v1/entity-types", [PROTEIN, LOT]);
  answer("GET /api/v1/projects", page([KINASE]));
  answer("GET /api/v1/entities/LOT-000001", lot());
  answer("GET /api/v1/entities/LOT-000001/provenance", {});
  answer("GET /api/v1/entities/LOT-000001/projects", []);
}

describe("rules in forms", () => {
  it("says what a save would break as it's typed, and sends nothing a rule refuses", async () => {
    onLab();
    const { user } = await renderApp("/projects/kinase-atlas/entities/new/?type=acme/lab.Lot");
    await user.type(screen.getByRole("textbox", { name: /^Name/ }), "Lot 8");
    // Date inputs have no role of their own.
    fireEvent.change(screen.getByLabelText(/^Made on/), { target: { value: "2026-11-02" } });
    const expires = screen.getByLabelText(/^Expires on/);
    fireEvent.change(expires, { target: { value: "2026-11-01" } });
    // A date reads as the day alone.
    expect(
      await screen.findByText("It expires on 2026-11-01, before it's made."),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Create lot" }));
    expect(calls("POST", "/api/v1/entities")).toHaveLength(0);
    expect(expires).toHaveAttribute("aria-invalid", "true");
  });

  it("asks why to save values a rule holds, and sends the reason", async () => {
    onLab();
    answer("POST /api/v1/entities", respond(201, { ...lot(), reused: false, registered: false }));
    const { user } = await renderApp("/projects/kinase-atlas/entities/new/?type=acme/lab.Lot");
    await user.type(screen.getByRole("textbox", { name: /^Name/ }), "Lot 7");
    await user.type(screen.getByRole("textbox", { name: /^Purity/ }), "88.5");
    expect(await screen.findByText("Its purity, 88.5%, is under 90%.")).toBeInTheDocument();
    const why = await screen.findByRole("textbox", { name: "Why save it anyway" });
    await user.click(screen.getByRole("button", { name: "Create lot" }));
    expect(
      await screen.findByText("Say why it’s saved though it breaks these rules."),
    ).toBeInTheDocument();
    expect(calls("POST", "/api/v1/entities")).toHaveLength(0);

    await user.type(why, "Retested: the first assay's column was failing.");
    await user.click(screen.getByRole("button", { name: "Create lot" }));
    await waitFor(() => {
      expect(calls("POST", "/api/v1/entities")).toHaveLength(1);
    });
    expect(calls("POST", "/api/v1/entities")[0]?.body).toMatchObject({
      values: { name: "Lot 7", purity: 88.5 },
      hold_reason: "Retested: the first assay's column was failing.",
    });
  });

  it("asks for a hold reason when Iki says a rule it couldn't check here holds it", async () => {
    onLab();
    answer(
      "POST /api/v1/entities",
      problem(422, "hold_reason_required", "Its values break rules that hold it for review.", [
        {
          pointer: "#/values/name",
          detail: "Its protein is BSL-3.",
          rule: "protein_not_bsl3",
          hold: true,
        } as { pointer: string; detail: string },
      ]),
    );
    const { user } = await renderApp("/projects/kinase-atlas/entities/new/?type=acme/lab.Lot");
    await user.type(screen.getByRole("textbox", { name: /^Name/ }), "Lot 9");
    await user.click(screen.getByRole("button", { name: "Create lot" }));
    expect(await screen.findByText("Its protein is BSL-3.")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Why save it anyway" })).toBeInTheDocument();
  });

  it("checks only the rules an edit's changes read", async () => {
    onLab();
    // Saved before its rule changed: it breaks it, but an edit of its name doesn't ask.
    answer(
      "GET /api/v1/entities/LOT-000001",
      lot({ values: { name: "Lot 7", purity: 80, made_on: null, expires_on: null } }),
    );
    answer(`PATCH /api/v1/entities/${LOT_ID}`, respond(200, lot({ revision: 3 })));
    const { user } = await renderApp("/entities/LOT-000001/edit");
    const name = await screen.findByRole("textbox", { name: /^Name/ });
    await user.type(name, "b");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => {
      expect(calls("PATCH", `/api/v1/entities/${LOT_ID}`)).toHaveLength(1);
    });
    expect(screen.queryByRole("textbox", { name: "Why save it anyway" })).not.toBeInTheDocument();
  });

  it("checks a rule that reads the state a correction would return to", async () => {
    onLab();
    const draft = {
      name: "draft",
      label: "Draft",
      description: null,
      set_by: ["acme/lab.analyst"],
      locks: false,
      holds_amount: false,
      reason: null,
      signatures: [],
    };
    const reviewed = {
      ...draft,
      name: "reviewed",
      label: "Reviewed",
      locks: true,
      on_edit: "draft",
    };
    answer("GET /api/v1/entity-types", [
      {
        ...LOT,
        fields: [
          ...LOT.fields,
          field({ name: "review", label: "Review", type: "state", states: [draft, reviewed] }),
        ],
        rules: [
          rule({
            name: "draft_purity",
            check: 'record.review != "draft" || record.purity >= 98.0',
            message: "A draft needs purity of at least 98%.",
            fields: ["purity"],
            reads: ["review", "purity"],
          }),
        ],
        rule_variables: {
          record: {
            type: "object",
            label: "Lot",
            fields: { review: { type: "string" }, purity: { type: "double" } },
          },
        },
      },
    ]);
    answer(
      "GET /api/v1/entities/LOT-000001",
      lot({
        values: { ...lot().values, review: "reviewed" },
        locked: true,
        locked_by: ["review"],
        can: { ...lot().can, edit: false, correct: true },
      }),
    );
    const { user } = await renderApp("/entities/LOT-000001/edit");
    await user.type(await screen.findByRole("textbox", { name: /^Name/ }), "B");
    expect(await screen.findByText("A draft needs purity of at least 98%.")).toBeInTheDocument();
    expect(calls("PATCH")).toHaveLength(0);
  });
});

describe("holds and breaches", () => {
  const HOLD: Schemas["HoldOut"] = {
    id: HOLD_ID,
    rule: "purity_in_specification",
    label: "Purity in specification",
    message: "Its purity, 88.5%, is under 90%.",
    reviewed_by: ["acme/lab:qa"],
    held_by: ALICE,
    agent: null,
    held_at: "2026-11-18T09:00:00Z",
    reason: "Retested.",
    revision: 2,
    last_revision: 2,
    occurrences: [OBSERVATION],
    review_version: "a".repeat(64),
    can_review: true,
    why_not: null,
  };

  it("shows a hold, and its reviewer clears it with a reason", async () => {
    onLab();
    answer("GET /api/v1/entities/LOT-000001", lot({ revision: 2, holds: [HOLD] }));
    answer(`POST /api/v1/entities/${LOT_ID}/holds/${HOLD_ID}/review`, respond(200, lot()));
    const { user } = await renderApp("/entities/LOT-000001/");
    const held = await screen.findByRole("region", { name: "Held for review" });
    expect(within(held).getByText("Purity in specification")).toBeInTheDocument();
    expect(within(held).getByText(/“Retested\.”/)).toBeInTheDocument();
    await user.click(within(held).getByRole("button", { name: "Review" }));
    const dialog = await screen.findByRole("alertdialog", {
      name: "Review the hold on LOT-000001",
    });
    expect(dialog).toHaveTextContent("As one of its qa reviewers");
    await user.click(within(dialog).getByRole("button", { name: "Clear the hold" }));
    expect(await within(dialog).findByText("Say why it may stand as it is.")).toBeInTheDocument();
    await user.type(
      within(dialog).getByRole("textbox", { name: "Why it may stand" }),
      "Within the method's error.",
    );
    await user.click(within(dialog).getByRole("button", { name: "Clear the hold" }));
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    });
    const sent = calls("POST", `/api/v1/entities/${LOT_ID}/holds/${HOLD_ID}/review`)[0];
    expect(sent?.headers.get("If-Match")).toBe('"2"');
    expect(sent?.body).toEqual({
      reason: "Within the method's error.",
      review_version: HOLD.review_version,
    });
  });

  it("shows the hold as it is now and asks for a new reason after a stale review", async () => {
    onLab();
    const record = "/api/v1/entities/LOT-000001";
    const route = `/api/v1/entities/${LOT_ID}/holds/${HOLD_ID}/review`;
    answer(`GET ${record}`, lot({ revision: 2, holds: [HOLD] }));
    const { user } = await renderApp("/entities/LOT-000001/");
    const held = await screen.findByRole("region", { name: "Held for review" });
    await user.click(within(held).getByRole("button", { name: "Review" }));
    const dialog = await screen.findByRole("alertdialog");
    const reason = within(dialog).getByRole("textbox", { name: "Why it may stand" });
    const clear = within(dialog).getByRole("button", { name: "Clear the hold" });
    await user.type(reason, "Reviewed the original measurement.");
    const retest = {
      ...OBSERVATION,
      id: "00000000-0000-7000-8000-00000000c202",
      message: "Retest under 90%.",
      reason: "As measured on the SEC.",
      revision: 3,
    };
    const updated = {
      ...HOLD,
      last_revision: 3,
      occurrences: [OBSERVATION, retest],
      review_version: "b".repeat(64),
    };
    // A retest added to the hold meanwhile: Iki refuses the old one, and the page reads it.
    answer(`POST ${route}`, () => {
      answer(`GET ${record}`, lot({ revision: 3, holds: [updated] }));
      return stale(3);
    });
    await user.click(clear);
    // In place: the record and hold as they are now, and the reason to give again.
    expect(await within(dialog).findByText("You’re reviewing record revision 3.")).toBeVisible();
    expect(within(dialog).getByRole("alert")).toHaveTextContent(
      "Someone changed the record or its hold while this was open.",
    );
    expect(calls("POST", route)[0]?.headers.get("If-Match")).toBe('"2"');
    const refreshed = dialog;
    expect(refreshed).toHaveTextContent("As measured on the SEC.");
    expect(refreshed).toHaveTextContent("Its purity, 88.5%, is under 90%.");
    const nextReason = within(refreshed).getByRole("textbox", { name: "Why it may stand" });
    expect(nextReason).toHaveValue("");
    await user.type(nextReason, "Reviewed the retest.");
    answer(`POST ${route}`, lot({ revision: 3 }));
    await user.click(within(refreshed).getByRole("button", { name: "Clear the hold" }));
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    });
    expect(calls("POST", route).at(-1)?.headers.get("If-Match")).toBe('"3"');
    expect(calls("POST", route).at(-1)?.body).toEqual({
      reason: "Reviewed the retest.",
      review_version: updated.review_version,
    });
  });

  it("says why someone may not review a hold", async () => {
    onLab();
    const why = "You saved what it holds: someone else reviews it.";
    answer(
      "GET /api/v1/entities/LOT-000001",
      lot({ holds: [{ ...HOLD, can_review: false, why_not: why }] }),
    );
    await renderApp("/entities/LOT-000001/");
    const held = await screen.findByRole("region", { name: "Held for review" });
    const review = within(held).getByRole("button", { name: "Review" });
    expect(review).toHaveAttribute("aria-disabled", "true");
    expect(review).toHaveAccessibleDescription(why);
  });

  it("shows the rules a deploy found it breaks", async () => {
    onLab();
    answer(
      "GET /api/v1/entities/LOT-000001",
      lot({
        breaches: [
          {
            rule: "expires_after_made",
            label: "Expires after it's made",
            message: "It expires on 2026-10-01, before it's made.",
            found_at: "2026-11-18T09:00:00Z",
          },
        ],
      }),
    );
    await renderApp("/entities/LOT-000001/");
    const breaks = await screen.findByRole("region", { name: "Breaks its plugins’ rules" });
    expect(within(breaks).getByText("Expires after it's made")).toBeInTheDocument();
  });
});

describe("guards", () => {
  const STATUS = field({
    name: "status",
    label: "Status",
    type: "state",
    plugin: "acme/lab",
    initial: "in_review",
    states: [
      {
        name: "in_review",
        label: "In review",
        description: null,
        set_by: ["acme/lab:analyst"],
        locks: false,
        holds_amount: false,
        reason: null,
        signatures: [],
        guards: [],
      },
      {
        name: "approved",
        label: "Approved",
        description: null,
        set_by: ["acme/lab:qa"],
        locks: true,
        holds_amount: false,
        reason: null,
        signatures: [],
        guards: [],
      },
    ] as Schemas["StateOut"][],
  });
  const FOUR_EYES = "Someone other than its analyst approves it.";

  it("says a guard's refusal by the state, and Iki's if it refuses at the save", async () => {
    onLab();
    answer("GET /api/v1/entity-types", [PROTEIN, { ...LOT, fields: [...LOT.fields, STATUS] }]);
    answer(
      "GET /api/v1/entities/LOT-000001",
      lot({
        values: { name: "Lot 7", purity: 97.5, status: "in_review" },
        can: {
          edit: true,
          correct: false,
          edit_amount: false,
          draw: false,
          sign: [],
          states: {
            status: [
              {
                name: "approved",
                label: "Approved",
                locks: true,
                reason: null,
                allowed: true,
                why_not: null,
                guard: null,
                signed: false,
              },
              {
                name: "in_review",
                label: "In review",
                locks: false,
                reason: null,
                allowed: false,
                why_not: "It's in review already.",
                guard: "allowed_move",
                signed: false,
              },
            ],
          },
        },
      }),
    );
    answer(
      `POST /api/v1/entities/${LOT_ID}/states/status`,
      problem(409, "guard_refused", FOUR_EYES, undefined, { guard: "four_eyes" }),
    );
    const { user } = await renderApp("/entities/LOT-000001/");
    const states = await screen.findByRole("region", { name: "States" });
    await user.click(within(states).getByRole("button", { name: "Set Status" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Set Status of LOT-000001" });
    expect(
      within(dialog).getByRole("radio", { name: "In review: It's in review already." }),
    ).toBeDisabled();
    await user.click(within(dialog).getByRole("button", { name: "Set Status" }));
    expect(await within(dialog).findByText(FOUR_EYES)).toBeInTheDocument();
  });
});

describe("finding what rules caught", () => {
  // The grid draws only the rows in view: give it a view.
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

  const HELD: Schemas["HoldOut"] = {
    id: HOLD_ID,
    rule: "purity_in_specification",
    label: "Purity in specification",
    message: "Its purity, 88.5%, is under 90%.",
    reviewed_by: ["acme/lab:qa"],
    held_by: ALICE,
    agent: null,
    held_at: "2026-11-18T09:00:00Z",
    reason: "Retested.",
    revision: 2,
    last_revision: 2,
    occurrences: [OBSERVATION],
    review_version: "a".repeat(64),
    can_review: true,
    why_not: null,
  };
  const BROKEN = {
    rule: "expires_after_made",
    label: "Expires after it's made",
    message: "It expires on 2026-10-01, before it's made.",
    found_at: "2026-11-18T09:00:00Z",
  };

  it("reads is:held, is:breaking and a rule's name in the search box", () => {
    expect(searchWords("is:held lot 7")).toEqual({
      text: "lot 7",
      conditions: [{ field: "held", op: "is_set", value: true }],
    });
    expect(
      searchWords("is:breaking held:purity_in_specification breaks:acme/x:named").conditions,
    ).toEqual([
      { field: "breaks", op: "is_set", value: true },
      { field: "held", op: "eq", value: "purity_in_specification" },
      { field: "breaks", op: "eq", value: "acme/x:named" },
    ]);
    // Anything else after is: is text.
    expect(searchWords("is:open").text).toBe("is:open");
  });

  it("offers the rules that can catch a type's records, each way", () => {
    const fields = filterable(LOT);
    const held = fields.find((f) => f.name === "held");
    const breaks = fields.find((f) => f.name === "breaks");
    expect(held?.values).toEqual(["purity_in_specification"]);
    // The database's own rules can't be broken by what's saved.
    expect(breaks?.values).toEqual(["expires_after_made"]);
    // Without a type, by any rule.
    expect(
      filterable(undefined)
        .filter((f) => f.type === "rule")
        .map((f) => f.values),
    ).toEqual([[], []]);
    // A type without rules has nothing to find.
    expect(filterable({ ...LOT, rules: [] }).some((f) => f.type === "rule")).toBe(false);
  });

  it("says by each record which rule caught it, and why", async () => {
    onLab();
    answer(
      "POST /api/v1/entities/search",
      page([
        lot({ holds: [HELD] }),
        lot({ id: `${LOT_ID.slice(0, -1)}2`, hrid: "LOT-000002", breaches: [BROKEN] }),
      ]),
    );
    await renderApp("/projects/kinase-atlas/entities/?type=acme/lab.Lot&q=is%3Aheld");
    const grid = await screen.findByRole("table", { name: "2 lots match" });
    expect(within(grid).getByRole("columnheader", { name: "Rules" })).toBeInTheDocument();
    const rows = within(grid).getAllByRole("row");
    expect(nth(rows, 1)).toHaveTextContent(
      "Held for review: Purity in specificationIts purity, 88.5%, is under 90%.",
    );
    expect(nth(rows, 2)).toHaveTextContent("Breaks: Expires after it's made");
    expect(calls("POST", "/api/v1/entities/search").at(-1)?.body).toMatchObject({
      type: "acme/lab.Lot",
      // The search box's words, as a group of their own.
      where: { all: [{ all: [{ field: "held", op: "is_set", value: true }] }] },
    });
  });

  it("filters by a rule, by its label, and shows no Rules column otherwise", async () => {
    onLab();
    answer("POST /api/v1/entities/search", page([lot({ holds: [HELD] })]));
    const { user } = await renderApp("/projects/kinase-atlas/entities/?type=acme/lab.Lot");
    const grid = await screen.findByRole("table", { name: "1 lot" });
    expect(within(grid).queryByRole("columnheader", { name: "Rules" })).not.toBeInTheDocument();
    const editor = await addFilter(user, "Held for review");
    const condition = within(editor).getByRole("combobox", { name: "Filter 1: condition" });
    expect(
      within(condition)
        .getAllByRole("option")
        .map((o) => o.textContent),
    ).toEqual(["by any rule", "by the rule", "by no rule"]);
    await user.selectOptions(condition, "by the rule");
    await user.selectOptions(
      within(editor).getByRole("combobox", { name: "Filter 1: value" }),
      "Purity in specification",
    );
    await waitFor(() => {
      expect(calls("POST", "/api/v1/entities/search").at(-1)?.body).toMatchObject({
        where: { all: [{ field: "held", op: "eq", value: "purity_in_specification" }] },
      });
    });
    await user.click(within(editor).getByRole("button", { name: "Done" }));
    expect(
      await screen.findByRole("columnheader", { name: "Rules" }, { timeout: 3000 }),
    ).toBeInTheDocument();
  });
});
