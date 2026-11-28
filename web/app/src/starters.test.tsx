/**
 * A project's starters (ADR 0198): choosing a review flow, drawn from plate assays' real
 * result types (flow-fixtures.json), and who holds each lab role;
 * previewing what applying changes; applying it. Against a fake API.
 */

import type { Schemas } from "@ikisci/sdk";
import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { queryClient } from "./api";
import fixtures from "./flow-fixtures.json";
import { createAppRouter } from "./router";
import {
  canStaff,
  eligible,
  independentEnough,
  routeOf,
  type Member,
  type StarterFlow,
} from "./starters";
import { answer, calls, ME, page, problem, project, signedIn, TEAM } from "./test-server";

const SERVICE = project({
  id: "00000000-0000-7000-8000-0000000000s1",
  name: "Assay Core",
  slug: "assay-core",
});
const PLATE = "iki/plate_assays.";
const [ANALYST, REVIEWER, QA] = [`${PLATE}analyst`, `${PLATE}reviewer`, `${PLATE}qa`];

function starter(more: Partial<Schemas["StarterOut"]> = {}): Schemas["StarterOut"] {
  return {
    id: "dose-response",
    version: "2.0.0",
    label: "Dose-response plate assay",
    description: "Choose the review flow for new dose-response fits and assign the assay roles.",
    governs: "dose-response fits",
    flows: [
      {
        id: "research",
        label: "Research",
        result_type: `${PLATE}ResearchAssayResult`,
        steps: [],
        apart: [],
        required_roles: [ANALYST, REVIEWER],
        independent_roles: [],
        summary:
          "A result is recorded, then one reviewer signs it off. For discovery work, where one check is enough.",
      },
      {
        id: "regulated_qc",
        label: "Regulated QC",
        result_type: `${PLATE}QCAssayResult`,
        steps: [],
        apart: [],
        required_roles: [ANALYST, REVIEWER, QA],
        independent_roles: [ANALYST, REVIEWER, QA],
        summary:
          "The analyst signs the submission, an independent reviewer signs the review, and QA signs the release: three different people, each signing in again. For GxP and QC work.",
      },
    ],
    roles: [
      { id: ANALYST, label: "Assay analyst", minimum_project_role: "editor" },
      { id: REVIEWER, label: "Assay reviewer", minimum_project_role: "viewer" },
      { id: QA, label: "Assay QA", minimum_project_role: "viewer" },
    ],
    types: [],
    applied: null,
    procedure: null,
    suggested: null,
    slots: [],
    chosen: null,
    default: null,
    ...more,
  };
}

function person(
  id: string,
  name: string,
  role: Member["role"],
  lab: string[] = [],
  more: Partial<Schemas["UserOut"]> = {},
): Member {
  return {
    id,
    added_at: "2026-03-14T09:00:00Z",
    role,
    team: null,
    team_size: null,
    lab_roles: lab.map((r) => ({
      role: r,
      label: r,
      declared: true,
      granted_at: "2026-03-14T09:00:00Z",
      granted_by: null,
    })),
    user: {
      id: `${id}-user`,
      name,
      email: `${name.split(" ")[0]?.toLowerCase() ?? "x"}@example.com`,
      kind: "person",
      active: true,
      imported_from: "",
      ...more,
    },
  };
}

const ADA = person("m1", "Ada Lovelace", "editor", [ANALYST]);
const BARBARA = person("m2", "Barbara McClintock", "editor");
const CARL = person("m3", "Carl Woese", "viewer");
const DOROTHY = person("m4", "Dorothy Hodgkin", "admin");
const AGENT = person("m5", "Plate robot", "editor", [], { kind: "service" });
const LEFT = person("m6", "Lise Meitner", "editor", [], { active: false });
const TEAM_GRANT: Member = { ...person("m7", "x", "admin"), user: null, team: TEAM, team_size: 4 };
const MEMBERS = [ADA, BARBARA, CARL, DOROTHY, AGENT, LEFT, TEAM_GRANT];

function flowOf(name: string): Schemas["TypeFlowOut"] {
  const found = fixtures.types.find((t) => t.type === name);
  if (!found) throw new Error(name);
  return found as unknown as Schemas["TypeFlowOut"];
}

function onStarters(
  found: Schemas["StarterOut"] = starter(),
  service: Schemas["ProjectOut"] = SERVICE,
  members: Member[] = MEMBERS,
): void {
  signedIn();
  answer("GET /api/v1/projects", ({ query }: { query: URLSearchParams }) =>
    page(!query.has("slug") || query.get("slug") === service.slug ? [service] : []),
  );
  answer("GET /api/v1/starters", [found]);
  answer(`GET /api/v1/projects/${service.id}/members`, page(members));
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
  for (const type of [
    `${PLATE}ResearchAssayResult`,
    `${PLATE}QCAssayResult`,
    "iki/experiments.Experiment",
  ]) {
    answer(`GET /api/v1/entity-types/${type}/flow`, flowOf(type));
  }
}

async function renderApp(path: string) {
  const router = createAppRouter(createMemoryHistory({ initialEntries: [path] }));
  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  await screen.findByRole("heading", { level: 1 });
  return { router, user: userEvent.setup() };
}

async function setupCard(): Promise<HTMLElement> {
  return screen.findByRole("region", { name: "Dose-response plate assay" });
}

describe("who can do what", () => {
  it("offers a lab role to people with a membership of their own and a high enough role", () => {
    expect(eligible(MEMBERS, "editor").map((m) => m.id)).toEqual(["m1", "m2", "m4"]);
    expect(eligible(MEMBERS, "viewer").map((m) => m.id)).toEqual(["m1", "m2", "m3", "m4"]);
  });

  it("needs a different signer for each step, though one person may hold several roles", () => {
    const roles = [ANALYST, REVIEWER, QA];
    expect(independentEnough(roles, { [ANALYST]: ["a"], [REVIEWER]: ["b"], [QA]: ["c"] })).toBe(
      true,
    );
    expect(independentEnough(roles, { [ANALYST]: ["a"], [REVIEWER]: ["a"], [QA]: ["c"] })).toBe(
      false,
    );
    // Ada may review or release, so long as someone else can do the one she doesn't.
    expect(
      independentEnough(roles, { [ANALYST]: ["b"], [REVIEWER]: ["a", "c"], [QA]: ["a"] }),
    ).toBe(true);
    expect(
      independentEnough(roles, { [ANALYST]: ["a", "b"], [REVIEWER]: ["a", "b"], [QA]: ["b"] }),
    ).toBe(false);
  });

  it("draws only a flow's own route through a type that has one per flow", () => {
    const field = flowOf("iki/stability.Measurement").fields[0];
    const research = routeOf(field, ["recorded", "signed"]);
    expect(research?.states.map((s) => s.name)).toEqual(["recorded", "signed"]);
    expect(research?.transitions.map((t) => `${t.from}>${t.to}`)).toEqual(["recorded>signed"]);
    const qc = routeOf(field, ["recorded", "submitted", "reviewed", "released"]);
    expect(qc?.states.map((s) => s.name)).not.toContain("signed");
    expect(routeOf(field, null)).toBe(field);

    // An experiment has a route for each of three flows (ADR 0247).
    const experiment = flowOf("iki/experiments.Experiment").fields[0];
    const signed = routeOf(experiment, ["recorded", "signed"]);
    expect(signed?.states.map((s) => s.name)).toEqual(["recorded", "signed"]);
    expect(signed?.transitions.map((t) => `${t.from}>${t.to}`)).toEqual(["recorded>signed"]);
    const regulated = routeOf(experiment, ["recorded", "submitted", "reviewed", "released"]);
    expect(regulated?.states.map((s) => s.name)).not.toContain("witnessed");
    // No state's correction leads off the route.
    const kept = new Set(regulated?.states.map((s) => s.name));
    expect(regulated?.states.every((s) => !s.on_edit || kept.has(s.on_edit))).toBe(true);
  });

  it("draws Record only as a result that stays recorded, with no one to sign", () => {
    const field = flowOf("iki/proteomics.ProteomicsResult").fields[0];
    const recorded = routeOf(field, ["recorded"]);
    expect(recorded?.states.map((s) => s.name)).toEqual(["recorded"]);
    expect(recorded?.transitions).toEqual([]);
    expect(recorded?.states.flatMap((s) => s.signatures)).toEqual([]);
  });
});

const EXPERIMENT = "iki/experiments.";
const [AUTHOR, WITNESS, EXPERIMENT_QA] = [
  `${EXPERIMENT}author`,
  `${EXPERIMENT}reviewer`,
  `${EXPERIMENT}qa`,
];

/** The experiment-review starter as the server lists it: every flow's records are experiments. */
function experimentReview(): Schemas["StarterOut"] {
  const flow = (
    id: Schemas["StarterFlowOut"]["id"],
    label: string,
    required: string[],
    independent: string[],
    states: string[],
  ): Schemas["StarterFlowOut"] => ({
    id,
    label,
    result_type: `${EXPERIMENT}Experiment`,
    steps: [],
    apart: [],
    required_roles: required,
    independent_roles: independent,
    summary: `${label}, in a sentence.`,
    states,
  });
  return {
    procedure: null,
    suggested: null,
    slots: [],
    chosen: null,
    default: null,
    id: "experiment-review",
    version: "1.0.0",
    label: "Experiment review",
    description: "Choose how experiment records are signed in this project and assign the roles.",
    governs: "experiment captures",
    flows: [
      flow("author_signed", "Author sign-off", [AUTHOR], [], ["recorded", "signed"]),
      flow("research", "Research", [AUTHOR, WITNESS], [AUTHOR, WITNESS], ["recorded", "witnessed"]),
      flow(
        "regulated_qc",
        "Regulated QC",
        [AUTHOR, WITNESS, EXPERIMENT_QA],
        [AUTHOR, WITNESS, EXPERIMENT_QA],
        ["recorded", "submitted", "reviewed", "released"],
      ),
    ],
    roles: [
      { id: AUTHOR, label: "Experiment author", minimum_project_role: "editor" },
      { id: WITNESS, label: "Experiment reviewer", minimum_project_role: "viewer" },
      { id: EXPERIMENT_QA, label: "Experiment QA", minimum_project_role: "viewer" },
    ],
    types: [`${EXPERIMENT}Experiment`],
    applied: null,
  };
}

describe("a project's starters", () => {
  it("draws only the states of the experiment flow chosen, and checks its signers", async () => {
    onStarters(experimentReview());
    const { user } = await renderApp("/projects/assay-core/settings/starters/");
    const card = await screen.findByRole("region", { name: "Experiment review" });
    const flows = within(card).getByRole("group", { name: "Review flow" });
    expect(
      within(flows).getByRole("radio", { name: /^Author sign-off: Author sign-off, in a/ }),
    ).toBeChecked();
    const states = await within(card).findByRole("list", { name: "Review’s states" });
    await waitFor(() => {
      expect(
        within(states)
          .getAllByRole("button")
          .map((b) => b.textContent),
      ).toEqual([
        "Recorded, where records start. Set by Experiment author, Experiment reviewer or Experiment QA",
        "Signed by author, locks it. Set by Experiment author, signed: Authorship",
      ]);
    });
    // Only an author is asked for, and one person is enough.
    expect(within(card).queryByRole("group", { name: "Experiment reviewer" })).toBeNull();

    await user.click(within(flows).getByRole("radio", { name: /^Research/ }));
    await waitFor(() => {
      expect(
        within(within(card).getByRole("list", { name: "Review’s states" }))
          .getAllByRole("button")
          .map((b) => b.textContent.split(",")[0]),
      ).toEqual(["Recorded", "Witnessed"]);
    });
    // The witness signs independently of the author: one person can't be both.
    const authors = within(card).getByRole("group", { name: "Experiment author" });
    const witnesses = within(card).getByRole("group", { name: "Experiment reviewer" });
    await user.click(within(authors).getByRole("checkbox", { name: "Barbara McClintock" }));
    await user.click(within(witnesses).getByRole("checkbox", { name: "Barbara McClintock" }));
    await user.click(within(card).getByRole("button", { name: "Preview" }));
    expect(within(card).getByRole("alert")).toHaveTextContent(
      "Choose 2 different people for Experiment author and Experiment reviewer",
    );
    expect(calls("POST")).toHaveLength(0);
  });

  it("draws each flow, checks the choice, previews what changes, then applies it", async () => {
    onStarters();
    const { user } = await renderApp("/projects/assay-core/settings/starters/");
    const card = await setupCard();
    // Not applied here yet: Research first, drawn.
    expect(within(card).getByText("Not applied here")).toBeInTheDocument();
    const flows = within(card).getByRole("group", { name: "Review flow" });
    expect(
      within(flows).getByRole("radio", { name: /^Research: A result is recorded/ }),
    ).toBeChecked();
    expect(await within(card).findByRole("list", { name: "Review’s states" })).toHaveTextContent(
      "Signed off",
    );
    await user.click(within(flows).getByRole("radio", { name: /^Regulated QC/ }));
    const states = await within(card).findByRole("list", { name: "Review’s states" });
    await waitFor(() => {
      expect(
        within(states)
          .getAllByRole("button")
          .map((b) => b.textContent),
      ).toEqual([
        "Recorded, where records start. Set by Assay analyst, Assay reviewer or Assay QA",
        "Submitted, locks it. Set by Assay analyst, signed: Authorship",
        "Independently reviewed, locks it. Set by Assay reviewer, signed: Review",
        "QA released, locks it. Set by Assay QA, signed: QA release (Approval)",
      ]);
    });

    // An analyst is an Editor or more, with their own membership; whoever holds a role
    // already is chosen for it.
    const analysts = within(card).getByRole("group", { name: "Assay analyst" });
    expect(
      within(analysts)
        .getAllByRole("checkbox")
        .map((c) => c.closest(".choice")?.querySelector("label")?.textContent),
    ).toEqual(["Ada Lovelace", "Barbara McClintock", "Dorothy Hodgkin"]);
    expect(
      within(card).getByRole("checkbox", {
        name: "Ada Lovelace",
        description: /holds Assay analyst already/,
      }),
    ).toBeChecked();
    const reviewers = within(card).getByRole("group", { name: "Assay reviewer" });
    expect(within(reviewers).getByRole("checkbox", { name: "Carl Woese" })).toBeInTheDocument();
    const qa = within(card).getByRole("group", { name: "Assay QA" });

    // One person for all three signatures: refused before asking Iki.
    await user.click(within(analysts).getByRole("checkbox", { name: "Ada Lovelace" }));
    await user.click(within(analysts).getByRole("checkbox", { name: "Barbara McClintock" }));
    await user.click(within(reviewers).getByRole("checkbox", { name: "Barbara McClintock" }));
    await user.click(within(qa).getByRole("checkbox", { name: "Barbara McClintock" }));
    await user.click(within(card).getByRole("button", { name: "Preview" }));
    expect(within(card).getByRole("alert")).toHaveTextContent(
      "Choose 3 different people for Assay analyst, Assay reviewer and Assay QA",
    );
    expect(calls("POST")).toHaveLength(0);

    await user.click(within(reviewers).getByRole("checkbox", { name: "Barbara McClintock" }));
    await user.click(within(reviewers).getByRole("checkbox", { name: "Carl Woese" }));
    await user.click(within(qa).getByRole("checkbox", { name: "Barbara McClintock" }));
    await user.click(within(qa).getByRole("checkbox", { name: "Dorothy Hodgkin" }));
    const plan = {
      starter: "dose-response",
      version: "2.0.0",
      project: SERVICE.id,
      flow: "regulated_qc",
      result_type: `${PLATE}QCAssayResult`,
      changed: true,
      role_assignments: {},
      grants_to_add: { [ANALYST]: ["m2"], [REVIEWER]: ["m3"], [QA]: ["m4"] },
      plan: "a1".repeat(32),
    };
    answer("POST /api/v1/starters/dose-response/preview", plan);
    await user.click(within(card).getByRole("button", { name: "Preview" }));
    const preview = await within(card).findByRole("status", { name: "What applying will do" });
    expect(calls("POST", "/api/v1/starters/dose-response/preview")[0]?.body).toEqual({
      project: SERVICE.id,
      version: "2.0.0",
      flow: "regulated_qc",
      role_assignments: {
        [ANALYST]: ["m2"],
        [REVIEWER]: ["m3"],
        [QA]: ["m4"],
      },
    });
    expect(
      within(preview)
        .getAllByRole("listitem")
        .map((i) => i.textContent),
    ).toEqual([
      "New results follow the Regulated QC flow.",
      "Barbara McClintock becomes Assay analyst.",
      "Carl Woese becomes Assay reviewer.",
      "Dorothy Hodgkin becomes Assay QA.",
    ]);

    answer("POST /api/v1/starters/dose-response/apply", plan);
    await user.click(within(card).getByRole("button", { name: "Apply" }));
    expect(
      await screen.findByText("Applied Dose-response plate assay with the Regulated QC flow."),
    ).toBeInTheDocument();
    // It applies the plan previewed, as Iki identified it.
    expect(calls("POST", "/api/v1/starters/dose-response/apply")[0]?.body).toEqual({
      ...(calls("POST", "/api/v1/starters/dose-response/preview")[0]?.body as object),
      plan: plan.plan,
    });
  });

  it("lets a project record results without sign-off, granting no lab roles", async () => {
    const PROTEOMICS = "iki/proteomics.";
    const result = `${PROTEOMICS}ProteomicsResult`;
    const flow = (
      id: StarterFlow["id"],
      label: string,
      states: string[],
      required_roles: string[],
      summary: string,
    ): StarterFlow => ({
      id,
      label,
      result_type: result,
      steps: [],
      apart: [],
      states,
      required_roles,
      independent_roles: id === "regulated_qc" ? required_roles : [],
      summary,
    });
    onStarters(
      starter({
        id: "proteomics-search-review",
        version: "1.0.0",
        label: "Proteomics search results",
        description: "Choose how newly imported search results are reviewed.",
        governs: "proteomics search results",
        flows: [
          flow(
            "research",
            "Research",
            ["recorded", "signed_off"],
            [`${PROTEOMICS}reviewer`],
            "A result is recorded, then one reviewer signs it off.",
          ),
          flow(
            "regulated_qc",
            "Regulated QC",
            ["recorded", "submitted", "reviewed", "released"],
            [`${PROTEOMICS}analyst`, `${PROTEOMICS}reviewer`, `${PROTEOMICS}qa`],
            "The analyst signs the submission, an independent reviewer the review, and QA the release.",
          ),
          flow(
            "record_only",
            "Record only",
            ["recorded"],
            [],
            "A result is kept as evidence, as it was recorded, and no one signs it.",
          ),
        ],
        roles: [
          {
            id: `${PROTEOMICS}analyst`,
            label: "Proteomics analyst",
            minimum_project_role: "editor",
          },
          {
            id: `${PROTEOMICS}reviewer`,
            label: "Proteomics reviewer",
            minimum_project_role: "viewer",
          },
          { id: `${PROTEOMICS}qa`, label: "Proteomics QA", minimum_project_role: "viewer" },
        ],
      }),
    );
    answer(`GET /api/v1/entity-types/${result}/flow`, flowOf(result));
    const { user } = await renderApp("/projects/assay-core/settings/starters/");
    const card = await screen.findByRole("region", { name: "Proteomics search results" });
    const flows = within(card).getByRole("group", { name: "Review flow" });
    // Research, the deployment's default, is chosen until the Admin picks another.
    expect(within(flows).getByRole("radio", { name: /^Research/ })).toBeChecked();
    expect(within(card).getByRole("group", { name: "Proteomics reviewer" })).toBeInTheDocument();

    await user.click(within(flows).getByRole("radio", { name: /^Record only: .*no one signs it/ }));
    const states = await within(card).findByRole("list", { name: "Review’s states" });
    await waitFor(() => {
      expect(
        within(states)
          .getAllByRole("button")
          .map((b) => b.textContent),
      ).toEqual([
        "Recorded, where records start. Set by Proteomics analyst, Proteomics reviewer or Proteomics QA",
      ]);
    });
    expect(within(card).getByText(/No one signs in Record only/)).toBeInTheDocument();
    expect(within(card).queryByRole("group", { name: "Proteomics reviewer" })).toBeNull();

    answer("POST /api/v1/starters/proteomics-search-review/preview", {
      starter: "proteomics-search-review",
      version: "1.0.0",
      project: SERVICE.id,
      flow: "record_only",
      result_type: result,
      changed: true,
      role_assignments: {},
      grants_to_add: {},
      plan: "b2".repeat(32),
    });
    await user.click(within(card).getByRole("button", { name: "Preview" }));
    const preview = await within(card).findByRole("status", { name: "What applying will do" });
    expect(calls("POST", "/api/v1/starters/proteomics-search-review/preview")[0]?.body).toEqual({
      project: SERVICE.id,
      version: "1.0.0",
      flow: "record_only",
      role_assignments: {},
    });
    expect(
      within(preview)
        .getAllByRole("listitem")
        .map((i) => i.textContent),
    ).toEqual(["New results follow the Record only flow."]);
  });

  it("applies nothing that changed since its preview, and shows what applying does now", async () => {
    onStarters(starter({ applied: { version: "2.0.0", flow: "regulated_qc" } }));
    const { user } = await renderApp("/projects/assay-core/settings/starters/");
    const card = await setupCard();
    await user.click(within(card).getByRole("radio", { name: /^Research/ }));
    const analysts = within(card).getByRole("group", { name: "Assay analyst" });
    await user.click(within(analysts).getByRole("checkbox", { name: "Dorothy Hodgkin" }));
    const reviewers = within(card).getByRole("group", { name: "Assay reviewer" });
    await user.click(within(reviewers).getByRole("checkbox", { name: "Carl Woese" }));
    const reviewed = {
      starter: "dose-response",
      version: "2.0.0",
      project: SERVICE.id,
      flow: "research" as const,
      result_type: `${PLATE}ResearchAssayResult`,
      changed: true,
      role_assignments: {},
      grants_to_add: {},
      plan: "b2".repeat(32),
    };
    answer("POST /api/v1/starters/dose-response/preview", reviewed);
    await user.click(within(card).getByRole("button", { name: "Preview" }));
    const shown = await within(card).findByRole("status", { name: "What applying will do" });
    expect(
      within(shown)
        .getAllByRole("listitem")
        .map((i) => i.textContent),
    ).toEqual(["New results follow the Research flow."]);
    // Meanwhile another Admin revoked Carl Woese's reviewer grant: the switch now adds it back.
    const now = { ...reviewed, grants_to_add: { [REVIEWER]: ["m3"] }, plan: "c3".repeat(32) };
    answer(
      "POST /api/v1/starters/dose-response/apply",
      problem(
        409,
        "starter_plan_changed",
        "What applying does changed since it was previewed.",
        undefined,
        { plan: now },
      ),
    );
    await user.click(within(card).getByRole("button", { name: "Apply" }));
    const note = await within(card).findByRole("alert");
    expect(note).toHaveTextContent("What applying does changed while you were looking at it.");
    expect(note).toHaveTextContent("Applying now also: Carl Woese becomes Assay reviewer.");
    expect(note).toHaveTextContent("If it’s still what you want, apply it.");
    const again = within(card).getByRole("status", { name: "What applying will do" });
    expect(
      within(again)
        .getAllByRole("listitem")
        .map((i) => i.textContent),
    ).toEqual(["New results follow the Research flow.", "Carl Woese becomes Assay reviewer."]);
    expect(screen.queryByText(/^Applied Dose-response/)).not.toBeInTheDocument();
    // Applying again applies what's shown now.
    answer("POST /api/v1/starters/dose-response/apply", now);
    await user.click(within(card).getByRole("button", { name: "Apply" }));
    expect(
      await screen.findByText("Applied Dose-response plate assay with the Research flow."),
    ).toBeInTheDocument();
    const sent = calls("POST", "/api/v1/starters/dose-response/apply").map(
      (c) => (c.body as { plan: string }).plan,
    );
    expect(sent).toEqual([reviewed.plan, now.plan]);
    expect(within(card).queryByRole("alert")).not.toBeInTheDocument();
  });

  it("asks again after any change to what was previewed, and shows what Iki refuses", async () => {
    onStarters(starter({ applied: { version: "2.0.0", flow: "research" } }));
    const { user } = await renderApp("/projects/assay-core/settings/starters/");
    const card = await setupCard();
    // Switching flow says what happens to records mid-flow.
    await user.click(within(card).getByRole("radio", { name: /^Regulated QC/ }));
    expect(within(card).getByRole("note")).toHaveTextContent(
      "New dose-response fits will use the Regulated QC flow. Existing ones keep their current review and signature requirements",
    );
    const analysts = within(card).getByRole("group", { name: "Assay analyst" });
    await user.click(within(analysts).getByRole("checkbox", { name: "Dorothy Hodgkin" }));
    const reviewers = within(card).getByRole("group", { name: "Assay reviewer" });
    await user.click(within(reviewers).getByRole("checkbox", { name: "Carl Woese" }));
    const qa = within(card).getByRole("group", { name: "Assay QA" });
    await user.click(within(qa).getByRole("checkbox", { name: "Barbara McClintock" }));
    answer("POST /api/v1/starters/dose-response/preview", {
      starter: "dose-response",
      version: "2.0.0",
      project: SERVICE.id,
      flow: "regulated_qc",
      result_type: `${PLATE}QCAssayResult`,
      changed: true,
      role_assignments: {},
      grants_to_add: { [QA]: ["m2"] },
      plan: "d4".repeat(32),
    });
    await user.click(within(card).getByRole("button", { name: "Preview" }));
    const preview = await within(card).findByRole("status", { name: "What applying will do" });
    expect(preview).toHaveTextContent("Barbara McClintock becomes Assay QA.");
    expect(within(card).getByRole("button", { name: "Apply" })).toBeInTheDocument();

    // A change to what was previewed needs another look before it's applied.
    await user.click(within(qa).getByRole("checkbox", { name: "Barbara McClintock" }));
    await user.click(within(qa).getByRole("checkbox", { name: "Ada Lovelace" }));
    expect(
      within(card).queryByRole("status", { name: "What applying will do" }),
    ).not.toBeInTheDocument();
    answer(
      "POST /api/v1/starters/dose-response/preview",
      problem(
        409,
        "refused",
        "Choose active people with a direct membership in the assay project.",
      ),
    );
    await user.click(within(card).getByRole("button", { name: "Preview" }));
    expect(
      await within(card).findByText(
        "Choose active people with a direct membership in the assay project.",
      ),
    ).toBeInTheDocument();
    expect(calls("POST", "/api/v1/starters/dose-response/apply")).toHaveLength(0);
  });

  it("shows an already applied configuration and prevents a redundant apply", async () => {
    onStarters(starter({ applied: { version: "2.0.0", flow: "research" } }), SERVICE, [
      ADA,
      person(CARL.id, "Carl Woese", "viewer", [REVIEWER]),
    ]);
    const { user } = await renderApp("/projects/assay-core/settings/starters/");
    const card = await setupCard();
    const reviewers = within(card).getByRole("group", { name: "Assay reviewer" });
    expect(within(reviewers).getByRole("checkbox", { name: "Carl Woese" })).toBeChecked();
    answer("POST /api/v1/starters/dose-response/preview", {
      starter: "dose-response",
      version: "2.0.0",
      project: SERVICE.id,
      flow: "research",
      result_type: `${PLATE}ResearchAssayResult`,
      changed: false,
      role_assignments: { [ANALYST]: [ADA.id], [REVIEWER]: [CARL.id] },
      grants_to_add: {},
      plan: "e5".repeat(32),
    });
    await user.click(within(card).getByRole("button", { name: "Preview" }));
    const plan = await within(card).findByRole("status", { name: "What applying will do" });
    expect(plan).toHaveTextContent("This starter is already configured with these choices.");
    const apply = within(card).getByRole("button", { name: "Apply" });
    expect(apply).toHaveAttribute("aria-disabled", "true");
    await user.click(apply);
    expect(calls("POST", "/api/v1/starters/dose-response/apply")).toHaveLength(0);
  });

  it("shows its members what's installed and applied, without the controls", async () => {
    const member = {
      ...SERVICE,
      my_role: "viewer" as const,
      can: { ...SERVICE.can, manage: false, edit: false },
    };
    onStarters(starter({ applied: { version: "2.0.0", flow: "research" } }), member);
    await renderApp("/projects/assay-core/settings/starters/");
    const card = await setupCard();
    expect(
      within(card).getByText("Only the project’s Admins set it up. These are its review flows."),
    ).toBeInTheDocument();
    expect(within(card).queryByRole("button", { name: "Preview" })).not.toBeInTheDocument();
    await waitFor(() => {
      expect(within(card).getAllByRole("list", { name: "Review’s states" })).toHaveLength(2);
    });
    expect(within(card).queryByRole("checkbox")).not.toBeInTheDocument();
    expect(within(card).queryByRole("radio")).not.toBeInTheDocument();
  });

  it("is linked from the project's settings, with what's applied", async () => {
    onStarters(starter({ applied: { version: "2.0.0", flow: "regulated_qc" } }));
    const { user } = await renderApp("/projects/assay-core/settings/");
    const list = await screen.findByRole("list", { name: "Starters" });
    expect(within(list).getByText("Applied: Regulated QC")).toBeInTheDocument();
    await user.click(screen.getByRole("link", { name: "Set it up from a starter" }));
    expect(await setupCard()).toBeInTheDocument();
  });
});

const SHARED = "iki/lab_roles.";
const [LAB_ANALYST, LAB_REVIEWER, LAB_QA] = [
  `${SHARED}analyst`,
  `${SHARED}reviewer`,
  `${SHARED}qa`,
];
const SEARCH_REVIEW = "iki/proteomics.search_review";
const SEARCH_RESULT = "iki/proteomics.ProteomicsResult";
const REGULATED_STEPS = {
  steps: [
    { state: "submitted", label: "Submitted", slots: ["analyst"] },
    { state: "reviewed", label: "Independently reviewed", slots: ["reviewer"] },
    { state: "released", label: "QA released", slots: ["qa"] },
  ],
  apart: [
    ["submitted", "reviewed"],
    ["submitted", "released"],
    ["reviewed", "released"],
  ],
};

/** A plugin's starter for core's result review (ADR 0303), as `GET /starters` lists it. */
function procedureStarter(more: Partial<Schemas["StarterOut"]> = {}): Schemas["StarterOut"] {
  const slot = (id: string, label: string, role: string, minimum: "viewer" | "editor") => ({
    id,
    label,
    description: "",
    role,
    starter_role: role,
    minimum_project_role: minimum,
  });
  return {
    id: SEARCH_REVIEW,
    version: "1.0.0",
    label: "Proteomics search results",
    description: "Choose how newly imported search results are reviewed, and who signs them.",
    governs: "proteomics results",
    procedure: "iki/core.result_review@1",
    suggested: "research",
    flows: [
      {
        id: "research",
        label: "Research",
        result_type: SEARCH_RESULT,
        states: ["recorded", "signed_off"],
        required_roles: [LAB_REVIEWER],
        independent_roles: [],
        summary: "One reviewer, who didn’t make or edit it, signs it off.",
        steps: [{ state: "signed_off", label: "Signed off", slots: ["reviewer"] }],
        apart: [],
      },
      {
        id: "regulated_qc",
        label: "Regulated QC",
        result_type: SEARCH_RESULT,
        states: ["recorded", "submitted", "reviewed", "released"],
        required_roles: [LAB_ANALYST, LAB_REVIEWER, LAB_QA],
        independent_roles: [LAB_ANALYST, LAB_REVIEWER, LAB_QA],
        summary: "Submitted, independently reviewed and released by QA.",
        ...REGULATED_STEPS,
      },
    ],
    roles: [
      { id: LAB_ANALYST, label: "Analyst", minimum_project_role: "editor" },
      { id: LAB_REVIEWER, label: "Reviewer", minimum_project_role: "viewer" },
      { id: LAB_QA, label: "Sample and result QA", minimum_project_role: "viewer" },
    ],
    slots: [
      slot("analyst", "Analyst", LAB_ANALYST, "editor"),
      slot("reviewer", "Reviewer", LAB_REVIEWER, "viewer"),
      slot("qa", "QA", LAB_QA, "viewer"),
    ],
    types: [SEARCH_RESULT],
    applied: null,
    chosen: null,
    default: null,
    ...more,
  };
}

describe("a plugin's starter", () => {
  it("needs someone for every step, with the steps the procedure keeps apart signed apart", () => {
    const roles = { analyst: LAB_ANALYST, reviewer: LAB_REVIEWER, qa: LAB_QA };
    const three = { [LAB_ANALYST]: ["a"], [LAB_REVIEWER]: ["b"], [LAB_QA]: ["c"] };
    expect(canStaff(REGULATED_STEPS, roles, three)).toBe(true);
    // One person can't submit, review and release.
    expect(canStaff(REGULATED_STEPS, roles, { ...three, [LAB_QA]: ["a"] })).toBe(false);
    // Two holders of a role leave room: b reviews, so QA can be c.
    expect(canStaff(REGULATED_STEPS, roles, { ...three, [LAB_QA]: ["a", "c"] })).toBe(true);
    // Steps nothing keeps apart may be one person's.
    expect(
      canStaff({ ...REGULATED_STEPS, apart: [] }, roles, {
        [LAB_ANALYST]: ["a"],
        [LAB_REVIEWER]: ["a"],
        [LAB_QA]: ["a"],
      }),
    ).toBe(true);
    // Two slots one role plays: its holders must be enough for both.
    const one = { analyst: LAB_ANALYST, reviewer: LAB_ANALYST, qa: LAB_QA };
    expect(canStaff(REGULATED_STEPS, one, { [LAB_ANALYST]: ["a"], [LAB_QA]: ["c"] })).toBe(false);
    expect(canStaff(REGULATED_STEPS, one, { [LAB_ANALYST]: ["a", "b"], [LAB_QA]: ["c"] })).toBe(
      true,
    );
  });

  it("starts from the deployment's default, checks the signers, and binds a part to another role", async () => {
    onStarters(
      procedureStarter({
        default: {
          procedure: "iki/core.result_review@1",
          flow: "research",
          version: "1.0.0",
          slots: { analyst: LAB_ANALYST, reviewer: LAB_REVIEWER, qa: LAB_QA },
          chosen_by: "Rosalind Franklin",
          chosen_at: "2026-11-20T10:00:00Z",
          reason: "SOP-QA-014 v3 covers discovery proteomics.",
        },
      }),
    );
    answer(`GET /api/v1/entity-types/${SEARCH_RESULT}/flow`, flowOf(SEARCH_RESULT));
    const { user } = await renderApp("/projects/assay-core/settings/starters/");
    const card = await screen.findByRole("region", { name: "Proteomics search results" });
    expect(within(card).getByText("Deployment default: Research")).toBeInTheDocument();
    expect(card).toHaveTextContent(
      "Until Assay Core chooses, new proteomics results follow the deployment’s default",
    );
    const flows = within(card).getByRole("group", { name: "Review flow" });
    expect(within(flows).getByRole("radio", { name: /^Research \(suggested\)/ })).toBeChecked();
    await user.click(within(flows).getByRole("radio", { name: /^Regulated QC/ }));

    const analyst = within(card).getByRole("group", { name: "Members who hold Analyst" });
    const reviewer = within(card).getByRole("group", { name: "Members who hold Reviewer" });
    const qa = within(card).getByRole("group", { name: "Members who hold Sample and result QA" });
    await user.click(within(analyst).getByRole("checkbox", { name: "Barbara McClintock" }));
    await user.click(within(reviewer).getByRole("checkbox", { name: "Barbara McClintock" }));
    await user.click(within(qa).getByRole("checkbox", { name: "Barbara McClintock" }));
    await user.click(within(card).getByRole("button", { name: "Preview" }));
    expect(within(card).getByRole("alert")).toHaveTextContent(
      "Choose enough different people that each step’s signer can be someone other",
    );
    expect(calls("POST")).toHaveLength(0);

    // The lab's own reviewers play Reviewer here.
    await user.selectOptions(
      within(card).getByRole("combobox", { name: "Lab role that plays Reviewer" }),
      "Assay reviewer",
    );
    const assayReviewers = within(card).getByRole("group", {
      name: "Members who hold Assay reviewer",
    });
    await user.click(within(assayReviewers).getByRole("checkbox", { name: "Carl Woese" }));
    await user.click(within(qa).getByRole("checkbox", { name: "Barbara McClintock" }));
    await user.click(within(qa).getByRole("checkbox", { name: "Dorothy Hodgkin" }));
    const plan = {
      starter: SEARCH_REVIEW,
      version: "1.0.0",
      project: SERVICE.id,
      flow: "regulated_qc",
      result_type: SEARCH_RESULT,
      changed: true,
      role_assignments: {},
      slots: { analyst: LAB_ANALYST, reviewer: REVIEWER, qa: LAB_QA },
      grants_to_add: { [LAB_ANALYST]: ["m2"], [REVIEWER]: ["m3"], [LAB_QA]: ["m4"] },
      plan: "b2".repeat(32),
      replaces: { procedure: "iki/core.result_review@1", flow: "research", starter: null },
    };
    answer(`POST /api/v1/starters/${SEARCH_REVIEW}/preview`, plan);
    await user.click(within(card).getByRole("button", { name: "Preview" }));
    const preview = await within(card).findByRole("status", { name: "What applying will do" });
    expect(calls("POST", `/api/v1/starters/${SEARCH_REVIEW}/preview`)[0]?.body).toEqual({
      project: SERVICE.id,
      version: "1.0.0",
      flow: "regulated_qc",
      role_assignments: { [LAB_ANALYST]: ["m2"], [REVIEWER]: ["m3"], [LAB_QA]: ["m4"] },
      slots: { reviewer: REVIEWER },
    });
    expect(within(preview).getAllByRole("listitem")[0]).toHaveTextContent(
      "New proteomics results follow the Regulated QC flow, replacing the project’s current choice.",
    );
  });
});

describe("Admin → Review procedures", () => {
  it("sets a deployment default only with a reason, and clears it with one", async () => {
    signedIn({ can: { ...ME.can, manage_org: true } });
    answer("GET /api/v1/starters", [starter(), procedureStarter()]);
    answer(`GET /api/v1/entity-types/${SEARCH_RESULT}/flow`, flowOf(SEARCH_RESULT));
    const set: Schemas["StarterDefaultOut"] = {
      starter: SEARCH_REVIEW,
      default: {
        procedure: "iki/core.result_review@1",
        flow: "regulated_qc",
        version: "1.0.0",
        slots: { analyst: LAB_ANALYST, reviewer: LAB_REVIEWER, qa: LAB_QA },
        chosen_by: "Rosalind Franklin",
        chosen_at: "2026-11-27T10:00:00Z",
        reason: "SOP-QA-014 v3: proteomics results are released by QA.",
      },
    };
    answer(`POST /api/v1/starters/${SEARCH_REVIEW}/default`, set);
    const { user } = await renderApp("/admin/review-procedures/");
    // Only a plugin's starter chooses a procedure: the server's own recipes aren't here.
    expect(screen.queryByRole("region", { name: "Dose-response plate assay" })).toBeNull();
    const card = await screen.findByRole("region", { name: "Proteomics search results" });
    expect(within(card).getByText("No default")).toBeInTheDocument();
    expect(card).toHaveTextContent(
      "Projects that haven’t chosen can’t create proteomics results until they do.",
    );
    await user.click(within(card).getByRole("radio", { name: /^Regulated QC/ }));
    await user.click(within(card).getByRole("button", { name: "Set as the default" }));
    expect(within(card).getByText(/Say why: a deployment’s default/)).toBeInTheDocument();
    expect(calls("POST")).toHaveLength(0);

    answer("GET /api/v1/starters", [procedureStarter({ default: set.default })]);
    await user.type(
      within(card).getByRole("textbox", { name: /Reason/ }),
      "SOP-QA-014 v3: proteomics results are released by QA.",
    );
    await user.click(within(card).getByRole("button", { name: "Set as the default" }));
    expect(
      await screen.findByText(
        "New proteomics results default to Regulated QC where a project hasn’t chosen.",
      ),
    ).toBeVisible();
    expect(calls("POST", `/api/v1/starters/${SEARCH_REVIEW}/default`)[0]?.body).toEqual({
      version: "1.0.0",
      flow: "regulated_qc",
      slots: {},
      reason: "SOP-QA-014 v3: proteomics results are released by QA.",
    });
    expect(await within(card).findByText("Default: Regulated QC")).toBeInTheDocument();

    answer(`POST /api/v1/starters/${SEARCH_REVIEW}/default/clear`, {
      starter: SEARCH_REVIEW,
      default: null,
    });
    await user.click(within(card).getByRole("button", { name: "Clear the default" }));
    const dialog = await screen.findByRole("alertdialog", {
      name: "Clear the default for proteomics results?",
    });
    expect(within(dialog).getByRole("button", { name: "Clear" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    await user.type(within(dialog).getByRole("textbox", { name: /Reason/ }), "SOP retired.");
    await user.click(within(dialog).getByRole("button", { name: "Clear" }));
    expect(await screen.findByText("Cleared the default for proteomics results.")).toBeVisible();
    expect(calls("POST", `/api/v1/starters/${SEARCH_REVIEW}/default/clear`)[0]?.body).toEqual({
      reason: "SOP retired.",
    });
  });
});
