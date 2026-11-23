/**
 * A record's electronic signatures (ADR 0126) on its page, against a fake API: the
 * signatures a state needs, signing (which leaves for the identity provider), what came of
 * it once back, and every signature as people read it, checked again on request.
 */

import type { Schemas } from "@ikisci/sdk";
import { act, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { entity, field, PROTEIN, renderApp, withTypes } from "./entity-fixtures";
import { leave } from "./pages/entity-signatures";
import { answer, calls, ME, problem } from "./test-server";

const LOT_ID = "00000000-0000-7000-8000-00000000e701";
const SIGNATURE_ID = "00000000-0000-7000-8000-00000000e7a1";
const SIGNING_ID = "00000000-0000-7000-8000-00000000e7b1";

const IN_REVIEW: Schemas["StateOut"] = {
  name: "in_review",
  label: "In review",
  description: null,
  set_by: ["acme/lims.analyst"],
  locks: false,
  holds_amount: false,
  reason: null,
  signatures: [],
};

const STATUS = field({
  name: "status",
  label: "Status",
  type: "state",
  plugin: "acme/lims",
  initial: "in_review",
  states: [
    IN_REVIEW,
    {
      ...IN_REVIEW,
      name: "approved",
      label: "Approved",
      set_by: ["acme/lims.reviewer"],
      locks: true,
      reason: "required",
      signatures: [
        {
          meaning: "review",
          meaning_label: "Review",
          verb: "Reviewed",
          label: "Review",
          statement: "I reviewed this record and found it in order.",
          roles: ["acme/lims.reviewer"],
        },
        {
          meaning: "approval",
          meaning_label: "Approval",
          verb: "Approved",
          label: "QA approval",
          statement: "I approve this lot.",
          roles: ["acme/lims.qa"],
        },
      ],
    },
  ],
});

const LOT: Schemas["EntityTypeOut"] = {
  ...PROTEIN,
  name: "acme/lims.Lot",
  plugin: "acme/lims",
  label: "Lot",
  label_plural: "Lots",
  description: null,
  hrid_prefix: "LOT",
  identity: [],
  fields: [...PROTEIN.fields.slice(0, 1), STATUS],
};

const APPROVAL: Schemas["SignatureCan"] = {
  index: 1,
  meaning: "approval",
  meaning_label: "Approval",
  label: "QA approval",
  statement: "I approve this lot.",
  roles: ["acme/lims.qa"],
  role_labels: ["QA"],
  given: null,
  allowed: true,
  why_not: null,
  lapsed: null,
};

const REVIEW: Schemas["SignatureCan"] = {
  ...APPROVAL,
  index: 0,
  meaning: "review",
  meaning_label: "Review",
  label: "Review",
  statement: "I reviewed this record and found it in order.",
  roles: ["acme/lims.reviewer"],
  role_labels: ["Reviewer"],
  allowed: false,
  why_not: "It needs someone with the Reviewer role in Kinase Atlas.",
};

const GIVEN: Schemas["SignatureOut"] = {
  id: SIGNATURE_ID,
  manifestation:
    "Reviewed by Ada Lovelace on 2026-11-18 at 14:03:22 UTC. Meaning: Review. Reason: Data checked.",
  record: LOT_ID,
  hrid: "LOT-000001",
  revision: 2,
  entry_revision: null,
  change: {
    field: "status",
    field_label: "Status",
    from: "in_review",
    from_label: "In review",
    to: "approved",
    to_label: "Approved",
  },
  index: 0,
  of: 2,
  meaning: "review",
  meaning_label: "Review",
  label: "Review",
  statement: "I reviewed this record and found it in order.",
  roles: ["acme/lims.reviewer"],
  signer: { id: ME.id, name: ME.name },
  signer_name: ME.name,
  signer_email: ME.email,
  signed_at: "2026-11-18T14:03:22Z",
  reason: "Data checked",
  authentication: {
    protocol: "oidc",
    issuer: "https://idp.example.com",
    subject: "uid-1",
    authenticated_at: "2026-11-18T14:03:20Z",
    methods: ["pwd", "otp"],
    context: "",
  },
  deployment: "iki.example.com",
  key: "ed25519:abc",
  status: "pending",
  status_detail: "Setting it to Approved waits for 1 more signature.",
  carried_by: null,
  document: "{}",
  signature: "x",
};

function lot(overrides: Partial<Schemas["EntityOut"]> = {}): Schemas["EntityOut"] {
  return entity({
    id: LOT_ID,
    hrid: "LOT-000001",
    type: "acme/lims.Lot",
    title: "LOT-000001",
    revision: 2,
    values: { name: "Lot A", status: "in_review" },
    locked: false,
    locked_by: [],
    can: {
      edit: true,
      correct: false,
      edit_amount: false,
      states: {
        status: [
          {
            name: "approved",
            label: "Approved",
            locks: true,
            reason: "required",
            allowed: false,
            why_not: "It’s set by signing for it: see its signatures.",
            signed: true,
          },
        ],
      },
      sign: [
        {
          field: "status",
          field_label: "Status",
          to: "approved",
          to_label: "Approved",
          reason: "required",
          allowed: true,
          why_not: null,
          signatures: [REVIEW, APPROVAL],
        },
      ],
    },
    ...overrides,
  });
}

function onLot(signatures: Schemas["SignatureOut"][] = []): void {
  withTypes();
  answer("GET /api/v1/entity-types", [PROTEIN, LOT]);
  answer("GET /api/v1/entities/LOT-000001", lot());
  answer("GET /api/v1/entities/LOT-000001/provenance", {});
  answer("GET /api/v1/entities/LOT-000001/projects", []);
  answer("GET /api/v1/entities/LOT-000001/signatures", { items: signatures, missing: [] });
  answer("GET /api/v1/me/signing", {
    can_sign: true,
    why_not: null,
    acknowledged: false,
    acknowledgment:
      "My electronic signature is the legally binding equivalent of my handwritten signature.",
    provider: "Acme SSO",
    locked: false,
  });
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("a record's signatures", () => {
  it.each([
    [3, true],
    [4, false],
  ] as const)(
    "marks only the revision that carried the signature as signed (revision %d)",
    async (revision, signed) => {
      onLot([{ ...GIVEN, status: "carried", carried_by: 3 }]);
      answer("GET /api/v1/entities/LOT-000001", lot({ revision }));
      await renderApp("/entities/LOT-000001/");
      // Wait for the shared signatures query. The record still lists its past signatures.
      expect(await screen.findByText("Carried")).toBeInTheDocument();
      expect(Boolean(screen.queryByText("Signed", { selector: ".badge" }))).toBe(signed);
    },
  );

  it("lists the signatures a state needs, each one this person may give or why not", async () => {
    onLot();
    await renderApp("/entities/LOT-000001/");
    const section = await screen.findByRole("region", { name: "Signatures" });
    const wanted = within(section).getByRole("list", { name: "Signatures for Approved" });
    const review = within(wanted).getByRole("button", { name: "Sign: Review" });
    expect(review).toHaveAttribute("aria-disabled", "true");
    expect(review).toHaveAccessibleDescription(
      "It needs someone with the Reviewer role in Kinase Atlas.",
    );
    expect(within(wanted).getByRole("button", { name: "Sign: QA approval" })).not.toHaveAttribute(
      "aria-disabled",
    );
  });

  it("signs with a reason and an acknowledgment, then leaves to sign in again", async () => {
    onLot();
    const left = vi.spyOn(leave, "to").mockImplementation(() => undefined);
    const { user } = await renderApp("/entities/LOT-000001/");
    const section = await screen.findByRole("region", { name: "Signatures" });
    await user.click(within(section).getByRole("button", { name: "Sign: QA approval" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Sign LOT-000001: QA approval" });
    expect(dialog).toHaveTextContent("Approval. I approve this lot.");
    expect(dialog).toHaveTextContent("Next, Acme SSO asks you to sign in again");
    const go = within(dialog).getByRole("button", { name: "Sign in again to sign" });
    // The reason it requires, then the agreement before a first signature: said before asking Iki.
    await user.click(go);
    expect(
      await within(dialog).findByText("Say why: it’s signed with your signature."),
    ).toBeInTheDocument();
    await user.type(within(dialog).getByRole("textbox", { name: "Reason" }), "Specs met");
    await user.click(go);
    expect(
      await within(dialog).findByText("Agree to this before your first signature."),
    ).toBeInTheDocument();
    expect(calls("POST", `/api/v1/entities/${LOT_ID}/signatures`)).toHaveLength(0);
    await user.click(within(dialog).getByRole("checkbox", { name: "I agree" }));
    answer(`POST /api/v1/entities/${LOT_ID}/signatures`, {
      id: SIGNING_ID,
      status: "started",
      message: "",
      record: LOT_ID,
      hrid: "LOT-000001",
      field: "status",
      field_label: "Status",
      to: "approved",
      to_label: "Approved",
      index: 1,
      sign_in_url: `/accounts/signing/${SIGNING_ID}/`,
      expires_at: "2026-11-18T14:13:22Z",
      signature: null,
      set: false,
      state: "in_review",
    });
    await user.click(go);
    await waitFor(() => {
      expect(left).toHaveBeenCalledWith(`/accounts/signing/${SIGNING_ID}/`);
    });
    const sent = calls("POST", `/api/v1/entities/${LOT_ID}/signatures`).at(-1);
    expect(sent?.body).toEqual({
      field: "status",
      state: "approved",
      index: 1,
      reason: "Specs met",
      acknowledge: true,
      return_to: "/entities/LOT-000001/",
    });
    expect(sent?.headers.get("If-Match")).toBe('"2"');
  });

  it("waits for whether it's a first signature before it can be signed", async () => {
    onLot();
    // What Iki knows of the signer comes later than the dialog opens.
    let release = (): void => undefined;
    answer(
      "GET /api/v1/me/signing",
      () =>
        new Promise((resolve) => {
          release = () => {
            resolve({
              can_sign: true,
              why_not: null,
              acknowledged: false,
              acknowledgment: "My electronic signature is legally binding.",
              provider: "Acme SSO",
              locked: false,
            });
          };
        }),
    );
    const { user } = await renderApp("/entities/LOT-000001/");
    const section = await screen.findByRole("region", { name: "Signatures" });
    await user.click(within(section).getByRole("button", { name: "Sign: QA approval" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Sign LOT-000001: QA approval" });
    const go = within(dialog).getByRole("button", { name: "Sign in again to sign" });
    expect(go).toHaveAttribute("aria-disabled", "true");
    await user.click(go);
    expect(calls("POST", `/api/v1/entities/${LOT_ID}/signatures`)).toHaveLength(0);
    await act(async () => {
      release();
      await Promise.resolve();
    });
    // Its agreement shows before it can be pressed, not after.
    expect(await within(dialog).findByRole("checkbox", { name: "I agree" })).not.toBeChecked();
    expect(go).not.toHaveAttribute("aria-disabled");
  });

  it("says when the record changed while signing was open", async () => {
    onLot();
    const left = vi.spyOn(leave, "to").mockImplementation(() => undefined);
    answer("GET /api/v1/me/signing", {
      can_sign: true,
      why_not: null,
      acknowledged: true,
      acknowledgment: "",
      provider: "Acme SSO",
      locked: false,
    });
    answer(
      `POST /api/v1/entities/${LOT_ID}/signatures`,
      problem(412, "stale", "It changed: revision 3 is the latest."),
    );
    const { user } = await renderApp("/entities/LOT-000001/");
    const section = await screen.findByRole("region", { name: "Signatures" });
    await user.click(within(section).getByRole("button", { name: "Sign: QA approval" }));
    const dialog = await screen.findByRole("alertdialog");
    await user.type(within(dialog).getByRole("textbox", { name: "Reason" }), "Specs met");
    await user.click(within(dialog).getByRole("button", { name: "Sign in again to sign" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "It changed while this was open",
    );
    expect(left).not.toHaveBeenCalled();
  });

  it("shows what came of signing once back from the identity provider", async () => {
    onLot();
    answer(`GET /api/v1/signings/${SIGNING_ID}`, {
      id: SIGNING_ID,
      status: "failed",
      message: "The identity provider didn’t ask you to sign in again just now.",
      record: LOT_ID,
      hrid: "LOT-000001",
      field: "status",
      field_label: "Status",
      to: "approved",
      to_label: "Approved",
      index: 1,
      sign_in_url: "",
      expires_at: "2026-11-18T14:13:22Z",
      signature: null,
      set: false,
      state: "in_review",
    });
    const { user, router } = await renderApp(`/entities/LOT-000001/?signing=${SIGNING_ID}`);
    const section = await screen.findByRole("region", { name: "Signatures" });
    const alert = await within(section).findByRole("alert");
    expect(alert).toHaveTextContent(
      "Your signature wasn’t given. The identity provider didn’t ask you to sign in again just now.",
    );
    await user.click(within(alert).getByRole("button", { name: "Dismiss" }));
    await waitFor(() => {
      expect(router.state.location.search).toEqual({});
    });
  });

  it("says which state the last signature set, and the state is set only by signing", async () => {
    onLot();
    answer(`GET /api/v1/signings/${SIGNING_ID}`, {
      id: SIGNING_ID,
      status: "signed",
      message: "",
      record: LOT_ID,
      hrid: "LOT-000001",
      field: "status",
      field_label: "Status",
      to: "approved",
      to_label: "Approved",
      index: 1,
      sign_in_url: "",
      expires_at: "2026-11-18T14:13:22Z",
      signature: SIGNATURE_ID,
      set: true,
      state: "approved",
    });
    await renderApp(`/entities/LOT-000001/?signing=${SIGNING_ID}`);
    const section = await screen.findByRole("region", { name: "Signatures" });
    expect(await within(section).findByRole("status")).toHaveTextContent(
      "You signed LOT-000001. Its Status is Approved now.",
    );
    // Its state field offers the signed state only through its signatures.
    const states = screen.getByRole("region", { name: "States" });
    const set = within(states).getByRole("button", { name: "Set Status" });
    expect(set).toHaveAttribute("aria-disabled", "true");
    expect(set).toHaveAccessibleDescription(/It’s set by signing for it: see its signatures\./);
  });

  it("shows each signature as people read it, checks them again, and withdraws one", async () => {
    onLot([GIVEN]);
    const { user } = await renderApp("/entities/LOT-000001/");
    const section = await screen.findByRole("region", { name: "Signatures" });
    const given = await within(section).findByRole("list", { name: "Signatures given" });
    expect(given).toHaveTextContent(GIVEN.manifestation);
    expect(given).toHaveTextContent("Waiting for others");
    answer("GET /api/v1/entities/LOT-000001/signatures", (call: { query: URLSearchParams }) => ({
      items: [
        {
          ...GIVEN,
          verification:
            call.query.get("verify") === "true"
              ? {
                  valid: false,
                  verified_at: "2026-11-18T15:00:00Z",
                  checks: [
                    {
                      name: "record",
                      ok: false,
                      detail: "The record's history isn't what was signed.",
                    },
                  ],
                }
              : null,
        },
      ],
      missing: [],
    }));
    await user.click(within(section).getByRole("button", { name: "Check signatures" }));
    expect(await within(given).findByRole("alert")).toHaveTextContent(
      "This signature doesn’t verify:The record's history isn't what was signed.",
    );
    answer(`POST /api/v1/signatures/${SIGNATURE_ID}/withdrawal`, { ...GIVEN, status: "withdrawn" });
    await user.click(
      within(given).getByRole("button", { name: "Withdraw your signature: Review" }),
    );
    const dialog = await screen.findByRole("alertdialog", {
      name: "Withdraw your signature on LOT-000001?",
    });
    await user.type(within(dialog).getByRole("textbox", { name: /^Reason/ }), "Wrong lot");
    await user.click(within(dialog).getByRole("button", { name: "Withdraw" }));
    expect(await screen.findByText("You withdrew your signature on LOT-000001.")).toBeVisible();
    expect(calls("POST", `/api/v1/signatures/${SIGNATURE_ID}/withdrawal`).at(-1)?.body).toEqual({
      reason: "Wrong lot",
    });
  });
});
