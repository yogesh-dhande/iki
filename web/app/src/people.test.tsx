/**
 * People in records (ADR 0122) against a fake API: whom a record names and taking it,
 * whose work it is in its state fields' states (ADR 0125), My work, finding records by
 * whom they name, and reassigning many from the grid.
 */

import type { Schemas } from "@ikisci/sdk";
import { act, screen, waitFor, within } from "@testing-library/react";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { entity, field, KINASE, nth, PROTEIN, renderApp, withTypes } from "./entity-fixtures";
import { ME as SEARCHER, searchWords, validateFilters } from "./filters";
import { narrowNameable } from "./people";
import { answer, calls, FakeEventSource, ME, page, problem, type Sent } from "./test-server";

const SAMPLE_ID = "00000000-0000-7000-8000-00000000e601";
const ANA = "00000000-0000-7000-8000-0000000000a1";
const BO = "00000000-0000-7000-8000-0000000000b0";

const RECEIVED: Schemas["StateOut"] = {
  name: "received",
  label: "Received",
  description: null,
  set_by: ["iki/lims.analyst"],
  locks: false,
  holds_amount: false,
  reason: null,
  signatures: [],
};
const IN_TESTING: Schemas["StateOut"] = { ...RECEIVED, name: "in_testing", label: "In testing" };
const IN_REVIEW: Schemas["StateOut"] = { ...RECEIVED, name: "in_review", label: "In review" };

const STATUS = field({
  name: "status",
  label: "Status",
  type: "state",
  plugin: "iki/lims",
  initial: "received",
  states: [RECEIVED, IN_TESTING, IN_REVIEW],
});

const ANALYST = field({
  name: "analyst",
  label: "Analyst",
  type: "person",
  role: "iki/lims.analyst",
  role_label: "Analyst",
  assignment: true,
  assignment_field: "status",
  assignment_states: ["received", "in_testing"],
});
const REVIEWER = field({
  name: "reviewer",
  label: "Reviewer",
  type: "person",
  role: "iki/lims.reviewer",
  role_label: "Reviewer",
  assignment: true,
  assignment_field: "status",
  assignment_states: ["in_review"],
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
  fields: [nth(PROTEIN.fields, 0), STATUS, ANALYST, REVIEWER],
};

function person(id: string, name: string, email: string | null = null): Schemas["PersonOut"] {
  return { id, name, email, active: true };
}

function sample(overrides: Partial<Schemas["EntityOut"]> = {}): Schemas["EntityOut"] {
  return entity({
    id: SAMPLE_ID,
    hrid: "SMP-000001",
    type: "iki/lims.Sample",
    title: "Lot 7",
    revision: 2,
    values: { name: "Lot 7", status: "in_testing", analyst: ANA, reviewer: null },
    people: { [ANA]: person(ANA, "Ana Lima", "ana@example.com") },
    needs_reassigning: [],
    can: {
      edit: true,
      correct: false,
      edit_amount: false,
      draw: false,
      take: [],
      states: {},
      sign: [],
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

describe("whom a record names", () => {
  it("shows its assignees, whose work it is now, and who needs replacing", async () => {
    onSample(
      sample({
        values: { name: "Lot 7", status: "in_testing", analyst: ANA, reviewer: BO },
        people: {
          [ANA]: person(ANA, "Ana Lima", "ana@example.com"),
          [BO]: { ...person(BO, "Bo Chen"), active: false },
        },
        needs_reassigning: [{ field: "reviewer", person: BO, why: "They were deactivated." }],
      }),
    );
    await renderApp("/entities/SMP-000001/");
    const card = await screen.findByRole("region", { name: "Assigned" });
    const rows = within(card).getAllByRole("listitem");
    expect(nth(rows, 0)).toHaveTextContent("Analyst: Ana Lima");
    expect(nth(rows, 0)).toHaveTextContent("Their work now: its Status is In testing.");
    expect(nth(rows, 1)).toHaveTextContent("Reviewer: Bo Chen (deactivated) Needs reassigning");
    expect(nth(rows, 1)).toHaveTextContent("Their work once its Status is In review.");
    expect(within(card).getByRole("note")).toHaveTextContent(
      "Bo Chen (deactivated) can’t be Reviewer any more (They were deactivated.)",
    );
  });

  it("lets someone with the field's lab role take open work", async () => {
    onSample(
      sample({
        values: { name: "Lot 7", status: "in_review", analyst: ANA, reviewer: null },
        can: {
          edit: true,
          correct: false,
          edit_amount: false,
          draw: false,
          take: ["reviewer"],
          states: {},
          sign: [],
        },
      }),
    );
    const taken = sample({
      revision: 3,
      values: { name: "Lot 7", status: "in_review", analyst: ANA, reviewer: ME.id },
      people: {
        [ANA]: person(ANA, "Ana Lima", "ana@example.com"),
        [ME.id]: person(ME.id, ME.name, ME.email),
      },
      can: {
        edit: true,
        correct: false,
        edit_amount: false,
        draw: false,
        take: [],
        states: {},
        sign: [],
      },
    });
    answer(`POST /api/v1/entities/${SAMPLE_ID}/take`, taken);
    const { user } = await renderApp("/entities/SMP-000001/");
    const card = await screen.findByRole("region", { name: "Assigned" });
    await user.click(within(card).getByRole("button", { name: "Take it as Reviewer" }));
    expect(await screen.findByText("SMP-000001 is yours, as Reviewer.")).toBeInTheDocument();
    const sent = calls("POST", `/api/v1/entities/${SAMPLE_ID}/take`);
    expect(sent.at(-1)?.body).toEqual({ field: "reviewer", reason: "" });
    expect(sent.at(-1)?.headers.get("If-Match")).toBe('"2"');
  });
});

describe("naming someone", () => {
  it("picks from the project's people with the field's role", async () => {
    onSample();
    answer(`GET /api/v1/entities/SMP-000001/provenance`, {});
    answer(`GET /api/v1/entities/SMP-000001/projects`, []);
    answer(`GET /api/v1/projects/${KINASE.id}/people`, [
      { id: BO, name: "Bo Chen", email: "bo@example.com" },
    ]);
    answer(`PATCH /api/v1/entities/${SAMPLE_ID}`, sample({ revision: 3 }));
    const { user } = await renderApp("/entities/SMP-000001/edit/");
    const picker = await screen.findByRole("combobox", { name: /^Reviewer/ });
    expect(picker).toHaveAccessibleDescription(
      /^Type a name or email address: someone with the Reviewer role in this project\./,
    );
    await user.type(picker, "Bo");
    await user.click(await screen.findByRole("option", { name: /Bo Chen/ }));
    const asked = calls("GET", `/api/v1/projects/${KINASE.id}/people`);
    expect(asked.at(-1)?.query.get("role")).toBe("iki/lims.reviewer");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await screen.findByText("Saved revision 3 of SMP-000001.");
    const sent = calls("PATCH", `/api/v1/entities/${SAMPLE_ID}`);
    expect(sent.at(-1)?.body).toEqual({ values: { reviewer: BO }, reason: "" });
  });
});

describe("suggesting people as they're typed", () => {
  const ana = { id: ANA, name: "Ana Lima", email: "ana@example.com" };
  const bo = { id: BO, name: "Bo Chen", email: "bo@example.com" };
  const cy = { id: `${BO.slice(0, -1)}c`, name: "Cy Bolt", email: "cy@example.com" };

  it("works out the server's answer from a whole earlier one, in its order", () => {
    // Those starting with the text first ("Bolt" starts a word), then by name.
    expect(narrowNameable([ana, bo, cy], "", "bo")).toEqual([bo, cy]);
    expect(narrowNameable([cy, bo], "b", "BO ")).toEqual([bo, cy]);
    expect(narrowNameable([ana, bo, cy], "", "lima")).toEqual([ana]);
    expect(narrowNameable([ana, bo, cy], "", "example")).toEqual([ana, bo, cy]);
    expect(narrowNameable([ana, bo, cy], "", "zed")).toEqual([]);
  });

  it("doesn't guess when the earlier answer can't hold everyone who matches", () => {
    // Other text, not more of it: people it left out may match.
    expect(narrowNameable([bo], "bo", "b")).toBeUndefined();
    expect(narrowNameable([bo], "bo", "ana")).toBeUndefined();
    // Cut at the limit: others may match too.
    const many = Array.from({ length: 20 }, (_, i) => ({
      ...bo,
      id: `${BO.slice(0, -2)}${String(10 + i)}`,
    }));
    expect(narrowNameable(many, "", "bo")).toBeUndefined();
  });

  it("shows only those matching what's typed, which stay put when the server answers", async () => {
    onSample();
    let release = (): void => undefined;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    answer(`GET /api/v1/projects/${KINASE.id}/people`, async (call: Sent) => {
      if (!call.query.get("q")) return [ana, bo, cy];
      await held;
      return narrowNameable([ana, bo, cy], "", call.query.get("q") ?? "");
    });
    const { user } = await renderApp("/entities/SMP-000001/edit/");
    const picker = await screen.findByRole("combobox", { name: /^Reviewer/ });
    await user.click(picker);
    await screen.findByRole("option", { name: /Ana Lima/ });
    await user.type(picker, "bo");
    // The server hasn't answered "bo" yet, and Ana, who doesn't match, is gone already.
    const shown = screen.getAllByRole("option");
    expect(shown.map((o) => o.textContent)).toEqual([
      "Bo Chen bo@example.com",
      "Cy Bolt cy@example.com",
    ]);
    expect(screen.queryByText("Searching…")).not.toBeInTheDocument();
    await act(async () => {
      release();
      await held;
    });
    await waitFor(() => {
      expect(calls("GET", `/api/v1/projects/${KINASE.id}/people`).at(-1)?.query.get("q")).toBe(
        "bo",
      );
    });
    // The same options, in the same place: none moved under the pointer.
    expect(screen.getAllByRole("option")).toEqual(shown);
    await user.click(screen.getByRole("option", { name: /Bo Chen/ }));
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(picker).toHaveValue("Bo Chen (bo@example.com)");
  });

  it("closes on Escape, keeping what's typed, and clears it on leaving if nobody's chosen", async () => {
    onSample();
    answer(`GET /api/v1/projects/${KINASE.id}/people`, (call: Sent) =>
      narrowNameable([ana, bo, cy], "", call.query.get("q") ?? ""),
    );
    const { user } = await renderApp("/entities/SMP-000001/edit/");
    const picker = await screen.findByRole("combobox", { name: /^Reviewer/ });
    await user.type(picker, "bo");
    expect(await screen.findByRole("option", { name: /Bo Chen/ })).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(picker).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(picker).toHaveValue("bo");
    await user.tab();
    expect(picker).toHaveValue("");
    expect(picker).toHaveAttribute("aria-expanded", "false");
  });

  it("says it's searching, rather than show people another text found", async () => {
    onSample();
    const many = Array.from({ length: 20 }, (_, i) => ({
      id: `${BO.slice(0, -2)}${String(10 + i)}`,
      name: `Person ${String(10 + i)}`,
      email: `p${String(10 + i)}@example.com`,
    }));
    answer(`GET /api/v1/projects/${KINASE.id}/people`, async (call: Sent) => {
      if (!call.query.get("q")) return many;
      return new Promise(() => undefined);
    });
    const { user } = await renderApp("/entities/SMP-000001/edit/");
    const picker = await screen.findByRole("combobox", { name: /^Reviewer/ });
    await user.click(picker);
    await screen.findByRole("option", { name: /Person 10/ });
    await user.type(picker, "bo");
    expect(await screen.findByText("Searching…")).toBeInTheDocument();
    expect(screen.queryByRole("option")).not.toBeInTheDocument();
  });
});

describe("Home work and projects", () => {
  function holdAnswer(path: string, value: unknown) {
    let release = (): void => undefined;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    answer(`GET ${path}`, async () => {
      await held;
      return value;
    });
    return () => {
      release();
      return held;
    };
  }

  function yourWork() {
    const section = within(screen.getByRole("region", { name: "Your work" }));
    expect(section.getByRole("link", { name: "Go to My work" })).toHaveAttribute("href", "/work/");
    return section;
  }

  it("keeps ready projects available and focused while assigned work loads", async () => {
    withTypes();
    const releaseWork = holdAnswer("/api/v1/me/work", page([]));
    answer("GET /api/v1/projects", page([KINASE]));
    await renderApp("/");
    const home = within(screen.getByRole("main"));
    expect(home.getByRole("heading", { name: "Welcome, Rosalind" })).toBeVisible();
    expect(home.getByRole("link", { name: "New project" })).toHaveAttribute(
      "href",
      "/projects/new/",
    );
    const project = await home.findByRole("link", { name: KINASE.name });
    expect(calls("GET", "/api/v1/me/work")).toHaveLength(1);
    expect(project).toHaveAttribute("href", "/projects/kinase-atlas/");
    expect(yourWork().getByRole("status")).toHaveTextContent(/loading/i);
    project.focus();

    await act(releaseWork);
    expect(await yourWork().findByText("Nothing’s assigned to you.")).toBeVisible();
    expect(project).toHaveFocus();
  });

  it("requests both lists at once and keeps a focused assignment when projects arrive", async () => {
    withTypes();
    answer("GET /api/v1/entity-types", [SAMPLE]);
    const releaseWork = holdAnswer(
      "/api/v1/me/work",
      page([{ entity: sample(), fields: ["analyst"], due: null }]),
    );
    const releaseProjects = holdAnswer("/api/v1/projects", page([KINASE]));
    await renderApp("/");
    const home = within(screen.getByRole("main"));
    expect(home.getByRole("link", { name: "New project" })).toBeVisible();
    await waitFor(() => {
      expect(calls("GET", "/api/v1/me/work")).toHaveLength(1);
      expect(calls("GET", "/api/v1/projects").some((c) => c.query.get("member") === "true")).toBe(
        true,
      );
    });
    await act(releaseWork);
    const assigned = await home.findByRole("link", { name: "Lot 7 (SMP-000001)" });
    expect(yourWork().getByRole("link", { name: "Lot 7 (SMP-000001)" })).toBe(assigned);
    expect(home.queryByRole("link", { name: KINASE.name })).not.toBeInTheDocument();
    assigned.focus();

    await act(releaseProjects);
    expect(await home.findByRole("link", { name: KINASE.name })).toBeVisible();
    expect(assigned).toHaveFocus();
  });

  it("keeps projects usable while state labels for assigned work arrive", async () => {
    withTypes();
    const releaseTypes = holdAnswer("/api/v1/entity-types", [SAMPLE]);
    answer("GET /api/v1/me/work", page([{ entity: sample(), fields: ["analyst"], due: null }]));
    answer("GET /api/v1/projects", page([KINASE]));
    await renderApp("/");
    const home = within(screen.getByRole("main"));
    const project = await home.findByRole("link", { name: KINASE.name });
    expect(home.getByRole("link", { name: "New project" })).toBeVisible();
    expect(yourWork().queryByText(/In testing/)).not.toBeInTheDocument();
    project.focus();

    await act(releaseTypes);
    expect(await yourWork().findByText(/In testing/)).toBeVisible();
    expect(project).toHaveFocus();
  });

  it("shows the first five assignments in server order with a link to all work", async () => {
    withTypes();
    answer("GET /api/v1/entity-types", [SAMPLE]);
    answer("GET /api/v1/projects", page([]));
    const assigned = Array.from({ length: 6 }, (_, i) => {
      const n = String(i + 1);
      return {
        entity: sample({
          id: `${SAMPLE_ID.slice(0, -1)}${n}`,
          hrid: `SMP-00000${n}`,
          title: `Lot ${n}`,
        }),
        fields: ["analyst"],
        due: null,
      };
    });
    answer("GET /api/v1/me/work", page(assigned));
    await renderApp("/");
    await screen.findByRole("link", { name: "Lot 1 (SMP-000001)" });
    const rows = yourWork().getAllByRole("listitem");
    expect(rows.map((row) => within(row).getByRole("link").textContent)).toEqual(
      assigned.slice(0, 5).map(({ entity }) => `${entity.title} (${entity.hrid})`),
    );
  });

  it("explains a work failure in Your work and keeps its navigation and projects available", async () => {
    withTypes();
    answer("GET /api/v1/projects", page([KINASE]));
    answer(
      "GET /api/v1/me/work",
      problem(403, "forbidden", "Your assigned work could not be read."),
    );
    await renderApp("/");
    const home = within(screen.getByRole("main"));
    expect(await home.findByRole("link", { name: KINASE.name })).toBeVisible();
    expect(await yourWork().findByRole("alert")).toHaveTextContent(
      "Your assigned work could not be read.",
    );
  });
});

describe("My work", () => {
  it("lists what's assigned to you, soonest due first, and filters it", async () => {
    withTypes();
    answer("GET /api/v1/entity-types", [PROTEIN, SAMPLE]);
    answer("GET /api/v1/projects", page([KINASE]));
    answer(
      "GET /api/v1/me/work",
      page([
        { entity: sample(), fields: ["analyst"], due: "2020-01-02" },
        {
          entity: sample({ id: `${SAMPLE_ID.slice(0, -1)}2`, hrid: "SMP-000002", title: "Lot 8" }),
          fields: ["analyst"],
          due: null,
        },
      ]),
    );
    const { router, user } = await renderApp("/work/");
    const list = await screen.findByRole("list", { name: "Your work" });
    const rows = within(list).getAllByRole("listitem");
    expect(nth(rows, 0)).toHaveTextContent("Lot 7 (SMP-000001) In testing Overdue");
    expect(nth(rows, 0)).toHaveTextContent("You’re its Analyst · Due");
    expect(within(nth(rows, 1)).queryByText("Overdue")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Assigned to me" })).toHaveAttribute(
      "aria-current",
      "page",
    );

    await user.selectOptions(screen.getByRole("combobox", { name: "Type" }), "Samples");
    await user.selectOptions(screen.getByRole("combobox", { name: "State" }), "Status: In testing");
    await waitFor(() => {
      expect(router.state.location.search).toEqual({
        type: "iki/lims.Sample",
        state: "status.in_testing",
      });
    });
    const sent = calls("GET", "/api/v1/me/work");
    expect(sent.at(-1)?.query.getAll("type")).toEqual(["iki/lims.Sample"]);
    expect(sent.at(-1)?.query.getAll("state")).toEqual(["status.in_testing"]);
    expect(sent.at(-1)?.query.get("sort")).toBe("due");
  });

  it("offers work open to your lab roles to take", async () => {
    withTypes();
    answer("GET /api/v1/entity-types", [PROTEIN, SAMPLE]);
    answer("GET /api/v1/projects", page([KINASE]));
    answer(
      "GET /api/v1/me/work/open",
      page([
        {
          entity: sample({
            values: { name: "Lot 7", status: "in_review", analyst: ANA, reviewer: null },
          }),
          fields: ["reviewer"],
          due: null,
        },
      ]),
    );
    answer(`POST /api/v1/entities/${SAMPLE_ID}/take`, sample({ revision: 3 }));
    const { user } = await renderApp("/work/?open=true");
    const list = await screen.findByRole("list", { name: "Open work" });
    expect(list).toHaveTextContent("Open as Reviewer");
    await user.click(within(list).getByRole("button", { name: "Take SMP-000001 as Reviewer" }));
    expect(await screen.findByText("SMP-000001 is yours, as Reviewer.")).toBeInTheDocument();
  });

  describe("after events were missed", () => {
    afterEach(() => {
      vi.unstubAllGlobals();
    });

    it.each(["resync", "ready"])(
      "reads it again on %s, so work reassigned meanwhile goes",
      async (event) => {
        FakeEventSource.opened = [];
        vi.stubGlobal("EventSource", FakeEventSource);
        withTypes();
        answer("GET /api/v1/entity-types", [PROTEIN, SAMPLE]);
        answer("GET /api/v1/projects", page([KINASE]));
        let work = [{ entity: sample(), fields: ["analyst"], due: null }];
        answer("GET /api/v1/me/work", () => page(work));
        await renderApp("/work/");
        const list = await screen.findByRole("list", { name: "Your work" });
        expect(within(list).getAllByRole("listitem")).toHaveLength(1);
        const stream = FakeEventSource.current();
        expect(stream).toBeDefined();

        // Reassigned while the server dropped this tab's events (or its stream was down):
        // no `work` event comes.
        work = [];
        act(() => {
          stream?.send(event, event === "ready" ? { unread: 0 } : {});
        });
        expect(
          await screen.findByRole("heading", { name: "Nothing’s assigned to you" }),
        ).toBeInTheDocument();
      },
    );
  });

  it("says when nothing is yours", async () => {
    withTypes();
    answer("GET /api/v1/projects", page([KINASE]));
    answer("GET /api/v1/me/work", page([]));
    await renderApp("/work/");
    expect(
      await screen.findByRole("heading", { name: "Nothing’s assigned to you" }),
    ).toBeInTheDocument();
  });
});

describe("finding records by whom they name", () => {
  it("reads people's words in the search box as conditions", () => {
    expect(searchWords("assigned:me lot 7")).toEqual({
      text: "lot 7",
      conditions: [{ field: "assigned", op: "eq", value: SEARCHER }],
    });
    expect(searchWords("assigned:none needs:reassigning").conditions).toEqual([
      { field: "assigned", op: "is_set", value: false },
      { field: "needs_reassigning", op: "eq", value: true },
    ]);
    expect(searchWords("reviewer:@ana@example.com").conditions).toEqual([
      { field: "reviewer", op: "eq", value: "ana@example.com" },
    ]);
    // Anything else is text, as typed.
    expect(searchWords("http://x ratio:1:2 @ana")).toEqual({
      text: "http://x ratio:1:2 @ana",
      conditions: [],
    });
  });

  it("keeps “is me” in the address, for whoever opens it", () => {
    const filters = [{ field: "analyst", op: "eq", value: SEARCHER }];
    expect(validateFilters(filters)).toEqual(filters);
  });
});

describe("reassigning from the grid", () => {
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

  it("names someone in the picked records, and says what it left as it was", async () => {
    withTypes();
    answer("GET /api/v1/entity-types", [PROTEIN, SAMPLE]);
    answer("GET /api/v1/projects", page([KINASE]));
    const second = sample({ id: `${SAMPLE_ID.slice(0, -1)}2`, hrid: "SMP-000002" });
    answer(
      "POST /api/v1/entities/search",
      page([
        sample({ needs_reassigning: [{ field: "analyst", person: ANA, why: "They left." }] }),
        second,
      ]),
    );
    answer(`GET /api/v1/projects/${KINASE.id}/people`, [
      { id: BO, name: "Bo Chen", email: "bo@example.com" },
    ]);
    answer("POST /api/v1/entities/assign", {
      changed: [SAMPLE_ID],
      skipped: [{ entity: second.id, hrid: "SMP-000002", why: "Its state locks it." }],
      group: "g1",
    });
    const { user } = await renderApp("/projects/kinase-atlas/entities/?type=iki/lims.Sample");
    const grid = await screen.findByRole("table", { name: "2 samples" });
    const rows = within(grid).getAllByRole("row");
    expect(nth(rows, 1)).toHaveTextContent("Ana LimaNeeds reassigning");
    await user.click(within(nth(rows, 1)).getByRole("checkbox"));
    await user.click(within(nth(rows, 2)).getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: "Reassign" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Reassign 2 samples" });
    await user.selectOptions(within(dialog).getByRole("combobox", { name: "Field" }), "Analyst");
    await user.type(within(dialog).getByRole("combobox", { name: /^Analyst/ }), "Bo");
    await user.click(await screen.findByRole("option", { name: /Bo Chen/ }));
    await user.type(within(dialog).getByRole("textbox", { name: /Reason/ }), "Ana left");
    await user.click(within(dialog).getByRole("button", { name: "Reassign" }));
    const done = await screen.findByRole("alertdialog", { name: "Reassigned 1 of 2" });
    expect(done).toHaveTextContent("SMP-000002: Its state locks it.");
    expect(calls("POST", "/api/v1/entities/assign").at(-1)?.body).toEqual({
      entities: [SAMPLE_ID, second.id],
      field: "analyst",
      person: BO,
      op: "set",
      reason: "Ana left",
    });
  });
});
