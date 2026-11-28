/**
 * A record's state fields (ADR 0125) on its page, in its history and in a project's grid,
 * against a fake API.
 */

import type { Schemas } from "@ikisci/sdk";
import { screen, waitFor, within } from "@testing-library/react";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import {
  addFilter,
  entity,
  field,
  KINASE,
  moreActions,
  nth,
  PROTEIN,
  renderApp,
  revision,
  withTypes,
} from "./entity-fixtures";
import { queryClient } from "./api";
import { answer, calls, page, problem, respond, stale } from "./test-server";

const SAMPLE_ID = "00000000-0000-7000-8000-00000000e501";
const POST = `POST /api/v1/entities/${SAMPLE_ID}/states/status`;

function state(
  name: string,
  label: string,
  more: Partial<Schemas["StateOut"]> = {},
): Schemas["StateOut"] {
  return {
    name,
    label,
    description: null,
    set_by: ["iki/lims.analyst"],
    locks: false,
    holds_amount: false,
    reason: null,
    signatures: [],
    ...more,
  };
}

const STATUS = field({
  name: "status",
  label: "Status",
  type: "state",
  plugin: "iki/lims",
  initial: "received",
  states: [
    state("received", "Received"),
    state("in_testing", "In testing"),
    state("reviewed", "Reviewed", { set_by: ["iki/lims.reviewer"], locks: true }),
  ],
});
const CUSTODY = field({
  name: "custody",
  label: "Custody",
  type: "state",
  plugin: "iki/lims",
  initial: "in_lab",
  states: [
    state("in_lab", "In the lab", { set_by: ["iki/lims.qa"] }),
    state("quarantined", "Quarantined", {
      set_by: ["iki/lims.qa"],
      locks: true,
      reason: "required",
    }),
  ],
});

const SAMPLE: Schemas["EntityTypeOut"] = {
  ...PROTEIN,
  name: "iki/lims.Sample",
  plugin: "iki/lims",
  label: "Sample",
  label_plural: "Samples",
  description: null,
  hrid_prefix: "SMP",
  identity: [],
  fields: [...PROTEIN.fields.slice(0, 1), STATUS, CUSTODY],
};

const ROLE = "It needs someone with the QA role in Kinase Atlas.";

function can(
  name: string,
  label: string,
  more: Partial<Schemas["StateCan"]> = {},
): Schemas["StateCan"] {
  return {
    name,
    label,
    locks: false,
    reason: null,
    allowed: true,
    why_not: null,
    signed: false,
    ...more,
  };
}

function sample(overrides: Partial<Schemas["EntityOut"]> = {}): Schemas["EntityOut"] {
  return entity({
    id: SAMPLE_ID,
    hrid: "SMP-000001",
    type: "iki/lims.Sample",
    title: "SMP-000001",
    revision: 2,
    values: { name: "Lot 7", status: "in_testing", custody: "in_lab" },
    locked: false,
    locked_by: [],
    can: {
      edit: true,
      correct: false,
      edit_amount: false,
      draw: false,
      sign: [],
      states: {
        status: [
          can("received", "Received", { reason: "required" }),
          can("reviewed", "Reviewed", {
            locks: true,
            allowed: false,
            why_not: "It needs someone with the Reviewer role in Kinase Atlas.",
          }),
        ],
        custody: [
          can("quarantined", "Quarantined", {
            locks: true,
            reason: "required",
            allowed: false,
            why_not: ROLE,
          }),
        ],
      },
    },
    ...overrides,
  });
}

function onSample(found: Schemas["EntityOut"] = sample()): void {
  withTypes();
  answer("GET /api/v1/entity-types", page([PROTEIN, SAMPLE]));
  answer("GET /api/v1/entities/SMP-000001", found);
  answer("GET /api/v1/entities/SMP-000001/provenance", {});
  answer("GET /api/v1/entities/SMP-000001/projects", page([]));
}

describe("a record's state fields", () => {
  it("shows each one's state, and a control to set it or why not", async () => {
    onSample();
    await renderApp("/entities/SMP-000001/");
    const states = await screen.findByRole("region", { name: "States" });
    const fields = within(states).getByRole("list", { name: "State fields" });
    expect(within(fields).getByText("Status: In testing")).toBeInTheDocument();
    const status = within(fields).getByRole("button", { name: "Set Status" });
    expect(status).not.toHaveAttribute("aria-disabled");
    // Disabled, but focusable, and saying why, when none of its states are this person's.
    const custody = within(fields).getByRole("button", { name: "Set Custody" });
    expect(custody).toHaveAttribute("aria-disabled", "true");
    expect(custody).toHaveAccessibleDescription(ROLE);
    // The header says each, and the values list has them too.
    expect(screen.getByText("In testing", { selector: ".badge" })).toBeInTheDocument();
    expect(screen.getByText("In the lab", { selector: ".badge" })).toBeInTheDocument();
  });

  it("sets a state, offering the ones this person may and saying why not of the rest", async () => {
    onSample();
    const { user } = await renderApp("/entities/SMP-000001/");
    const states = await screen.findByRole("region", { name: "States" });
    await user.click(within(states).getByRole("button", { name: "Set Status" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Set Status of SMP-000001" });
    const options = within(dialog).getByRole("group", { name: "Set it to" });
    expect(within(options).getByRole("radio", { name: "Received" })).toBeChecked();
    const reviewed = within(options).getByRole("radio", {
      name: "Reviewed: It needs someone with the Reviewer role in Kinase Atlas.",
    });
    expect(reviewed).toBeDisabled();
    // Without the reason it asks for, it says so before asking Iki.
    await user.click(within(dialog).getByRole("button", { name: "Set Status" }));
    expect(
      await within(dialog).findByText("Setting its Status to Received needs a reason: say why."),
    ).toBeInTheDocument();
    expect(calls("POST", `/api/v1/entities/${SAMPLE_ID}/states/status`)).toHaveLength(0);
    // Iki's own word on the reason shows by it too.
    answer(
      POST,
      problem(422, "validation_failed", "Setting that state needs more.", [
        { pointer: "#/reason", detail: "Say why in 2000 characters or fewer." },
      ]),
    );
    await user.type(within(dialog).getByRole("textbox", { name: "Reason" }), "Long");
    await user.click(within(dialog).getByRole("button", { name: "Set Status" }));
    expect(
      await within(dialog).findByText("Say why in 2000 characters or fewer."),
    ).toBeInTheDocument();
    await user.clear(within(dialog).getByRole("textbox", { name: "Reason" }));

    answer(
      POST,
      respond(
        200,
        sample({ revision: 3, values: { name: "Lot 7", status: "received", custody: "in_lab" } }),
      ),
    );
    await user.type(within(dialog).getByRole("textbox", { name: "Reason" }), "Wrong lot");
    await user.click(within(dialog).getByRole("button", { name: "Set Status" }));
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    });
    const sent = calls("POST", `/api/v1/entities/${SAMPLE_ID}/states/status`);
    expect(sent.at(-1)?.body).toEqual({ state: "received", reason: "Wrong lot" });
    expect(sent.at(-1)?.headers.get("If-Match")).toBe('"2"');
    expect(await screen.findByText("SMP-000001’s Status is Received now.")).toBeInTheDocument();
  });

  it("says when someone else changed it first, reads it again, and tries with its new revision", async () => {
    onSample();
    // Someone else renamed it while this dialog was open: Iki refuses the old revision.
    const theirs = sample({
      revision: 3,
      values: { name: "Lot 7 (relabelled)", status: "in_testing", custody: "in_lab" },
    });
    answer(POST, () => {
      answer("GET /api/v1/entities/SMP-000001", theirs);
      return stale(3);
    });
    const { user } = await renderApp("/entities/SMP-000001/");
    const states = await screen.findByRole("region", { name: "States" });
    await user.click(within(states).getByRole("button", { name: "Set Status" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Set Status of SMP-000001" });
    await user.type(within(dialog).getByRole("textbox", { name: "Reason" }), "Wrong lot");
    const reads = calls("GET", "/api/v1/entities/SMP-000001").length;
    await user.click(within(dialog).getByRole("button", { name: "Set Status" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "Someone changed it while this was open. The page now shows their change: check it, then try again.",
    );
    // It's read again, so the page behind the dialog shows their change.
    expect(await screen.findByText("Lot 7 (relabelled)")).toBeInTheDocument();
    expect(calls("GET", "/api/v1/entities/SMP-000001").length).toBeGreaterThan(reads);

    answer(
      POST,
      sample({
        revision: 4,
        values: { name: "Lot 7 (relabelled)", status: "received", custody: "in_lab" },
      }),
    );
    await user.click(within(dialog).getByRole("button", { name: "Set Status" }));
    expect(await screen.findByText("SMP-000001’s Status is Received now.")).toBeInTheDocument();
    const sent = calls("POST", `/api/v1/entities/${SAMPLE_ID}/states/status`);
    expect(sent.map((call) => call.headers.get("If-Match"))).toEqual(['"2"', '"3"']);
    expect(sent.at(-1)?.body).toEqual({ state: "received", reason: "Wrong lot" });
  });

  it("says a locked record can't change until its states do, and offers no edit", async () => {
    onSample(
      sample({
        values: { name: "Lot 7", status: "reviewed", custody: "quarantined" },
        locked: true,
        locked_by: ["status", "custody"],
        can: {
          edit: false,
          correct: false,
          edit_amount: false,
          draw: false,
          sign: [],
          states: {
            status: [can("received", "Received"), can("in_testing", "In testing")],
            custody: [can("in_lab", "In the lab", { allowed: false, why_not: ROLE })],
          },
        },
      }),
    );
    const { user } = await renderApp("/entities/SMP-000001/");
    const states = await screen.findByRole("region", { name: "States" });
    expect(
      within(states).getByText(
        "Locked, as its Status is Reviewed and its Custody is Quarantined: its other values can’t change, nor can it be archived, until its Status is set to Received or In testing and its Custody is set to In the lab.",
      ),
    ).toBeInTheDocument();
    // Its state fields stay settable: they're how it's unlocked.
    expect(within(states).getByRole("button", { name: "Set Status" })).not.toHaveAttribute(
      "aria-disabled",
    );
    expect(screen.getByText("Locked", { selector: ".badge" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Edit" })).not.toBeInTheDocument();
    const offered = await moreActions(user, "SMP-000001");
    expect(offered).toContain("Download audit trail (CSV)");
    expect(offered).not.toContain("Archive…");
  });

  it("warns before saving a correction and records why with the edit", async () => {
    const reviewed = sample({
      values: { name: "Lot 7", status: "reviewed", custody: "in_lab" },
      can: { ...sample().can, edit: false, correct: true },
      locked: true,
      locked_by: ["status"],
    });
    onSample(reviewed);
    answer(
      "GET /api/v1/entity-types",
      page([
        {
          ...SAMPLE,
          fields: SAMPLE.fields.map((f) =>
            f.name !== "status"
              ? f
              : {
                  ...f,
                  states: f.states.map((s) =>
                    s.name === "reviewed" ? { ...s, on_edit: "received" } : s,
                  ),
                },
          ),
        },
      ]),
    );
    answer(`PATCH /api/v1/entities/${SAMPLE_ID}`, sample({ revision: 3 }));
    const { user } = await renderApp("/entities/SMP-000001/");
    await user.click(await screen.findByRole("link", { name: "Edit" }));
    const name = await screen.findByRole("textbox", { name: /^Name/ });
    const warning = "Saving these changes will return Status from Reviewed to Received.";
    expect(screen.queryByText(warning)).not.toBeInTheDocument();
    await user.type(name, "B");
    expect(screen.getByRole("alert")).toHaveTextContent(warning);
    expect(calls("PATCH")).toHaveLength(0);
    await user.keyboard("{Backspace}");
    expect(screen.queryByText(warning)).not.toBeInTheDocument();
    await user.type(name, "C");
    const reason = screen.getByRole("textbox", { name: "Reason" });
    expect(reason).toBeRequired();
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(reason).toHaveFocus();
    expect(calls("PATCH")).toHaveLength(0);
    await user.type(reason, "Corrected the lot label.");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await screen.findByText("Saved revision 3 of SMP-000001.");
    const sent = calls("PATCH")[0];
    expect(sent?.body).toEqual({ values: { name: "Lot 7C" }, reason: "Corrected the lot label." });
    expect(sent?.headers.get("If-Match")).toBe('"2"');
  });

  it("asks for a reason before clearing a marked Boolean", async () => {
    const held = sample({ values: { ...sample().values, on_hold: true } });
    onSample(held);
    answer(
      "GET /api/v1/entity-types",
      page([
        PROTEIN,
        {
          ...SAMPLE,
          fields: [
            ...SAMPLE.fields,
            field({ name: "on_hold", label: "On hold", type: "bool", reason_on_clear: true }),
          ],
        },
      ]),
    );
    answer(
      `PATCH /api/v1/entities/${SAMPLE_ID}`,
      sample({ revision: 3, values: { ...held.values, on_hold: false } }),
    );
    const { user } = await renderApp("/entities/SMP-000001/");
    await user.click(await screen.findByRole("link", { name: "Edit" }));
    const flag = await screen.findByRole("group", { name: /^On hold/ });
    const reason = screen.getByRole("textbox", { name: /^Reason/ });
    expect(reason).not.toBeRequired();
    await user.click(within(flag).getByRole("radio", { name: "No" }));
    expect(reason).toBeRequired();
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(reason).toHaveFocus();
    expect(calls("PATCH")).toHaveLength(0);

    await user.type(reason, "Confirmed the specimen identifier.");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await screen.findByText("Saved revision 3 of SMP-000001.");
    expect(calls("PATCH")[0]?.body).toEqual({
      values: { on_hold: false },
      reason: "Confirmed the specimen identifier.",
    });
  });

  it("are described on its type's page", async () => {
    withTypes();
    answer("GET /api/v1/entity-types", page([PROTEIN, SAMPLE]));
    await renderApp("/types/iki/lims.Sample/");
    expect(
      screen.getByText(
        "State, one of: Received (new records start here), In testing, Reviewed (locks it)",
      ),
    ).toBeInTheDocument();
  });

  it("shows state changes in its audit trail", async () => {
    onSample();
    answer(
      "GET /api/v1/entities/SMP-000001/revisions",
      page([
        revision({
          number: 2,
          kind: "state_changed",
          changed_fields: ["status"],
          reason: "Bench 4",
          state: {
            field: { name: "status", label: "Status" },
            from: { name: "received", label: "Received" },
            to: { name: "in_testing", label: "In testing" },
            signatures: [],
          },
        }),
        revision({ number: 1, changed_fields: ["name"] }),
      ]),
    );
    await renderApp("/entities/SMP-000001/audit/");
    expect(
      await screen.findByText(
        "Revision 2: Status set to In testing, from Received by Rosalind Franklin",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("“Bench 4”")).toBeInTheDocument();
  });
});

describe("setting a state while someone else changes the record", () => {
  it("sends nothing from a revision a refetch brought in while it was open, and says so", async () => {
    onSample();
    const { user } = await renderApp("/entities/SMP-000001/");
    const states = await screen.findByRole("region", { name: "States" });
    await user.click(within(states).getByRole("button", { name: "Set Status" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Set Status of SMP-000001" });
    const changed = sample({
      revision: 3,
      values: { name: "Lot 8", status: "in_testing", custody: "in_lab" },
    });
    answer("GET /api/v1/entities/SMP-000001", changed);
    await queryClient.refetchQueries();
    await screen.findAllByText("Lot 8");
    await user.type(within(dialog).getByRole("textbox", { name: "Reason" }), "Wrong lot");
    await user.click(within(dialog).getByRole("button", { name: "Set Status" }));
    // Not sent from revision 2, nor from 3, which the reviewer hasn't been told of.
    expect(await within(dialog).findByText(/Someone changed it while this was open/)).toBeVisible();
    expect(calls("POST", `/api/v1/entities/${SAMPLE_ID}/states/status`)).toEqual([]);
    expect(within(dialog).getByRole("textbox", { name: "Reason" })).toHaveValue("Wrong lot");
    // Confirming again, they've seen the change: it's made from the revision now shown.
    answer(
      POST,
      respond(200, sample({ revision: 4, values: { ...changed.values, status: "received" } })),
    );
    await user.click(within(dialog).getByRole("button", { name: "Set Status" }));
    await waitFor(() => {
      expect(calls("POST", `/api/v1/entities/${SAMPLE_ID}/states/status`)).toHaveLength(1);
    });
    expect(
      calls("POST", `/api/v1/entities/${SAMPLE_ID}/states/status`).map((c) =>
        c.headers.get("If-Match"),
      ),
    ).toEqual(['"3"']);
  });
});

describe("a project's grid of a type with state fields", () => {
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

  it("shows each record's states, and filters by each field", async () => {
    withTypes();
    answer("GET /api/v1/entity-types", page([PROTEIN, SAMPLE]));
    answer("GET /api/v1/projects", page([KINASE]));
    answer(
      "POST /api/v1/entities/search",
      page([
        sample(),
        sample({
          hrid: "SMP-000002",
          values: { name: "Lot 8", status: "reviewed", custody: "in_lab" },
          locked: true,
          locked_by: ["status"],
        }),
      ]),
    );
    const { router, user } = await renderApp(
      "/projects/kinase-atlas/entities/?type=iki/lims.Sample",
    );
    const grid = await screen.findByRole("table", { name: "2 samples" });
    const rows = within(grid).getAllByRole("row");
    expect(
      within(nth(rows, 0))
        .getAllByRole("columnheader")
        .map((c) => c.textContent),
    ).toEqual(["", "ID", "Name", "Status", "Custody", "Made by", "Changed▼"]);
    expect(within(nth(rows, 1)).getAllByRole("cell")[3]).toHaveTextContent("In testing");
    expect(within(nth(rows, 2)).getAllByRole("cell")[3]).toHaveTextContent("ReviewedLocked");
    expect(within(nth(rows, 2)).getAllByRole("cell")[4]).toHaveTextContent("In the lab");

    const editor = await addFilter(user, "Custody");
    const value = within(editor).getByRole("combobox", { name: "Filter 1: value" });
    expect(within(value).getByRole("option", { name: "In the lab" })).toBeInTheDocument();
    await user.selectOptions(value, "Quarantined");
    const want = [{ field: "custody", op: "eq", value: "quarantined" }];
    await waitFor(() => {
      expect(router.state.location.search).toEqual({ type: "iki/lims.Sample", filters: want });
    });
    const sent = calls("POST", "/api/v1/entities/search");
    expect(nth(sent, sent.length - 1).body).toMatchObject({ where: { all: want } });
  });
});
