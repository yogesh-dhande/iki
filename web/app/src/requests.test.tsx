/**
 * Requests between projects (ADR 0132), against a fake API: a request's two sides on its
 * page, sending it to a service, each side's form offering only its own fields, a result's
 * note, what a service takes in its settings, and its queue in the grid.
 */

import type { Schemas } from "@ikisci/sdk";
import { screen, waitFor, within } from "@testing-library/react";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import {
  addFilter,
  entity,
  field,
  filterFields,
  KINASE,
  nth,
  PROTEIN,
  renderApp,
  withTypes,
} from "./entity-fixtures";
import { answer, calls, MAIN_REGISTRY, page, problem, project, respond } from "./test-server";

const CORE = project({
  id: "00000000-0000-7000-8000-0000000000p2",
  name: "Protein Core",
  slug: "protein-core",
  description: "Assays",
});
const ELSEWHERE = project({
  id: "00000000-0000-7000-8000-0000000000p3",
  name: "Partner Lab",
  slug: "partner-lab",
  registry: { id: "00000000-0000-7000-8000-0000000000r2", name: "Partners", slug: "partners" },
});
const ref = (p: Schemas["ProjectOut"]) => ({ id: p.id, name: p.name, slug: p.slug });

const ASK_ID = "00000000-0000-7000-8000-00000000a501";
const LOT_ID = "00000000-0000-7000-8000-00000000a511";
const RESULT_ID = "00000000-0000-7000-8000-00000000a521";

function state(name: string, label: string): Schemas["StateOut"] {
  return {
    name,
    label,
    description: null,
    set_by: ["acme/protein.qa"],
    locks: false,
    holds_amount: false,
    reason: null,
    signatures: [],
  };
}

const SAMPLE: Schemas["EntityTypeOut"] = {
  ...PROTEIN,
  name: "acme/protein.Sample",
  plugin: "acme/protein",
  label: "Sample",
  label_plural: "Samples",
  description: null,
  hrid_prefix: "SMP",
  identity: [],
  fields: [field({ name: "label", label: "Label", type: "string", plugin: "acme/protein" })],
};

const ASK: Schemas["EntityTypeOut"] = {
  ...SAMPLE,
  name: "acme/protein.AssayRequest",
  label: "Assay request",
  label_plural: "Assay requests",
  description: "Samples a project sends a core facility to measure.",
  hrid_prefix: "ARQ",
  request: {
    service_fields: ["status", "priority", "technician", "core_notes"],
    sends: ["samples"],
  },
  fields: [
    field({ name: "title", label: "Title", type: "string", required: true }),
    field({
      name: "samples",
      label: "Samples",
      type: "list",
      items: "ref",
      to: ["acme/protein.Sample"],
    }),
    field({ name: "instructions", label: "Instructions", type: "text" }),
    field({ name: "priority", label: "Priority", type: "enum", values: ["routine", "urgent"] }),
    field({
      name: "technician",
      label: "Technician",
      type: "person",
      role: "acme/protein.analyst",
      role_label: "Analyst",
      assignment: true,
      assignment_field: "status",
      assignment_states: ["accepted"],
    }),
    field({ name: "core_notes", label: "Core notes", type: "text" }),
    field({
      name: "status",
      label: "Status",
      type: "state",
      initial: "submitted",
      states: [state("submitted", "Submitted"), state("accepted", "Accepted")],
    }),
  ],
};

const RESULT: Schemas["EntityTypeOut"] = {
  ...SAMPLE,
  name: "acme/protein.AssayResult",
  label: "Assay result",
  label_plural: "Assay results",
  hrid_prefix: "ARS",
  result: { of: "request", released_field: "review", released_states: ["released"] },
  fields: [
    field({ name: "request", label: "Request", type: "ref", to: [ASK.name], required: true }),
    field({ name: "purity", label: "Purity", type: "float" }),
    field({
      name: "review",
      label: "Review",
      type: "state",
      initial: "draft",
      states: [state("draft", "Draft"), state("released", "Released")],
    }),
  ],
};

const LOT: Schemas["RefOut"] = {
  hrid: "SMP-000001",
  title: "Lot 7",
  type: SAMPLE.name,
  archived: false,
  purged: false,
};

function sides(overrides: Partial<Schemas["RequestSidesOut"]> = {}): Schemas["RequestSidesOut"] {
  return {
    asking_project: ref(KINASE),
    service: null,
    submitted: false,
    submitted_at: null,
    submitted_by: null,
    service_fields: ["status", "priority", "technician", "core_notes"],
    sends: ["samples"],
    yours: ["asker"],
    ...overrides,
  };
}

const SENT = {
  service: ref(CORE),
  submitted: true,
  submitted_at: "2026-03-16T09:00:00Z",
  submitted_by: {
    id: "00000000-0000-7000-8000-000000000001",
    name: "Rosalind Franklin",
    email: "rosalind@example.com",
    active: true,
    kind: "person" as const,
    imported_from: "",
  },
};

function request(
  more: Partial<Schemas["EntityOut"]> = {},
  side: Partial<Schemas["RequestSidesOut"]> = {},
): Schemas["EntityOut"] {
  return entity({
    id: ASK_ID,
    hrid: "ARQ-000001",
    type: ASK.name,
    title: "Purity of the kinases",
    revision: 1,
    values: {
      title: "Purity of the kinases",
      samples: [LOT_ID],
      instructions: "Desalt first",
      priority: "routine",
      core_notes: null,
      status: "submitted",
    },
    refs: { [LOT_ID]: LOT },
    request: sides(side),
    ...more,
  });
}

function lot(): Schemas["EntityOut"] {
  return entity({
    id: LOT_ID,
    hrid: "SMP-000001",
    type: SAMPLE.name,
    title: "Lot 7",
    values: { label: "Lot 7" },
  });
}

const SERVICES: Schemas["ServiceOut"][] = [
  {
    project: ref(CORE),
    description: "Assays",
    types: [{ name: ASK.name, label: "Assay request" }],
  },
];

/** The projects the reader sees, and each by its slug. */
function seeing(...projects: Schemas["ProjectOut"][]): void {
  answer("GET /api/v1/projects", ({ query }: { query: URLSearchParams }) => {
    const slug = query.get("slug");
    return page(slug ? projects.filter((p) => p.slug === slug) : projects);
  });
}

function onRequest(found: Schemas["EntityOut"], ...projects: Schemas["ProjectOut"][]): void {
  withTypes();
  answer("GET /api/v1/entity-types", [SAMPLE, ASK, RESULT]);
  seeing(...(projects.length ? projects : [KINASE]));
  answer("GET /api/v1/entities/ARQ-000001", found);
  answer(`GET /api/v1/entities/${ASK_ID}`, found);
  answer("GET /api/v1/entities/ARQ-000001/provenance", {});
  answer("GET /api/v1/entities/ARQ-000001/projects", []);
  answer(`GET /api/v1/projects/${KINASE.id}/services`, SERVICES);
}

function region(name: string): HTMLElement {
  return screen.getByRole("region", { name });
}

describe("a request's page", () => {
  it("shows both sides, and whose each value is", async () => {
    onRequest(request());
    await renderApp("/entities/ARQ-000001/");
    const panel = await screen.findByRole("region", { name: "Request" });
    expect(
      within(panel).getByText(
        "Kinase Atlas asks a service for this. Once it’s sent, the service fills in Status, Priority, Technician and Core notes; until then, no one can.",
      ),
    ).toBeInTheDocument();
    expect(within(panel).getByText("Not sent yet")).toBeInTheDocument();
    const sends = within(panel).getByRole("list", { name: "What it sends" });
    expect(within(sends).getByRole("link", { name: "Lot 7" })).toHaveAttribute(
      "href",
      "/entities/SMP-000001/",
    );
    // The asking project's values, then the service's.
    const ours = region("Kinase Atlas’s values");
    expect(within(ours).getByText("Desalt first")).toBeInTheDocument();
    expect(within(ours).queryByText("Priority")).not.toBeInTheDocument();
    const theirs = region("The service’s values");
    expect(within(theirs).getByText("routine")).toBeInTheDocument();
    expect(
      within(theirs).getByText("The service it’s sent to fills these in, once it’s sent."),
    ).toBeInTheDocument();
  });

  it("says its service's states aren't set until it's sent, and then shows them", async () => {
    // A service state starts as its type's initial one, such as Submitted, which would
    // say it was sent: until it is, its page says it's not sent yet.
    onRequest(request());
    await renderApp("/entities/ARQ-000001/");
    // Its state pills sit beside its title (ADR 0176).
    const heading = (await screen.findByRole("heading", { level: 1 })).parentElement;
    expect(heading).toHaveTextContent(/Not sent/);
    expect(heading).not.toHaveTextContent(/Submitted/);
    const states = within(region("States")).getByRole("list", { name: "State fields" });
    expect(within(states).getByRole("listitem")).toHaveTextContent(
      /^Status: Not sent yetThe service sets it once it’s sent, starting from Submitted\./,
    );
    expect(within(region("The service’s values")).queryByText("Submitted")).toBeNull();
  });

  it("shows its service's states once it's sent", async () => {
    onRequest(request({}, SENT));
    answer(`GET /api/v1/entities/${ASK_ID}/request`, {
      request: request({}, SENT),
      asking_project: ref(KINASE),
      service: ref(CORE),
      submitted_at: SENT.submitted_at,
      submitted_by: SENT.submitted_by,
      sent: [lot()],
      results: [],
    });
    await renderApp("/entities/ARQ-000001/");
    const heading = (await screen.findByRole("heading", { level: 1 })).parentElement;
    expect(heading).toHaveTextContent(/Submitted/);
    expect(heading).not.toHaveTextContent(/Not sent/);
    expect(within(region("States")).getByText(/^Status: Submitted/)).toBeInTheDocument();
  });

  it("sends it to a service, saying it can't be taken back", async () => {
    onRequest(request());
    const { user } = await renderApp("/entities/ARQ-000001/");
    const panel = await screen.findByRole("region", { name: "Request" });
    const send = within(panel).getByRole("button", { name: "Send to a service…" });
    await waitFor(() => {
      expect(send).not.toHaveAttribute("aria-disabled");
    });
    await user.click(send);
    const dialog = await screen.findByRole("alertdialog", { name: "Send ARQ-000001 to a service" });
    // The only service it may go to is chosen.
    expect(within(dialog).getByRole("radio", { name: "Protein Core: Assays" })).toBeChecked();
    expect(
      within(dialog).getByText(
        "Protein Core reads this assay request and the records its Samples name, now or later: nothing else of Kinase Atlas’s.",
      ),
    ).toBeInTheDocument();
    expect(
      within(within(dialog).getByRole("list", { name: "Sent with it" })).getByRole("link", {
        name: "Lot 7",
      }),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByText(
        "It can’t be taken back: once it’s sent, it stays with Protein Core, and can’t go to another.",
      ),
    ).toBeInTheDocument();

    const sent = request({}, SENT);
    answer(`POST /api/v1/entities/${ASK_ID}/submit`, sent);
    answer("GET /api/v1/entities/ARQ-000001", sent);
    answer(`GET /api/v1/entities/${ASK_ID}/request`, {
      request: sent,
      asking_project: ref(KINASE),
      service: ref(CORE),
      submitted_at: SENT.submitted_at,
      submitted_by: SENT.submitted_by,
      sent: [lot()],
      results: [],
    });
    await user.type(within(dialog).getByRole("textbox", { name: /^Reason/ }), "Q4 batch");
    await user.click(within(dialog).getByRole("button", { name: "Send to Protein Core" }));
    expect(await screen.findByText("Sent ARQ-000001 to Protein Core.")).toBeInTheDocument();
    const posted = calls("POST", `/api/v1/entities/${ASK_ID}/submit`);
    expect(posted.at(-1)?.body).toEqual({ service: CORE.id, reason: "Q4 batch" });
    expect(posted.at(-1)?.headers.get("If-Match")).toBe('"1"');

    // It's the service's to work on now: no more sending.
    const now = region("Request");
    expect(await within(now).findByText("By Rosalind Franklin,", { exact: false })).toBeVisible();
    expect(
      within(await within(now).findByRole("list", { name: "What it sent" })).getByRole("link", {
        name: "Lot 7",
      }),
    ).toBeInTheDocument();
    expect(within(now).getByText("None released yet")).toBeInTheDocument();
    expect(within(now).queryByRole("button", { name: "Send to a service…" })).toBeNull();
    expect(region("Protein Core’s values")).toBeInTheDocument();
  });

  it("says when it changed since it was read, and when Iki refuses", async () => {
    onRequest(request());
    const { user } = await renderApp("/entities/ARQ-000001/");
    const panel = await screen.findByRole("region", { name: "Request" });
    const send = within(panel).getByRole("button", { name: "Send to a service…" });
    await waitFor(() => {
      expect(send).not.toHaveAttribute("aria-disabled");
    });
    await user.click(send);
    const dialog = await screen.findByRole("alertdialog", { name: "Send ARQ-000001 to a service" });
    answer(
      `POST /api/v1/entities/${ASK_ID}/submit`,
      problem(412, "stale", "Someone changed it since you read it."),
    );
    await user.click(within(dialog).getByRole("button", { name: "Send to Protein Core" }));
    expect(
      await within(dialog).findByText(
        "Someone changed it while this was open. The page now shows their change: check it, then send it again.",
      ),
    ).toBeInTheDocument();
    const refusal = "Kinase Atlas doesn’t allow sharing its entities, so it can’t send requests.";
    answer(`POST /api/v1/entities/${ASK_ID}/submit`, problem(409, "refused", refusal));
    await user.click(within(dialog).getByRole("button", { name: "Send to Protein Core" }));
    expect(await within(dialog).findByText(refusal)).toBeInTheDocument();
  });

  it("says why it can't be sent, when no service takes it", async () => {
    onRequest(request());
    answer(`GET /api/v1/projects/${KINASE.id}/services`, []);
    await renderApp("/entities/ARQ-000001/");
    const panel = await screen.findByRole("region", { name: "Request" });
    const why =
      "No service takes assay requests from Kinase Atlas yet. A service’s Admins choose what it takes, in its settings.";
    await within(panel).findByText(why);
    const send = within(panel).getByRole("button", { name: "Send to a service…" });
    expect(send).toHaveAttribute("aria-disabled", "true");
    expect(send).toHaveAccessibleDescription(why);
  });

  it("says why it can't be sent, when its project keeps its records to itself", async () => {
    onRequest(request(), project({ allow_sharing: false }));
    await renderApp("/entities/ARQ-000001/");
    const panel = await screen.findByRole("region", { name: "Request" });
    expect(
      await within(panel).findByText(
        "Kinase Atlas doesn’t allow sharing its entities outside it, so it can’t send requests. Its Admins can allow it in its settings.",
      ),
    ).toBeInTheDocument();
  });

  it("shows the requesting project which revision QA signed", async () => {
    const seen = request({}, SENT);
    onRequest(seen);
    const result = entity({ id: RESULT_ID, hrid: "ARS-000001", type: RESULT.name });
    answer(`GET /api/v1/entities/${ASK_ID}/request`, {
      request: seen,
      sent: [],
      results: [result],
      result_releases: [
        { entity: RESULT_ID, reviewed_revision: 4, signed_revision: 5, released_revision: 6 },
      ],
    });
    await renderApp("/entities/ARQ-000001/");
    const results = await screen.findByRole("list", { name: "Results" });
    const receipt = within(results).getByRole("link", {
      name: "Reviewed revision 4 · Signed revision 5 · Released at revision 6",
    });
    expect(receipt).toHaveAttribute("href", "/entities/ARS-000001/audit/");
  });

  it("shows the service its results, and what it was sent", async () => {
    const seen = request({}, { ...SENT, yours: ["service"] });
    onRequest(seen, CORE);
    const result = entity({
      id: RESULT_ID,
      hrid: "ARS-000001",
      type: RESULT.name,
      title: "ARS-000001",
      owner_project: ref(CORE),
      values: { request: ASK_ID, purity: 97.5, review: "draft" },
    });
    answer(`GET /api/v1/entities/${ASK_ID}/request`, {
      request: seen,
      asking_project: ref(KINASE),
      service: ref(CORE),
      submitted_at: SENT.submitted_at,
      submitted_by: SENT.submitted_by,
      sent: [lot()],
      results: [result],
    });
    await renderApp("/entities/ARQ-000001/");
    const panel = await screen.findByRole("region", { name: "Request" });
    expect(
      within(panel).getByText(
        "Protein Core fills in Status, Priority, Technician and Core notes. Kinase Atlas’s Editors change the rest.",
      ),
    ).toBeInTheDocument();
    // The service can't open the asking project: its name isn't a link.
    expect(within(panel).queryByRole("link", { name: "Kinase Atlas" })).toBeNull();
    expect(within(panel).getByRole("link", { name: "Protein Core" })).toBeInTheDocument();
    const results = await within(panel).findByRole("list", { name: "Results" });
    expect(within(results).getByRole("link", { name: "ARS-000001" })).toBeInTheDocument();
  });
});

describe("a request's forms", () => {
  it("offer the service only its own fields", async () => {
    onRequest(request({}, { ...SENT, yours: ["service"] }), CORE);
    await renderApp("/entities/ARQ-000001/edit/");
    expect(screen.getByRole("combobox", { name: /^Priority/ })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: /^Core notes/ })).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: /^Title/ })).toBeNull();
    expect(screen.queryByRole("textbox", { name: /^Instructions/ })).toBeNull();
    expect(
      screen.getByText(
        "Title, Samples and Instructions are Kinase Atlas’s: its Editors change them.",
      ),
    ).toBeInTheDocument();
    // Its people are named from the service: the asking project isn't the service's to read.
    await waitFor(() => {
      expect(calls("GET", `/api/v1/projects/${CORE.id}/people`)).not.toEqual([]);
    });
    expect(calls("GET", `/api/v1/projects/${KINASE.id}/people`)).toEqual([]);
  });

  it("offer the asking project only its own, sent or not", async () => {
    onRequest(request({}, SENT));
    const { user } = await renderApp("/entities/ARQ-000001/edit/");
    expect(screen.queryByRole("combobox", { name: /^Priority/ })).toBeNull();
    expect(
      screen.getByText("Priority, Technician and Core notes are Protein Core’s: it fills them in."),
    ).toBeInTheDocument();
    answer(`PATCH /api/v1/entities/${ASK_ID}`, request({ revision: 2 }, SENT));
    await user.type(screen.getByRole("textbox", { name: /^Instructions/ }), " and spin");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await screen.findByText("Saved revision 2 of ARQ-000001.");
    expect(calls("PATCH").at(-1)?.body).toEqual({
      values: { instructions: "Desalt first and spin" },
      reason: "",
    });
  });

  it("leave a new request's service fields to the service", async () => {
    onRequest(request());
    answer(`GET /api/v1/projects/${KINASE.id}/members`, page([]));
    const { user } = await renderApp(
      "/projects/kinase-atlas/entities/new/?type=acme/protein.AssayRequest",
    );
    expect(screen.queryByRole("combobox", { name: /^Priority/ })).toBeNull();
    expect(screen.queryByRole("textbox", { name: /^Core notes/ })).toBeNull();
    expect(
      screen.getByText(
        "Priority, Technician and Core notes are for the service it’s sent to: it fills them in once it’s sent. Once it’s made, send it to a service from its page.",
      ),
    ).toBeInTheDocument();
    // Its service's state isn't said to start as Submitted: it isn't sent yet.
    expect(
      screen.getByText("Status is the service’s: it’s set once the request is sent."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Submitted/)).toBeNull();
    answer("POST /api/v1/entities", respond(201, request()));
    await user.type(screen.getByRole("textbox", { name: /^Title/ }), "Purity of the kinases");
    await user.click(screen.getByRole("button", { name: "Create assay request" }));
    await waitFor(() => {
      expect(calls("POST", "/api/v1/entities")).toHaveLength(1);
    });
    expect(nth(calls("POST", "/api/v1/entities"), 0).body).toMatchObject({
      values: { title: "Purity of the kinases" },
    });
  });
});

describe("a result's page", () => {
  function onResult(review: string): void {
    withTypes();
    answer("GET /api/v1/entity-types", [SAMPLE, ASK, RESULT]);
    seeing(CORE);
    answer(
      "GET /api/v1/entities/ARS-000001",
      entity({
        id: RESULT_ID,
        hrid: "ARS-000001",
        type: RESULT.name,
        title: "ARS-000001",
        owner_project: ref(CORE),
        values: { request: ASK_ID, purity: 97.5, review },
        refs: {
          [ASK_ID]: {
            hrid: "ARQ-000001",
            title: "Purity of the kinases",
            type: ASK.name,
            archived: false,
            purged: false,
          },
        },
      }),
    );
    answer("GET /api/v1/entities/ARS-000001/provenance", {});
    answer("GET /api/v1/entities/ARS-000001/projects", []);
  }

  it("says it's the service's until it's released, and links its request", async () => {
    onResult("draft");
    await renderApp("/entities/ARS-000001/");
    const note = await screen.findByText(/^A result of/);
    expect(note).toHaveTextContent(
      "A result of Purity of the kinases ARQ-000001. It’s Protein Core’s until its Review is Released: then the project that asked for it reads it too.",
    );
    expect(within(note).getByRole("link", { name: "Purity of the kinases" })).toHaveAttribute(
      "href",
      "/entities/ARQ-000001/",
    );
  });

  it("says once it's released", async () => {
    onResult("released");
    await renderApp("/entities/ARS-000001/");
    expect(await screen.findByText(/^A result of/)).toHaveTextContent(
      "It’s released to the project that asked for it, where it stays.",
    );
  });
});

describe("what a service takes", () => {
  const INTAKE = `/api/v1/projects/${CORE.id}/intake`;

  function onCore(found: Schemas["ProjectOut"] = CORE): void {
    withTypes();
    answer("GET /api/v1/entity-types", [SAMPLE, ASK, RESULT]);
    seeing(found, KINASE, ELSEWHERE);
    answer(`GET /api/v1/projects/${CORE.id}/dashboard`, { app: null, set_by: null, set_at: null });
    answer(`GET /api/v1/projects/${CORE.id}/dashboard/choices`, []);
    answer(`GET ${INTAKE}`, { types: [], from_registry: false, projects: [] });
  }

  it("its Admins choose, from projects in its registry", async () => {
    onCore();
    const { user } = await renderApp("/projects/protein-core/settings/");
    const card = (await screen.findByRole("heading", { name: "Requests it takes" })).closest(
      "section",
    );
    if (!card) throw new Error("no card");
    const kinds = await within(card).findByRole("group", { name: "Request types" });
    // Only request types: never samples or results.
    expect(
      within(kinds)
        .getAllByRole("checkbox")
        .map((c) => c.getAttribute("value")),
    ).toEqual([ASK.name]);
    await user.click(within(kinds).getByRole("checkbox", { name: "Assay request" }));
    await user.click(within(card).getByRole("radio", { name: "Only the projects chosen here" }));
    const projects = await within(card).findByRole("group", { name: "Projects" });
    // Not itself, nor a project in another registry.
    expect(
      within(projects)
        .getAllByRole("checkbox")
        .map((c) => c.getAttribute("value")),
    ).toEqual([KINASE.id]);
    await user.click(within(projects).getByRole("checkbox", { name: "Kinase Atlas" }));
    answer(
      `PUT ${INTAKE}`,
      problem(422, "validation_failed", "Those aren’t projects you can see.", [
        { pointer: "#/projects/0", detail: "is no project you can see." },
      ]),
    );
    await user.click(within(card).getByRole("button", { name: "Save" }));
    expect(await within(card).findByText("is no project you can see.")).toBeInTheDocument();
    expect(calls("PUT", INTAKE).at(-1)?.body).toEqual({
      types: [ASK.name],
      from_registry: false,
      projects: [KINASE.id],
    });

    const saved = {
      types: [{ name: ASK.name, label: "Assay request" }],
      from_registry: false,
      projects: [ref(KINASE)],
    };
    answer(`PUT ${INTAKE}`, saved);
    answer(`GET ${INTAKE}`, saved);
    await user.click(within(card).getByRole("button", { name: "Save" }));
    expect(
      await screen.findByText("Protein Core takes assay requests from Kinase Atlas now."),
    ).toBeInTheDocument();
  });

  it("its other members read", async () => {
    onCore({ ...CORE, my_role: "editor", can: { ...CORE.can, manage: false } });
    answer(`GET ${INTAKE}`, {
      types: [{ name: ASK.name, label: "Assay request" }],
      from_registry: true,
      projects: [],
    });
    await renderApp("/projects/protein-core/settings/");
    expect(
      await screen.findByText(
        `Protein Core takes assay requests from every project in the ${MAIN_REGISTRY.name} registry.`,
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("The project’s Admins choose what it takes.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
  });
});

describe("a project's Requests tab", () => {
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

  const TAKES = {
    types: [{ name: ASK.name, label: "Assay request" }],
    from_registry: true,
    projects: [],
  };
  const NOTHING = { types: [], from_registry: false, projects: [] };

  /** A project's two sides: what it takes, whom it sent to and who sent to it. */
  function on(
    found: Schemas["ProjectOut"],
    {
      intake = NOTHING,
      to = [],
      from = [],
    }: {
      intake?: Schemas["IntakeOut"];
      to?: Schemas["ProjectOut"][];
      from?: Schemas["ProjectOut"][];
    } = {},
  ): void {
    withTypes();
    answer("GET /api/v1/entity-types", [SAMPLE, ASK, RESULT]);
    seeing(found, KINASE, CORE);
    answer(`GET /api/v1/projects/${found.id}/intake`, intake);
    answer(`GET /api/v1/projects/${found.id}/sent-to`, to.map(ref));
    answer(`GET /api/v1/projects/${found.id}/sent-from`, from.map(ref));
    answer(`GET /api/v1/projects/${found.id}/dashboard`, { app: null, set_by: null, set_at: null });
  }

  /** The searches: whether any request was sent to it, whether it owns any, and the grid's. */
  function searching({
    queued = false,
    owns = false,
    rows = [],
  }: {
    queued?: boolean;
    owns?: boolean;
    rows?: Schemas["EntityOut"][];
  }): void {
    answer("POST /api/v1/entities/search", ({ body }: { body: Record<string, unknown> }) => {
      if (body["limit"] === 1) return page((body["type"] ? owns : queued) ? [request()] : []);
      return page(rows);
    });
  }

  /** The grid's last search, and its column headings. */
  function lastSearch(): unknown {
    const sent = calls("POST", "/api/v1/entities/search").filter(
      (s) => (s.body as { limit: number }).limit !== 1,
    );
    return nth(sent, sent.length - 1).body;
  }

  function headings(grid: HTMLElement): (string | null)[] {
    const rows = within(grid).getAllByRole("row");
    return within(nth(rows, 0))
      .getAllByRole("columnheader")
      .map((c) => c.textContent);
  }

  it("is a service's queue, filtered by status, assignee and asking project", async () => {
    on(CORE, { intake: TAKES, from: [KINASE] });
    searching({ queued: true, rows: [request({}, { ...SENT, yours: ["service"] })] });
    const { router, user } = await renderApp("/projects/protein-core/requests/");
    // Its own tab: the current one, as Entities isn't.
    const tabs = screen.getByRole("navigation", { name: "Project" });
    expect(await within(tabs).findByRole("link", { name: "Requests" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(within(tabs).getByRole("link", { name: "Entities" })).not.toHaveAttribute(
      "aria-current",
    );
    expect(screen.queryByRole("navigation", { name: "Which requests" })).toBeNull();
    expect(
      screen.getByText(/^Protein Core takes assay requests from every project in the Main/),
    ).toBeInTheDocument();
    const grid = await screen.findByRole("table", { name: "1 assay request" });
    expect(headings(grid).slice(0, 4)).toEqual(["", "ID", "Title", "Asked by"]);
    expect(within(grid).getAllByRole("row")[1]).toHaveTextContent("Kinase Atlas");
    const queue = [{ field: "sent_to", op: "eq", value: CORE.id }];
    expect(lastSearch()).toMatchObject({
      project: CORE.id,
      type: ASK.name,
      where: { all: queue },
    });

    expect(await filterFields(user)).toEqual(
      expect.arrayContaining(["Asked by", "Status", "Assigned to", "Technician"]),
    );
    const editor = await addFilter(user, "Asked by");
    await user.selectOptions(
      within(editor).getByRole("combobox", { name: "Filter 1: value" }),
      "Kinase Atlas",
    );
    const asked = [{ field: "owner_project", op: "eq", value: KINASE.id }];
    await waitFor(() => {
      expect(router.state.location.search).toEqual({ filters: asked });
    });
    // The queue's own condition stays, and isn't one to remove.
    await waitFor(() => {
      expect(lastSearch()).toMatchObject({ where: { all: [...queue, ...asked] } });
    });
    await user.click(within(editor).getByRole("button", { name: "Done" }));
    const chips = screen.getByRole("group", { name: "Filters" });
    expect(within(chips).getAllByRole("button", { name: /^Filter \d/ })).toHaveLength(1);
  });

  it("is the requests an asking project sent, filtered by status and service", async () => {
    on(KINASE, { to: [CORE] });
    searching({ owns: true, rows: [request({}, SENT)] });
    const { user } = await renderApp("/projects/kinase-atlas/requests/");
    const grid = await screen.findByRole("table", { name: "1 assay request" });
    expect(headings(grid).slice(0, 4)).toEqual(["", "ID", "Title", "Sent to"]);
    expect(lastSearch()).toMatchObject({
      project: KINASE.id,
      where: {
        all: [
          { field: "owner_project", op: "eq", value: KINASE.id },
          { field: "sent_to", op: "is_set", value: true },
        ],
      },
    });
    expect(screen.getByRole("link", { name: "New assay request" })).toBeInTheDocument();

    const editor = await addFilter(user, "Sent to");
    // Each is sent: it's to one service or another.
    const condition = within(editor).getByRole("combobox", { name: "Filter 1: condition" });
    expect(within(condition).queryByRole("option", { name: /not sent/ })).toBeNull();
    const value = within(editor).getByRole("combobox", { name: "Filter 1: value" });
    expect(within(value).getByRole("option", { name: "Protein Core" })).toBeInTheDocument();
  });

  it("switches between both sides, for a project that's both", async () => {
    on(KINASE, { intake: TAKES, to: [CORE], from: [CORE] });
    searching({ queued: true, owns: true });
    const { router, user } = await renderApp("/projects/kinase-atlas/requests/");
    const sides = await screen.findByRole("navigation", { name: "Which requests" });
    expect(within(sides).getByRole("link", { name: "Sent to us" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(await screen.findByText("No requests were sent to Kinase Atlas yet.")).toBeVisible();
    await user.click(within(sides).getByRole("link", { name: "We sent" }));
    await waitFor(() => {
      expect(router.state.location.search).toEqual({ side: "sent" });
    });
    expect(within(sides).getByRole("link", { name: "We sent" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(within(sides).getByRole("link", { name: "Sent to us" })).not.toHaveAttribute(
      "aria-current",
    );
    expect(await screen.findByText("Kinase Atlas hasn’t sent a request yet.")).toBeVisible();
  });

  it("isn't there for a project that neither takes nor sends requests", async () => {
    on(CORE);
    searching({});
    await renderApp("/projects/protein-core/");
    await waitFor(() => {
      expect(calls("GET", `/api/v1/projects/${CORE.id}/sent-to`)).toHaveLength(1);
    });
    await waitFor(() => {
      expect(calls("POST", "/api/v1/entities/search").length).toBeGreaterThan(1);
    });
    const tabs = screen.getByRole("navigation", { name: "Project" });
    expect(within(tabs).queryByRole("link", { name: "Requests" })).toBeNull();
    expect(screen.queryByRole("region", { name: "Requests" })).toBeNull();
  });

  it("opens from the service's page, which says what it takes", async () => {
    on(CORE, { intake: TAKES });
    searching({});
    await renderApp("/projects/protein-core/");
    const card = await screen.findByRole("region", { name: "Requests" });
    expect(
      within(card).getByRole("link", { name: "Requests sent to Protein Core" }),
    ).toHaveAttribute("href", "/projects/protein-core/requests/?side=received");
  });
});
