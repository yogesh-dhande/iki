/**
 * A record's amount (ADR 0139) on its page: how much there is, drawing and splitting it,
 * a hold, and its ledger, against a fake API.
 */

import type { Schemas } from "@ikisci/sdk";
import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { entity, field, PROTEIN, renderApp, withTypes } from "./entity-fixtures";
import { answer, calls, page, problem, respond } from "./test-server";

const SAMPLE_ID = "00000000-0000-7000-8000-00000000a501";
const DRAW = `POST /api/v1/entities/${SAMPLE_ID}/amount/draw`;
const SPLIT = `POST /api/v1/entities/${SAMPLE_ID}/aliquots`;

const LABEL = field({
  name: "label",
  label: "Label",
  type: "string",
  required: true,
  unique: true,
});
const CUSTODY = field({
  name: "custody",
  label: "Custody",
  type: "state",
  initial: "in_lab",
  states: [
    {
      name: "in_lab",
      label: "In the lab",
      description: null,
      set_by: [],
      locks: false,
      holds_amount: false,
      reason: null,
      signatures: [],
    },
    {
      name: "quarantined",
      label: "Quarantined",
      description: null,
      set_by: [],
      locks: true,
      holds_amount: true,
      reason: "required",
      signatures: [],
    },
  ],
});

const SAMPLE: Schemas["EntityTypeOut"] = {
  ...PROTEIN,
  name: "acme/lims.Sample",
  plugin: "acme/lims",
  label: "Sample",
  label_plural: "Samples",
  description: null,
  hrid_prefix: "SMP",
  identity: [],
  fields: [LABEL, CUSTODY],
  amount: {
    unit: "uL",
    label: "Volume",
    description: null,
    display_unit: null,
    concentration: "concentration",
    aliquots: "acme/lims.Sample",
    aliquots_of: null,
    material: null,
  },
};

function amount(value: string, more: Partial<Schemas["AmountOut"]> = {}): Schemas["AmountOut"] {
  return {
    value,
    unit: "uL",
    label: "Volume",
    display: null,
    held_by: null,
    updated_at: "2026-11-18T10:00:00Z",
    ...more,
  };
}

function movement(
  kind: Schemas["MovementOut"]["kind"],
  change: string,
  balance: string,
  more: Partial<Schemas["MovementOut"]> = {},
): Schemas["MovementOut"] {
  return {
    id: `00000000-0000-7000-8000-0000000${kind.length.toString().padStart(5, "0")}`,
    kind,
    change,
    balance,
    unit: "uL",
    given: null,
    counterpart: null,
    used_in: null,
    source: null,
    confirmed: "",
    operation: "00000000-0000-7000-8000-000000000001",
    user: {
      id: "00000000-0000-7000-8000-000000000002",
      name: "Ada",
      email: "ada@example.com",
      active: true,
      kind: "person",
      imported_from: "",
    },
    via: "web",
    client: "",
    agent: null,
    reason: "",
    purpose: kind === "drawn" ? "routine" : null,
    created_at: "2026-11-18T10:00:00Z",
    ...more,
  };
}

function sample(overrides: Partial<Schemas["EntityOut"]> = {}): Schemas["EntityOut"] {
  return entity({
    id: SAMPLE_ID,
    hrid: "SMP-000001",
    type: "acme/lims.Sample",
    title: "SMP-000001",
    values: { label: "S-1", custody: "in_lab" },
    amount: amount("250"),
    can: {
      edit: true,
      edit_amount: true,
      draw: true,
      draw_purposes: ["routine"],
      states: {},
      sign: [],
    },
    draw_refusals: {},
    ...overrides,
  });
}

function onSample(found: Schemas["EntityOut"] = sample()): void {
  withTypes();
  answer("GET /api/v1/entity-types", [PROTEIN, SAMPLE]);
  answer("GET /api/v1/entities/SMP-000001", found);
  answer("GET /api/v1/entities/SMP-000001/provenance", {});
  answer("GET /api/v1/entities/SMP-000001/projects", []);
  answer(`GET /api/v1/entities/${SAMPLE_ID}/amount/movements`, {
    ...page([
      movement("drawn", "-25", "250", {
        given: { value: "50", unit: "ug" },
        purpose: "qa_sampling",
        reason: "SEC",
      }),
      movement("recorded", "275", "275"),
    ]),
    refs: {},
  });
}

describe("a record's amount", () => {
  it("shows how much there is, and its ledger", async () => {
    onSample();
    const { user } = await renderApp("/entities/SMP-000001/");
    const card = await screen.findByRole("region", { name: "Volume" });
    expect(within(card).getByText("250 uL", { selector: ".amount-value" })).toBeInTheDocument();
    await user.click(within(card).getByText("Its ledger"));
    const ledger = await within(card).findByRole("table");
    const rows = within(ledger).getAllByRole("row");
    expect(rows[1]).toHaveTextContent("Drawn");
    expect(rows[1]).toHaveTextContent("QA sampling");
    expect(rows[1]).toHaveTextContent("−25 uL50 ug");
    expect(rows[1]).toHaveTextContent("SEC");
    expect(rows[2]).toHaveTextContent("+275 uL");
  });

  it("draws some, and says what Iki says when it hasn't that much", async () => {
    onSample();
    const { user } = await renderApp("/entities/SMP-000001/");
    const card = await screen.findByRole("region", { name: "Volume" });
    await user.click(within(card).getByRole("button", { name: "Draw" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Draw from SMP-000001" });
    expect(within(dialog).getByRole("option", { name: "Routine use" })).toBeInTheDocument();
    expect(within(dialog).queryByRole("option", { name: "QA sampling" })).not.toBeInTheDocument();
    answer(
      DRAW,
      problem(422, "validation_failed", "The amount can't change so.", [
        { pointer: "#/amount", detail: "is more than SMP-000001 has: 300 uL of 250 uL." },
      ]),
    );
    await user.type(within(dialog).getByRole("textbox", { name: "How much" }), "300");
    await user.click(within(dialog).getByRole("button", { name: "Draw" }));
    expect(
      await within(dialog).findByText("is more than SMP-000001 has: 300 uL of 250 uL."),
    ).toBeInTheDocument();

    answer(
      DRAW,
      respond(200, { amount: amount("237.5"), movement: movement("drawn", "-12.5", "237.5") }),
    );
    const much = within(dialog).getByRole("textbox", { name: "How much" });
    await user.clear(much);
    await user.type(much, "12.5");
    await user.type(within(dialog).getByRole("textbox", { name: /^Reason/ }), "SEC");
    await user.click(within(dialog).getByRole("button", { name: "Draw" }));
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    });
    expect(calls("POST", `/api/v1/entities/${SAMPLE_ID}/amount/draw`).at(-1)?.body).toEqual({
      amount: { value: "12.5", unit: "uL" },
      purpose: "routine",
      reason: "SEC",
      confirm: "",
    });
    expect(await screen.findByText("SMP-000001 has 237.5 uL now.")).toBeInTheDocument();
  });

  it("explains a due retest on the page and in the dialog, and allows a QA sample", async () => {
    const retest = "Retest due. Only QA sampling is allowed.";
    onSample(
      sample({
        locked: true,
        can: { ...sample().can, edit: false, draw_purposes: ["routine", "qa_sampling"] },
        draw_refusals: { routine: retest },
      }),
    );
    const { user } = await renderApp("/entities/SMP-000001/");
    const card = await screen.findByRole("region", { name: "Volume" });
    expect(within(card).getByText(retest)).toBeInTheDocument();
    await user.click(within(card).getByRole("button", { name: "Draw" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText(retest)).toBeInTheDocument();
    const draw = within(dialog).getByRole("button", { name: "Draw" });
    expect(draw).toHaveAttribute("aria-disabled", "true");

    answer(
      DRAW,
      respond(200, {
        amount: amount("248"),
        movement: movement("drawn", "-2", "248", { purpose: "qa_sampling" }),
      }),
    );
    await user.selectOptions(
      within(dialog).getByRole("combobox", { name: "Purpose" }),
      "QA sampling",
    );
    expect(draw).not.toHaveAttribute("aria-disabled");
    expect(within(dialog).queryByText(retest)).not.toBeInTheDocument();
    await user.type(within(dialog).getByRole("textbox", { name: "How much" }), "2");
    await user.click(draw);
    expect(await screen.findByText("SMP-000001 has 248 uL now.")).toBeInTheDocument();
    expect(calls("POST", `/api/v1/entities/${SAMPLE_ID}/amount/draw`).at(-1)?.body).toEqual({
      amount: { value: "2", unit: "uL" },
      purpose: "qa_sampling",
      reason: "",
      confirm: "",
    });
  });

  it("explains an expired lot even when QA sampling is selected", async () => {
    const expired = "This lot has reached its expiry date.";
    onSample(
      sample({
        can: { ...sample().can, draw_purposes: ["routine", "qa_sampling"] },
        draw_refusals: { routine: expired, qa_sampling: expired },
      }),
    );
    const { user } = await renderApp("/entities/SMP-000001/");
    const card = await screen.findByRole("region", { name: "Volume" });
    expect(within(card).getByText(expired)).toBeInTheDocument();
    await user.click(within(card).getByRole("button", { name: "Draw" }));
    const dialog = await screen.findByRole("alertdialog");
    await user.selectOptions(
      within(dialog).getByRole("combobox", { name: "Purpose" }),
      "QA sampling",
    );
    expect(within(dialog).getByText(expired)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Draw" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
  });

  it("disables only the draw when the caller has no authorized purpose", async () => {
    onSample(sample({ can: { ...sample().can, draw_purposes: [] } }));
    await renderApp("/entities/SMP-000001/");
    const card = await screen.findByRole("region", { name: "Volume" });
    expect(within(card).getByRole("button", { name: "Draw" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    expect(within(card).getByRole("button", { name: "Transfer" })).not.toHaveAttribute(
      "aria-disabled",
    );
  });

  it("refreshes the available purposes if QA sampling permission is revoked", async () => {
    onSample(sample({ can: { ...sample().can, draw_purposes: ["qa_sampling"] } }));
    const { user } = await renderApp("/entities/SMP-000001/");
    const card = await screen.findByRole("region", { name: "Volume" });
    await user.click(within(card).getByRole("button", { name: "Draw" }));
    const dialog = await screen.findByRole("alertdialog");
    const purpose = within(dialog).getByRole("combobox", { name: "Purpose" });
    expect(purpose).toHaveValue("qa_sampling");
    await user.type(within(dialog).getByRole("textbox", { name: "How much" }), "2");
    answer("GET /api/v1/entities/SMP-000001", sample());
    const denied = "QA sampling requires the QA role.";
    answer(DRAW, problem(403, "forbidden", denied));
    const draw = within(dialog).getByRole("button", { name: "Draw" });
    await user.click(draw);
    expect(await within(dialog).findByText(denied)).toBeInTheDocument();
    await waitFor(() => {
      expect(within(dialog).queryByRole("option", { name: "QA sampling" })).not.toBeInTheDocument();
    });
    expect(draw).toHaveAttribute("aria-disabled", "true");
    await user.selectOptions(purpose, "Routine use");
    expect(draw).not.toHaveAttribute("aria-disabled");
  });

  it("shows the date warning to a reader even before an amount is recorded", async () => {
    const expired = "This lot has reached its expiry date.";
    onSample(
      sample({
        amount: null,
        can: { edit: false, edit_amount: false, draw: false, states: {}, sign: [] },
        draw_refusals: { routine: expired, qa_sampling: expired },
      }),
    );
    await renderApp("/entities/SMP-000001/");
    const card = await screen.findByRole("region", { name: "Volume" });
    expect(within(card).getByText(expired)).toBeInTheDocument();
    expect(within(card).queryByRole("button", { name: "Draw" })).not.toBeInTheDocument();
  });

  it("splits it into aliquots, each with a label of its own", async () => {
    onSample();
    const { user } = await renderApp("/entities/SMP-000001/");
    const card = await screen.findByRole("region", { name: "Volume" });
    await user.click(within(card).getByRole("button", { name: "Split into aliquots" }));
    const dialog = await screen.findByRole("alertdialog", {
      name: "Split SMP-000001 into aliquots",
    });
    const ref = (hrid: string) => ({
      hrid,
      title: hrid,
      type: SAMPLE.name,
      archived: false,
      purged: false,
    });
    answer(
      SPLIT,
      respond(201, {
        amount: amount("210"),
        movement: movement("aliquoted", "-40", "210"),
        aliquots: [ref("SMP-000002"), ref("SMP-000003")],
        ids: ["00000000-0000-7000-8000-00000000a502", "00000000-0000-7000-8000-00000000a503"],
      }),
    );
    await user.type(within(dialog).getByRole("textbox", { name: "Each holds" }), "20");
    await user.type(
      within(dialog).getByRole("textbox", { name: "Each aliquot’s label" }),
      "S-1.1{Enter}S-1.2",
    );
    await user.click(within(dialog).getByRole("button", { name: "Split" }));
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    });
    expect(calls("POST", `/api/v1/entities/${SAMPLE_ID}/aliquots`).at(-1)?.body).toEqual({
      count: 2,
      each: { value: "20", unit: "uL" },
      values: [{ label: "S-1.1" }, { label: "S-1.2" }],
      reason: "",
    });
    expect(
      await screen.findByText("Split SMP-000001 into SMP-000002, SMP-000003: 210 uL left."),
    ).toBeInTheDocument();
  });

  it("says a hold stops it being taken, and still lets it be adjusted", async () => {
    onSample(
      sample({
        values: { label: "S-1", custody: "quarantined" },
        amount: amount("250", { held_by: "custody" }),
      }),
    );
    await renderApp("/entities/SMP-000001/");
    const card = await screen.findByRole("region", { name: "Volume" });
    const draw = within(card).getByRole("button", { name: "Draw" });
    expect(draw).toHaveAttribute("aria-disabled", "true");
    expect(draw).toHaveAccessibleDescription(
      "Held: its custody is Quarantined, so none of it can be drawn, transferred or split off until that changes.",
    );
    expect(within(card).getByRole("button", { name: "Adjust" })).not.toHaveAttribute(
      "aria-disabled",
    );
  });
});
