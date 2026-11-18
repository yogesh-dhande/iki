/**
 * A record's state (ADR 0118) on its page, in its history and in a project's grid,
 * against a fake API.
 */

import type { Schemas } from "@ikisci/sdk";
import { screen, waitFor, within } from "@testing-library/react";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { entity, KINASE, nth, PROTEIN, renderApp, revision, withTypes } from "./entity-fixtures";
import { answer, calls, page, problem, respond } from "./test-server";

const SAMPLE_ID = "00000000-0000-7000-8000-00000000e501";

const RECEIVED: Schemas["StateOut"] = {
  name: "received",
  label: "Received",
  description: null,
  locks: false,
};
const IN_TESTING: Schemas["StateOut"] = { ...RECEIVED, name: "in_testing", label: "In testing" };
const REVIEWED: Schemas["StateOut"] = {
  ...RECEIVED,
  name: "reviewed",
  label: "Reviewed",
  locks: true,
};
const STATES = [RECEIVED, IN_TESTING, REVIEWED];

const SAMPLE: Schemas["EntityTypeOut"] = {
  ...PROTEIN,
  name: "iki/lims.Sample",
  plugin: "iki/lims",
  label: "Sample",
  label_plural: "Samples",
  description: null,
  hrid_prefix: "SMP",
  identity: [],
  fields: PROTEIN.fields.slice(0, 1),
  states: STATES,
  transitions: [
    {
      name: "start",
      label: "Start testing",
      description: null,
      from: ["received"],
      to: "in_testing",
      roles: ["iki/lims.analyst"],
      reason: null,
      not_by: [],
    },
    {
      name: "approve",
      label: "Approve",
      description: null,
      from: ["in_testing"],
      to: "reviewed",
      roles: ["iki/lims.reviewer"],
      reason: null,
      not_by: ["start"],
    },
    {
      name: "send_back",
      label: "Send back",
      description: null,
      from: ["in_testing", "reviewed"],
      to: "received",
      roles: ["iki/lims.reviewer"],
      reason: "required",
      not_by: [],
    },
  ],
};

function sample(overrides: Partial<Schemas["EntityOut"]> = {}): Schemas["EntityOut"] {
  return entity({
    id: SAMPLE_ID,
    hrid: "SMP-000001",
    type: "iki/lims.Sample",
    title: "SMP-000001",
    revision: 2,
    values: { name: "Lot 7" },
    state: IN_TESTING,
    locked: false,
    can: {
      edit: true,
      transitions: [
        {
          name: "approve",
          label: "Approve",
          to: "reviewed",
          to_label: "Reviewed",
          reason: null,
          allowed: false,
          why_not:
            "You made “Start testing” on it, so someone else makes “Approve”: the plugin keeps them apart.",
        },
        {
          name: "send_back",
          label: "Send back",
          to: "received",
          to_label: "Received",
          reason: "required",
          allowed: true,
          why_not: null,
        },
      ],
    },
    ...overrides,
  });
}

function onSample(found: Schemas["EntityOut"] = sample()): void {
  withTypes();
  answer("GET /api/v1/entity-types", [PROTEIN, SAMPLE]);
  answer("GET /api/v1/entities/SMP-000001", found);
  answer("GET /api/v1/entities/SMP-000001/provenance", {});
  answer("GET /api/v1/entities/SMP-000001/projects", []);
}

describe("a record's state", () => {
  it("shows the state, and the transitions this person can make or why not", async () => {
    onSample();
    await renderApp("/entities/SMP-000001/");
    const state = await screen.findByRole("region", { name: "State: In testing" });
    const transitions = within(state).getByRole("list", { name: "Transitions" });
    const approve = within(transitions).getByRole("button", { name: "Approve" });
    // Disabled, but focusable, and saying why.
    expect(approve).toHaveAttribute("aria-disabled", "true");
    expect(approve).toHaveAccessibleDescription(
      "To Reviewed · You made “Start testing” on it, so someone else makes “Approve”: the plugin keeps them apart.",
    );
    const back = within(transitions).getByRole("button", { name: "Send back" });
    expect(back).not.toHaveAttribute("aria-disabled");
    expect(back).toHaveAccessibleDescription("To Received · asks why");
    // The header says it too.
    expect(screen.getByText("In testing", { selector: ".badge" })).toBeInTheDocument();
  });

  it("makes a transition with the reason it asks for", async () => {
    onSample();
    const { user } = await renderApp("/entities/SMP-000001/");
    const state = await screen.findByRole("region", { name: "State: In testing" });
    await user.click(within(state).getByRole("button", { name: "Send back" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Send back SMP-000001?" });
    // Without one, it says so before asking Iki.
    await user.click(within(dialog).getByRole("button", { name: "Send back" }));
    expect(
      await within(dialog).findByText("“Send back” needs a reason: say why."),
    ).toBeInTheDocument();
    expect(
      calls("POST", "/api/v1/entities/00000000-0000-7000-8000-00000000e501/transitions/send_back"),
    ).toHaveLength(0);
    // Iki's own word on the reason shows by it too.
    answer(
      "POST /api/v1/entities/00000000-0000-7000-8000-00000000e501/transitions/send_back",
      problem(422, "validation_failed", "The transition needs more.", [
        { pointer: "#/reason", detail: "Say it in 2000 characters or fewer." },
      ]),
    );
    await user.type(within(dialog).getByRole("textbox", { name: "Reason" }), "Long");
    await user.click(within(dialog).getByRole("button", { name: "Send back" }));
    expect(
      await within(dialog).findByText("Say it in 2000 characters or fewer."),
    ).toBeInTheDocument();
    await user.clear(within(dialog).getByRole("textbox", { name: "Reason" }));

    answer(
      "POST /api/v1/entities/00000000-0000-7000-8000-00000000e501/transitions/send_back",
      respond(200, sample({ revision: 3, state: RECEIVED })),
    );
    await user.type(within(dialog).getByRole("textbox", { name: "Reason" }), "Wrong lot");
    await user.click(within(dialog).getByRole("button", { name: "Send back" }));
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    });
    const sent = calls(
      "POST",
      "/api/v1/entities/00000000-0000-7000-8000-00000000e501/transitions/send_back",
    );
    expect(sent.at(-1)?.body).toEqual({ reason: "Wrong lot" });
    expect(sent.at(-1)?.headers.get("If-Match")).toBe('"2"');
    expect(await screen.findByText("SMP-000001 is Received now.")).toBeInTheDocument();
  });

  it("says a locked record can't change, and offers no edit", async () => {
    onSample(
      sample({
        state: REVIEWED,
        locked: true,
        can: {
          edit: false,
          transitions: [
            {
              name: "send_back",
              label: "Send back",
              to: "received",
              to_label: "Received",
              reason: "required",
              allowed: false,
              why_not: "It needs someone with the Reviewer role in Kinase Atlas.",
            },
          ],
        },
      }),
    );
    await renderApp("/entities/SMP-000001/");
    const state = await screen.findByRole("region", { name: "State: Reviewed" });
    expect(
      within(state).getByText(
        "Reviewed locks it: its values can’t change, nor can it be archived, until “Send back” takes it out.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("Locked", { selector: ".badge" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Edit" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Archive" })).not.toBeInTheDocument();
  });

  it("shows transitions in its history", async () => {
    onSample();
    answer(
      "GET /api/v1/entities/SMP-000001/revisions",
      page([
        revision({
          number: 2,
          kind: "transitioned",
          changed_fields: [],
          reason: "Bench 4",
          transition: {
            name: "start",
            label: "Start testing",
            from: RECEIVED,
            to: IN_TESTING,
          },
        }),
        revision({ number: 1, changed_fields: ["name"] }),
      ]),
    );
    await renderApp("/entities/SMP-000001/history/");
    expect(
      await screen.findByText(
        "Revision 2: Start testing: Received to In testing by Rosalind Franklin",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("“Bench 4”")).toBeInTheDocument();
  });
});

describe("a project's grid of a type with states", () => {
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

  it("shows each record's state, and filters by it", async () => {
    withTypes();
    answer("GET /api/v1/entity-types", [PROTEIN, SAMPLE]);
    answer("GET /api/v1/projects", page([KINASE]));
    answer(
      "POST /api/v1/entities/search",
      page([sample(), sample({ hrid: "SMP-000002", state: REVIEWED, locked: true })]),
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
    ).toEqual(["", "ID", "State", "Name", "Last changed"]);
    expect(within(nth(rows, 1)).getAllByRole("cell")[2]).toHaveTextContent("In testing");
    expect(within(nth(rows, 2)).getAllByRole("cell")[2]).toHaveTextContent("ReviewedLocked");

    await user.click(screen.getByRole("button", { name: "Filters" }));
    const panel = screen.getByRole("region", { name: "Filters" });
    await user.click(within(panel).getByRole("button", { name: "Add a filter" }));
    await user.selectOptions(
      within(panel).getByRole("combobox", { name: "Filter 1: field" }),
      "State",
    );
    const value = within(panel).getByRole("combobox", { name: "Filter 1: value" });
    expect(within(value).getByRole("option", { name: "In testing" })).toBeInTheDocument();
    await user.selectOptions(value, "In testing");
    const want = [{ field: "state", op: "eq", value: "in_testing" }];
    await waitFor(() => {
      expect(router.state.location.search).toEqual({ type: "iki/lims.Sample", filters: want });
    });
    const sent = calls("POST", "/api/v1/entities/search");
    expect(nth(sent, sent.length - 1).body).toMatchObject({ where: { all: want } });
  });
});
