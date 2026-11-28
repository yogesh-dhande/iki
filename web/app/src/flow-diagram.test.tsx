/**
 * A type's flow (ADR 0199) on its page and on its records' pages, drawn from the request and sample plugins'
 * analysis results and requests (flow-fixtures.json: the plugins' own flows, as
 * `GET /entity-types/{type}/flow` serves them).
 */

import type { Schemas } from "@ikisci/sdk";
import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { entity, field, PROTEIN, renderApp, withTypes } from "./entity-fixtures";
import fixtures from "./flow-fixtures.json";
import { answer, sent } from "./test-server";

const RESULT_ID = "00000000-0000-7000-8000-00000000a001";

/** One of the fixtures' types, as `GET /entity-types` serves it. */
function typeOf(name: string): Schemas["EntityTypeOut"] {
  const found = fixtures.types.find((t) => t.type === name);
  if (!found) throw new Error(name);
  return {
    ...PROTEIN,
    name,
    plugin: name.slice(0, name.indexOf(".")),
    label: found.label,
    label_plural: `${found.label}s`,
    description: null,
    hrid_prefix: "ANR",
    identity: [],
    fields: [
      field({ name: "notes", label: "Notes", type: "text" }),
      ...found.fields.map((f) =>
        field({
          ...(f as Partial<Schemas["FieldOut"]>),
          name: f.name,
          label: f.label,
          type: "state",
        }),
      ),
    ],
  };
}

const RESULT = typeOf("iki/requests.QCDeliverable");
const REQUEST = typeOf("iki/requests.Request");

/** The deployment's lab roles, and each type's flow: none for a protein. */
function labRoles(): void {
  for (const flow of fixtures.types) {
    answer(`GET /api/v1/entity-types/${flow.type}/flow`, flow);
  }
  answer(`GET /api/v1/entity-types/${PROTEIN.name}/flow`, {
    type: PROTEIN.name,
    label: PROTEIN.label,
    fields: [],
  });
  answer(
    "GET /api/v1/lab-roles",
    Object.entries(fixtures.roles).map(([name, label]) => ({
      name,
      label,
      description: null,
      plugin: name.slice(0, name.indexOf(".")),
      uses: [],
    })),
  );
}

describe("a type's page", () => {
  it("draws how its records move, who moves them and what they sign", async () => {
    withTypes();
    answer("GET /api/v1/entity-types", [PROTEIN, RESULT]);
    labRoles();
    await renderApp("/types/iki/requests.QCDeliverable/");
    const section = await screen.findByRole("region", { name: "How records move" });
    const states = within(section).getByRole("list", { name: "Review’s states" });
    const buttons = within(states).getAllByRole("button");
    expect(buttons.map((b) => b.textContent)).toEqual([
      "Recorded, where records start. Set by Analyst, Reviewer or Sample and result QA",
      "Submitted, locks it. Set by Analyst, signed: Authorship",
      "Reviewed, locks it. Set by Reviewer, signed: Review",
      "Released, locks it. Set by Sample and result QA, signed: QA release (Approval)",
    ]);
    // Each state's button is described by the same flow in words.
    const released = buttons[3];
    expect(released).toHaveAccessibleDescription(
      expect.stringContaining(
        "Sample and result QA sets it from Reviewed by signing it: QA release (Approval).",
      ),
    );
    // Those words are on the page, with what its guards say.
    const words = within(section).getByRole("list", { name: "Review’s states in words" });
    expect(
      within(words).getByText(
        "Have someone in QA who did not create, submit, edit or review the deliverable release it.",
      ),
    ).toBeInTheDocument();
    expect(
      within(words).getByText(
        "QA release (Approval): “I release the independently reviewed deliverable to the requesting project.”",
      ),
    ).toBeInTheDocument();
    // A key says what the marks mean: these, as the flow has them.
    const key = within(section).getByRole("list", { name: "Key" });
    expect(
      within(key)
        .getAllByRole("listitem")
        .map((i) => i.textContent.trim()),
    ).toEqual([
      "Where records start",
      "Locks the record",
      "Set by an electronic signature, with its meaning",
      "Someone moves it",
    ]);
  });

  it("moves between states by keyboard, and follows one's arrows when chosen", async () => {
    withTypes();
    answer("GET /api/v1/entity-types", [PROTEIN, RESULT]);
    labRoles();
    const { user } = await renderApp("/types/iki/requests.QCDeliverable/");
    const states = await screen.findByRole("list", { name: "Review’s states" });
    const [recorded, inReview, , released] = within(states).getAllByRole("button");
    recorded?.focus();
    await user.keyboard("{ArrowRight}");
    expect(inReview).toHaveFocus();
    await user.keyboard("{End}");
    expect(released).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(released).toHaveAttribute("aria-pressed", "true");
    // Its arrows are lit; the others aren't.
    const lines = document.querySelectorAll(".flow-edge.is-lit");
    expect([...lines].map((l) => l.getAttribute("data-edge"))).toEqual(["reviewed>released"]);
    await user.keyboard("{Escape}");
    expect(released).toHaveAttribute("aria-pressed", "false");
  });

  it("draws each of its state fields", async () => {
    withTypes();
    answer("GET /api/v1/entity-types", [PROTEIN, REQUEST]);
    labRoles();
    await renderApp("/types/iki/requests.Request/");
    const section = await screen.findByRole("region", { name: "How records move" });
    expect(
      within(section)
        .getAllByRole("heading", { level: 3 })
        .map((h) => h.textContent),
    ).toEqual(["Standing", "Status"]);
    // A state that follows any other says so once, not with an arrow from each.
    const status = within(section).getByRole("list", { name: "Status’s states" });
    expect(within(status).getByRole("button", { name: /^Accepted/ })).toHaveTextContent(
      "Coordinator · from any state",
    );
    expect(within(status).getByRole("button", { name: /^Accepted/ })).toHaveAccessibleName(
      "Accepted. Set by Coordinator · from any state",
    );
  });

  it("has no flow section for a type without state fields", async () => {
    withTypes();
    labRoles();
    await renderApp("/types/iki/protein.Protein/");
    await screen.findByRole("heading", { name: "Fields" });
    expect(screen.queryByRole("region", { name: "How records move" })).not.toBeInTheDocument();
  });
});

/** A reviewed type's flow as `GET …/flow` serves it (ADR 0306): no review field of its own,
 * and each flow its records can follow, compiled; here the QC deliverable's review drawn as
 * two flows of one procedure. */
function reviewed(marks: { pinned?: string; chosen?: string; default?: string } = {}) {
  const compiled = fixtures.types.find((t) => t.type === RESULT.name)?.fields[0];
  if (!compiled) throw new Error("no review field");
  const one = (flow: string, label: string, summary: string) => ({
    procedure: "iki/core.result_review@1",
    procedure_label: "Result review",
    flow,
    label,
    summary,
    starter: "iki/requests.deliverable_review",
    suggested: flow === "research",
    chosen: marks.chosen === flow,
    default: marks.default === flow,
    pinned: marks.pinned === flow,
    field: compiled,
  });
  return {
    type: RESULT.name,
    label: RESULT.label,
    fields: [],
    review: {
      field: "review",
      follows: null,
      flows: [
        one("research", "Research", "One person signs it off."),
        one("regulated_qc", "Regulated QC", "Submitted, reviewed and released by three people."),
      ],
    },
  };
}

describe("a reviewed type's page", () => {
  it("says which procedure it follows, and draws each flow that could be chosen", async () => {
    withTypes();
    answer("GET /api/v1/entity-types", [PROTEIN, RESULT]);
    labRoles();
    answer(`GET /api/v1/entity-types/${RESULT.name}/flow`, reviewed({ default: "regulated_qc" }));
    await renderApp("/types/iki/requests.QCDeliverable/");
    const section = await screen.findByRole("region", { name: "How records move" });
    expect(
      within(section).getByText(
        "Reviewed where a project hasn’t chosen its own under Result review, Regulated QC, the deployment’s default. Each record keeps the procedure it was made under.",
      ),
    ).toBeInTheDocument();
    const titles = within(section).getAllByRole("heading", { level: 3 });
    // The one that applies first, then the others.
    expect(titles.map((t) => t.textContent)).toEqual([
      "Result review, Regulated QC Deployment default",
      "Result review, Research Suggested",
    ]);
    expect(within(section).getAllByRole("list", { name: "Review’s states" })).toHaveLength(2);
  });

  it("says so when no procedure is chosen", async () => {
    withTypes();
    answer("GET /api/v1/entity-types", [PROTEIN, RESULT]);
    labRoles();
    answer(`GET /api/v1/entity-types/${RESULT.name}/flow`, reviewed());
    await renderApp("/types/iki/requests.QCDeliverable/");
    const section = await screen.findByRole("region", { name: "How records move" });
    expect(
      within(section).getByText(/^No review procedure is chosen where a project hasn’t/),
    ).toBeInTheDocument();
  });
});

describe("a record's page", () => {
  function onResult(): void {
    withTypes();
    answer("GET /api/v1/entity-types", [PROTEIN, RESULT]);
    labRoles();
    const result = entity({
      id: RESULT_ID,
      hrid: "QCD-000001",
      type: RESULT.name,
      title: "QCD-000001",
      values: { notes: "SEC run", review: "submitted" },
      locked: true,
      locked_by: ["review"],
      can: {
        edit: false,
        correct: false,
        edit_amount: false,
        draw: false,
        sign: [],
        states: {
          review: [
            {
              name: "recorded",
              label: "Recorded",
              locks: false,
              reason: "required",
              allowed: true,
              why_not: null,
              signed: false,
            },
            {
              name: "reviewed",
              label: "Reviewed",
              locks: true,
              reason: null,
              allowed: false,
              why_not:
                "Someone other than the result's creator and submitting analyst must review it.",
              signed: true,
            },
          ],
        },
      },
    });
    answer("GET /api/v1/entities/QCD-000001", result);
    answer("GET /api/v1/entities/QCD-000001/provenance", {});
    answer("GET /api/v1/entities/QCD-000001/projects", []);
  }

  it("shows where it is in its flow, and what's next for the reader, when asked", async () => {
    onResult();
    const { user } = await renderApp("/entities/QCD-000001/");
    const states = await screen.findByRole("region", { name: "States" });
    const toggle = within(states).getByRole("button", { name: "How it moves" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(within(states).queryByRole("list", { name: "Review’s states" })).not.toBeInTheDocument();
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(within(states).getByText("Next for you: Recorded.")).toBeInTheDocument();
    const flow = within(states).getByRole("list", { name: "Review’s states" });
    const now = within(flow).getByRole("button", { current: "step" });
    expect(now).toHaveTextContent(/^Submitted/);
    expect(now).toHaveTextContent("Now");
    expect(within(flow).getByRole("button", { name: /^Recorded/ })).toHaveTextContent("You can");
    // Why not, in the words the diagram's described by.
    expect(within(flow).getByRole("button", { name: /^Reviewed/ })).toHaveAccessibleDescription(
      expect.stringContaining(
        "You can’t set it to this now: Someone other than the result's creator and submitting analyst must review it.",
      ),
    );
    // Its arrow to the state the reader may set is marked as theirs.
    const next = [...document.querySelectorAll(".flow-edge.is-next")];
    expect(next.map((e) => e.getAttribute("data-edge"))).toEqual(["submitted>recorded"]);
    await user.click(toggle);
    expect(within(states).queryByRole("list", { name: "Review’s states" })).not.toBeInTheDocument();
  });
});

describe("a reviewed record's page", () => {
  it("draws the flow it's pinned to, not the project's choice now", async () => {
    withTypes();
    answer("GET /api/v1/entity-types", [PROTEIN, RESULT]);
    labRoles();
    answer(
      `GET /api/v1/entity-types/${RESULT.name}/flow`,
      reviewed({ pinned: "regulated_qc", chosen: "research" }),
    );
    answer(
      "GET /api/v1/entities/QCD-000002",
      entity({
        id: "00000000-0000-7000-8000-00000000a002",
        hrid: "QCD-000002",
        type: RESULT.name,
        title: "QCD-000002",
        values: {
          review: "submitted",
          review_procedure: "iki/core.result_review@1",
          review_flow: "regulated_qc",
        },
      }),
    );
    answer("GET /api/v1/entities/QCD-000002/provenance", {});
    answer("GET /api/v1/entities/QCD-000002/projects", []);
    const { user } = await renderApp("/entities/QCD-000002/");
    const states = await screen.findByRole("region", { name: "States" });
    expect(
      await within(states).findByText("Reviewed under Result review, Regulated QC."),
    ).toBeInTheDocument();
    // The state's own label, from the flow it's pinned to.
    expect(within(states).getByText("Review: Submitted")).toBeInTheDocument();
    const asked = sent.find((c) => c.path === `/api/v1/entity-types/${RESULT.name}/flow`);
    expect(Object.fromEntries(asked?.query ?? [])).toMatchObject({
      procedure: "iki/core.result_review@1",
      flow: "regulated_qc",
    });
    await user.click(within(states).getByRole("button", { name: "How it moves" }));
    const flow = within(states).getByRole("list", { name: "Review’s states" });
    expect(within(flow).getByRole("button", { current: "step" })).toHaveTextContent(/^Submitted/);
  });
});
