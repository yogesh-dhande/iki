/**
 * A record's amount (ADR 0139) on its page: how much there is, drawing and splitting it,
 * a hold, and its ledger, against a fake API.
 */

import type { Schemas } from "@ikisci/sdk";
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { entity, field, PROTEIN, renderApp, withTypes } from "./entity-fixtures";
import { answer, calls, ME, page, problem, respond } from "./test-server";

const SAMPLE_ID = "00000000-0000-7000-8000-00000000a501";
const DRAW_PATH = `/api/v1/entities/${SAMPLE_ID}/amount/draw`;
const DRAW = `POST ${DRAW_PATH}`;
const SPLIT = `POST /api/v1/entities/${SAMPLE_ID}/aliquots`;
const ADJUST = `POST /api/v1/entities/${SAMPLE_ID}/amount/adjust`;

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
    whole_units: false,
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
      correct: false,
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
      used_in: null,
      confirm: "",
    });
    expect(await screen.findByText("SMP-000001 has 237.5 uL now.")).toBeInTheDocument();
  });

  it("records what a draw went into, once the tube's own label is scanned to confirm it", async () => {
    const tube: Schemas["EntityTypeOut"] = {
      ...SAMPLE,
      storage: { holds: [], grid: null, barcode: "label", label: null, confirm: ["draw"] },
    };
    onSample();
    answer("GET /api/v1/entity-types", [PROTEIN, tube]);
    answer(`GET /api/v1/entities/${SAMPLE_ID}/location`, {
      path: [],
      hidden: false,
      stored: false,
      position: null,
      custodian: null,
      custody_since: null,
    });
    answer(`GET /api/v1/entities/${SAMPLE_ID}/location/events`, page([]));
    const experiment = entity({
      id: "00000000-0000-7000-8000-00000000e0e1",
      hrid: "EXP-000003",
      title: "Kinase assay",
    });
    answer("GET /api/v1/entities", page([experiment]));
    const { user } = await renderApp("/entities/SMP-000001/");
    const card = await screen.findByRole("region", { name: "Volume" });
    await user.click(within(card).getByRole("button", { name: "Draw" }));
    const dialog = await screen.findByRole("alertdialog");
    const draw = within(dialog).getByRole("button", { name: "Draw" });
    await user.type(within(dialog).getByRole("textbox", { name: "How much" }), "5");
    expect(draw).toHaveAttribute("aria-disabled", "true");
    await user.click(within(dialog).getByRole("combobox", { name: /^Used in/ }));
    await user.click(await screen.findByRole("option", { name: /EXP-000003/ }));
    await user.type(within(dialog).getByRole("textbox", { name: "Scan its label" }), "S-1");
    expect(draw).not.toHaveAttribute("aria-disabled");
    answer(DRAW, respond(200, { amount: amount("245"), movement: movement("drawn", "-5", "245") }));
    await user.click(draw);
    expect(await screen.findByText("SMP-000001 has 245 uL now.")).toBeInTheDocument();
    expect(calls("POST", `/api/v1/entities/${SAMPLE_ID}/amount/draw`).at(-1)?.body).toEqual({
      amount: { value: "5", unit: "uL" },
      purpose: "routine",
      reason: "",
      used_in: experiment.id,
      confirm: "S-1",
    });
  });

  it("confirms a draw by the Iki ID printed on the tube, for a label it shares", async () => {
    const tube: Schemas["EntityTypeOut"] = {
      ...SAMPLE,
      storage: { holds: [], grid: null, barcode: "label", label: null, confirm: ["draw"] },
    };
    onSample();
    answer("GET /api/v1/entity-types", [PROTEIN, tube]);
    answer(`GET /api/v1/entities/${SAMPLE_ID}/location`, {
      path: [],
      hidden: false,
      stored: false,
      position: null,
      custodian: null,
      custody_since: null,
    });
    answer(`GET /api/v1/entities/${SAMPLE_ID}/location/events`, page([]));
    const refused =
      "This scan can’t identify a tube here. Use its printed Iki ID or ask an authorized reviewer.";
    answer(DRAW, () =>
      problem(422, "validation_failed", "Some fields are invalid.", [
        { pointer: "#/confirm", detail: refused },
      ]),
    );
    const { user } = await renderApp("/entities/SMP-000001/");
    const card = await screen.findByRole("region", { name: "Volume" });
    await user.click(within(card).getByRole("button", { name: "Draw" }));
    const dialog = await screen.findByRole("alertdialog");
    await user.type(within(dialog).getByRole("textbox", { name: "How much" }), "5");
    await user.type(within(dialog).getByRole("textbox", { name: "Scan its label" }), "SHARED-1");
    await user.click(within(dialog).getByRole("button", { name: "Draw" }));
    expect(await within(dialog).findByText(refused)).toBeInTheDocument();
    expect(calls("POST", DRAW_PATH).at(-1)?.body).toMatchObject({ confirm: "SHARED-1" });
    await user.click(within(dialog).getByRole("radio", { name: /^Type its printed Iki ID/ }));
    // A typed Iki ID it refuses is said in words that don't tell a missing one apart.
    await user.type(within(dialog).getByRole("textbox", { name: "Iki ID" }), "SMP-000099");
    await user.click(within(dialog).getByRole("button", { name: "Draw" }));
    expect(
      await within(dialog).findByText(
        "No tube you can open has this Iki ID. Check the ID, or ask an authorized reviewer.",
      ),
    ).toBeInTheDocument();
    expect(within(dialog).queryByText(refused)).toBeNull();
    await user.clear(within(dialog).getByRole("textbox", { name: "Iki ID" }));
    await user.type(within(dialog).getByRole("textbox", { name: "Iki ID" }), "SMP-000001");
    answer(DRAW, respond(200, { amount: amount("245"), movement: movement("drawn", "-5", "245") }));
    await user.click(within(dialog).getByRole("button", { name: "Draw" }));
    expect(await screen.findByText("SMP-000001 has 245 uL now.")).toBeInTheDocument();
    expect(calls("POST", DRAW_PATH).at(-1)?.body).toMatchObject({
      confirm: { code: "SMP-000001", mode: "iki_id" },
    });
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
      used_in: null,
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
        can: {
          edit: false,
          correct: false,
          edit_amount: false,
          draw: false,
          states: {},
          sign: [],
        },
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

  it("counts against the balance it showed, and records a first count as unrecorded", async () => {
    onSample();
    const { user } = await renderApp("/entities/SMP-000001/");
    const card = await screen.findByRole("region", { name: "Volume" });
    await user.click(within(card).getByRole("button", { name: "Adjust" }));
    const dialog = await screen.findByRole("alertdialog", {
      name: "Adjust SMP-000001’s volume",
    });
    expect(dialog).toHaveTextContent("It had 250 uL when this count began.");
    answer(ADJUST, { amount: amount("240"), movement: movement("adjusted", "-10", "240") });
    await user.type(within(dialog).getByRole("textbox", { name: "It has" }), "240");
    await user.type(within(dialog).getByRole("textbox", { name: "Reason" }), "Recount");
    await user.click(within(dialog).getByRole("button", { name: "Adjust" }));
    expect(await screen.findByText("SMP-000001 has 240 uL now.")).toBeInTheDocument();
    const [sent] = calls("POST", `/api/v1/entities/${SAMPLE_ID}/amount/adjust`);
    expect(sent?.headers.get("If-Balance-Revision")).toBe("2026-11-18T10:00:00Z");
    expect(sent?.body).toEqual({ amount: { value: "240", unit: "uL" }, reason: "Recount" });
  });

  it("sends unrecorded for a first count", async () => {
    onSample(sample({ amount: null }));
    const { user } = await renderApp("/entities/SMP-000001/");
    const card = await screen.findByRole("region", { name: "Volume" });
    await user.click(within(card).getByRole("button", { name: "Record volume" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Record SMP-000001’s volume" });
    answer(ADJUST, { amount: amount("100"), movement: movement("recorded", "100", "100") });
    await user.type(within(dialog).getByRole("textbox", { name: "It has" }), "100");
    await user.type(within(dialog).getByRole("textbox", { name: "Reason" }), "First count");
    await user.click(within(dialog).getByRole("button", { name: "Record" }));
    expect(await screen.findByText("SMP-000001 has 100 uL now.")).toBeInTheDocument();
    expect(
      calls("POST", `/api/v1/entities/${SAMPLE_ID}/amount/adjust`)[0]?.headers.get(
        "If-Balance-Revision",
      ),
    ).toBe("unrecorded");
  });

  it("shows what changed a stale count, keeps it, and records it only once recounted", async () => {
    // Ines drew 20 uL after this count began, within the same millisecond as the balance
    // it began from: only the microseconds tell them apart. Each movement is written just
    // after the balance it made.
    const began = "2026-11-18T10:00:00.000100+00:00";
    const now = amount("230", { updated_at: "2026-11-18T10:00:00.000400+00:00" });
    onSample(sample({ amount: amount("250", { updated_at: began }) }));
    const { user } = await renderApp("/entities/SMP-000001/");
    const card = await screen.findByRole("region", { name: "Volume" });
    await user.click(within(card).getByRole("button", { name: "Adjust" }));
    const dialog = await screen.findByRole("alertdialog", {
      name: "Adjust SMP-000001’s volume",
    });
    answer(`GET /api/v1/entities/${SAMPLE_ID}/amount`, now);
    answer(`GET /api/v1/entities/${SAMPLE_ID}/amount/movements`, {
      ...page([
        movement("drawn", "-20", "230", {
          id: "00000000-0000-7000-8000-00000000d002",
          created_at: "2026-11-18T10:00:00.000450+00:00",
          reason: "Western blot",
          user: { ...ME, name: "Ines" },
        }),
        movement("recorded", "250", "250", { created_at: "2026-11-18T10:00:00.000150+00:00" }),
      ]),
      refs: {},
    });
    answer(
      ADJUST,
      problem(
        409,
        "stale_balance",
        "The stock balance changed. Refresh it and recount before submitting.",
      ),
    );
    const count = within(dialog).getByRole("textbox", { name: "It has" });
    await user.type(count, "245");
    await user.type(within(dialog).getByRole("textbox", { name: "Reason" }), "Recount");
    await user.click(within(dialog).getByRole("button", { name: "Adjust" }));

    const stale = await within(dialog).findByRole("alert");
    expect(stale).toHaveTextContent(
      "Your count wasn’t recorded: the volume changed after you started counting.",
    );
    expect(
      await within(stale).findByText("It has 230 uL now, not the 250 uL you started from."),
    ).toBeInTheDocument();
    const since = within(stale).getByRole("list", { name: "Changed since you started" });
    expect(within(since).getAllByRole("listitem")).toHaveLength(1);
    expect(within(since).getByRole("listitem")).toHaveTextContent(
      /^Drawn: −20 uL, by Ines, .+ \(“Western blot”\)\.$/,
    );
    expect(count).toHaveValue("245");
    expect(within(dialog).getByRole("button", { name: "Adjust" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );

    await user.click(within(stale).getByRole("button", { name: "Recount" }));
    expect(within(dialog).queryByRole("alert")).not.toBeInTheDocument();
    expect(dialog).toHaveTextContent("It had 230 uL when this count began.");
    expect(count).toHaveFocus();
    expect(count).toHaveValue("245");
    expect(calls("POST", `/api/v1/entities/${SAMPLE_ID}/amount/adjust`)).toHaveLength(1);

    answer(ADJUST, { amount: amount("228"), movement: movement("adjusted", "-2", "228") });
    await user.clear(count);
    await user.type(count, "228");
    await user.click(within(dialog).getByRole("button", { name: "Adjust" }));
    expect(await screen.findByText("SMP-000001 has 228 uL now.")).toBeInTheDocument();
    const adjusts = calls("POST", `/api/v1/entities/${SAMPLE_ID}/amount/adjust`);
    expect(adjusts.map((c) => c.headers.get("If-Balance-Revision"))).toEqual([
      began,
      now.updated_at,
    ]);
    expect(adjusts[1]?.body).toEqual({ amount: { value: "228", unit: "uL" }, reason: "Recount" });
  });

  it("holds Draw still while its box's map comes above it, and draws the map once it's let go", async () => {
    const tube: Schemas["EntityTypeOut"] = {
      ...SAMPLE,
      storage: { holds: [], grid: null, barcode: "label", label: null, confirm: [] },
    };
    const box: Schemas["StepOut"] = {
      id: "00000000-0000-7000-8000-0000000b0001",
      hrid: "BOX-000014",
      title: "Kinase retains",
      type: "acme/lims.Box",
      archived: false,
      purged: false,
      position: "A1",
    };
    onSample();
    answer("GET /api/v1/entity-types", [PROTEIN, tube]);
    answer(`GET /api/v1/entities/${SAMPLE_ID}/location`, {
      path: [box],
      hidden: false,
      stored: true,
      position: "A1",
      custodian: null,
      custody_since: null,
    });
    answer(`GET /api/v1/entities/${SAMPLE_ID}/location/events`, page([]));
    let release = (): void => undefined;
    const held = new Promise<void>((done) => {
      release = done;
    });
    const map: Schemas["ContainerOut"] = {
      id: box.id,
      hrid: box.hrid,
      title: box.title,
      type: box.type,
      archived: false,
      purged: false,
      project: "00000000-0000-7000-8000-0000000000c1",
      shape: { rows: 2, columns: 2, labels: "letter_number", order: "by_row", size: 4 },
      holds: ["acme/lims.Sample"],
      code: box.hrid,
      location: {
        path: [],
        hidden: false,
        stored: false,
        position: null,
        custodian: null,
        custody_since: null,
      },
      contents: [
        {
          id: SAMPLE_ID,
          ref: {
            hrid: "SMP-000001",
            title: "SMP-000001",
            type: "acme/lims.Sample",
            archived: false,
            purged: false,
          },
          row: 1,
          column: 1,
          position: "A1",
          code: "S-1",
          custodian: null,
          custody_since: null,
          container: false,
          count: 0,
        },
      ],
      free: 3,
      reconciliation: null,
      can: { keep: true, label: true },
    };
    answer("GET /api/v1/storage/containers/BOX-000014", async () => {
      await held;
      return map;
    });
    await renderApp("/entities/SMP-000001/");
    const card = await screen.findByRole("region", { name: "Volume" });
    await screen.findByRole("navigation", { name: "Where SMP-000001 is" });
    const positions = { name: "Kinase retains’s positions, with SMP-000001’s marked" };
    // A finger on Draw as the map's read answers: the map, above it, waits for the release.
    fireEvent.pointerDown(within(card).getByRole("button", { name: "Draw" }));
    await act(async () => {
      release();
      await held;
      await new Promise((done) => setTimeout(done, 50));
    });
    expect(calls("GET", "/api/v1/storage/containers/BOX-000014")).toHaveLength(1);
    expect(screen.queryByRole("grid", positions)).not.toBeInTheDocument();
    fireEvent.pointerUp(window);
    expect(await screen.findByRole("grid", positions)).toBeInTheDocument();
  });
});
