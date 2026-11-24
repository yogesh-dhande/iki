import type { Schemas } from "@ikisci/sdk";
import { act, screen, waitFor, within } from "@testing-library/react";
import type { UserEvent } from "@testing-library/user-event";
import { expect, it } from "vitest";

import { PROTEIN, renderApp, withTypes } from "./entity-fixtures";
import type { ObservationEvent, Receipt, ReceiptLine } from "./storage";
import { answer, calls, ME, page, problem } from "./test-server";

const id = "00000000-0000-7000-8000-0000000000a1";
const api = `/api/v1/receipts/${id}`;
const ask = "REQ-000001";

/** Someone else at the bench. */
const ADA = {
  ...ME,
  id: "00000000-0000-7000-8000-000000000002",
  name: "Ada Lovelace",
  email: "ada@example.com",
};

function tube(number: number, changes: Partial<ReceiptLine> = {}): ReceiptLine {
  const serial = String(number).padStart(6, "0");
  return {
    id: `line-${serial}`,
    line_revision: 1,
    entity: `tube-${serial}`,
    ref: {
      hrid: `TUB-${serial}`,
      type: "acme/lims.Tube",
      title: `TUB-${serial}`,
      archived: false,
      purged: false,
    },
    code: `RC${serial}`,
    expected: true,
    outcome: null,
    note: "",
    checked_by: null,
    checked_at: null,
    put_away: null,
    received_unplaced: false,
    ...changes,
  };
}

function receipt(revision: number, lines: ReceiptLine[], changes: Partial<Receipt> = {}): Receipt {
  return {
    id,
    plan_revision: revision,
    request: {
      id: "req",
      hrid: ask,
      type: "acme/lims.Request",
      title: "Intact mass",
      position: null,
      archived: false,
      purged: false,
    },
    project: "project",
    status: "open",
    handed_over_by: "",
    note: "",
    started_by: ME,
    started_at: "2026-01-12T09:00:00Z",
    received_by: null,
    cancelled_by: null,
    cancel_reason: "",
    completed_at: null,
    lines,
    removed_lines: [],
    outcomes: {},
    can_receive: true,
    ...changes,
  };
}

function event(changes: Partial<ObservationEvent>): ObservationEvent {
  return {
    id: "00000000-0000-7000-8000-0000000000e1",
    operation: "00000000-0000-7000-8000-0000000000f1",
    kind: "outcome",
    receipt_id: id,
    reconciliation_id: null,
    line_id: null,
    before: null,
    after: null,
    actor: ADA,
    created_at: "2026-01-12T09:30:00Z",
    revision: 2,
    previous: null,
    reason: "",
    ...changes,
  };
}

async function openReceipt(open: Receipt, types?: Schemas["EntityTypeOut"][]) {
  withTypes();
  if (types) answer("GET /api/v1/entity-types", types);
  answer(`GET /api/v1/entities/${ask}/receipts`, {
    receipts: [open],
    expected: open.lines.length,
    can_receive: true,
    why_not: null,
  });
  // Read again just before it's completed: unchanged unless a test says otherwise.
  answer(`GET ${api}`, open);
  const rendered = await renderApp(`/storage/receive/${ask}/`);
  await screen.findByRole("list", { name: "Checklist" });
  return rendered;
}

function line(hrid: string) {
  const item = screen
    .getAllByRole("listitem")
    .find((li) => li.classList.contains("receive-line") && li.textContent.includes(hrid));
  if (!item) throw new Error(`Missing line for ${hrid}.`);
  return within(item);
}

function scanBox() {
  return screen.getByRole("textbox", { name: "Scan each tube" });
}

async function why(user: UserEvent, reason: string, confirm: string) {
  const dialog = within(await screen.findByRole("dialog"));
  await user.type(dialog.getByRole("textbox", { name: /^Why are you/ }), reason);
  await user.click(dialog.getByRole("button", { name: confirm }));
  return dialog;
}

it("records a scan in one step, with no reason and no version", async () => {
  const first = tube(1);
  const { user } = await openReceipt(receipt(1, [first]));
  answer(`POST ${api}/scan`, {
    found: "matched",
    line: first.id,
    receipt: receipt(2, [{ ...first, outcome: "matched", checked_by: ME }]),
  });
  await user.type(scanBox(), "RC000001{Enter}");
  expect(await screen.findByText("TUB-000001: matched.")).toBeVisible();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(calls("POST", `${api}/scan`).map((c) => c.body)).toEqual([{ code: "RC000001" }]);
  expect(line("TUB-000001").getByRole("button", { name: "Matched" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
});

it("keeps both people’s scans when their answers arrive out of order", async () => {
  const [first, second] = [tube(1), tube(2)];
  const { user } = await openReceipt(receipt(1, [first, second]));
  let release = (): void => undefined;
  const held = new Promise<void>((done) => {
    release = done;
  });
  const both = receipt(3, [
    { ...first, outcome: "matched", checked_by: ME },
    { ...second, outcome: "matched", checked_by: ADA },
  ]);
  answer(`POST ${api}/scan`, async ({ body }: { body: { code: string } }) => {
    if (body.code === "RC000001") {
      await held;
      return {
        found: "matched",
        line: first.id,
        receipt: receipt(2, [{ ...first, outcome: "matched", checked_by: ME }, second]),
      };
    }
    return { found: "matched", line: second.id, receipt: both };
  });
  await user.type(scanBox(), "RC000001{Enter}");
  await user.type(scanBox(), "RC000002{Enter}");
  expect(await screen.findByText("TUB-000002: matched.")).toBeVisible();
  await act(async () => {
    release();
    await held;
  });
  await screen.findByText("TUB-000001: matched.");
  for (const hrid of ["TUB-000001", "TUB-000002"]) {
    expect(line(hrid).getByRole("button", { name: "Matched" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  }
  expect(screen.getByText("All checked.", { exact: false })).toBeVisible();
});

it("says plainly when a tube was already checked or its extra was removed, and restores it with a reason", async () => {
  const checked = tube(1, {
    outcome: "matched",
    checked_by: ADA,
    checked_at: "2026-01-12T09:05:00Z",
  });
  const removed = tube(7, {
    expected: false,
    outcome: "extra",
    code: "UNLABELLED-7",
    ref: null,
    entity: null,
  });
  const shown = receipt(4, [checked], { removed_lines: [removed] });
  const { user } = await openReceipt(shown);
  const removedList = within(screen.getByRole("list", { name: "Removed extras" }));
  expect(removedList.getByText("UNLABELLED-7")).toBeVisible();
  expect(
    within(screen.getByRole("list", { name: "Checklist" })).queryByText("UNLABELLED-7"),
  ).toBeNull();

  answer(`POST ${api}/scan`, { found: "again", line: checked.id, receipt: shown });
  await user.type(scanBox(), "RC000001{Enter}");
  expect(
    await screen.findByText(
      "TUB-000001 is already checked: matched, by Ada Lovelace. Nothing changed.",
    ),
  ).toBeVisible();

  answer(`POST ${api}/scan`, { found: "removed", line: removed.id, receipt: shown });
  await user.type(scanBox(), "UNLABELLED-7{Enter}");
  expect(
    await screen.findByText(
      "UNLABELLED-7 was removed from this checklist earlier. Nothing changed: restore it if it’s part of this delivery.",
    ),
  ).toBeVisible();
  answer(
    `POST ${api}/lines/${removed.id}/restore`,
    receipt(5, [checked, { ...removed, line_revision: 2 }]),
  );
  // Beside what was heard, as well as in the list of removed extras.
  const [restore] = screen.getAllByRole("button", { name: "Restore UNLABELLED-7" });
  if (!restore) throw new Error("No way to restore it.");
  await user.click(restore);
  await why(user, "It was in the second bag", "Restore it");
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(calls("POST", `${api}/lines/${removed.id}/restore`).map((c) => c.body)).toEqual([
    { line_revision: 1, reason: "It was in the second bag" },
  ]);
  expect(screen.queryByRole("list", { name: "Removed extras" })).not.toBeInTheDocument();
});

it("keeps another person’s first outcome when theirs arrived first", async () => {
  const first = tube(1);
  const { user } = await openReceipt(receipt(1, [first]));
  answer(`PATCH ${api}/lines/${first.id}`, {
    found: "again",
    line: first.id,
    receipt: receipt(2, [{ ...first, outcome: "missing", checked_by: ADA }]),
  });
  await user.click(line("TUB-000001").getByRole("button", { name: "Matched" }));
  expect(
    await screen.findByText(
      "TUB-000001 was already checked by Ada Lovelace: missing. Theirs is kept; change it if it’s wrong.",
    ),
  ).toBeVisible();
  expect(calls("PATCH").map((c) => c.body)).toEqual([{ outcome: "matched" }]);
  expect(line("TUB-000001").getByRole("button", { name: "Missing" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
});

it("asks why before changing a recorded outcome, and keeps the condition note separate", async () => {
  const first = tube(1, {
    outcome: "matched",
    checked_by: ADA,
    checked_at: "2026-01-12T09:05:00Z",
  });
  const { user } = await openReceipt(receipt(2, [first]));
  await user.click(line("TUB-000001").getByRole("button", { name: "Damaged" }));
  const dialog = within(
    await screen.findByRole("dialog", { name: "Change TUB-000001 to damaged?" }),
  );
  expect(dialog.getByText("Recorded by Ada Lovelace", { exact: false })).toBeVisible();
  const confirm = dialog.getByRole("button", { name: "Save the change" });
  expect(confirm).toHaveAttribute("aria-disabled", "true");
  await user.type(dialog.getByRole("textbox", { name: /^Condition note/ }), "Cap cracked");
  // Without a reason it isn't sent: the field says why, and takes focus.
  await user.click(confirm);
  expect(dialog.getByRole("alert")).toHaveTextContent("Say why you’re making this change.");
  expect(dialog.getByRole("textbox", { name: "Why are you changing this?" })).toHaveFocus();
  expect(calls("PATCH")).toHaveLength(0);
  answer(`PATCH ${api}/lines/${first.id}`, {
    found: "recorded",
    line: first.id,
    receipt: receipt(3, [{ ...first, outcome: "damaged", note: "Cap cracked", line_revision: 2 }]),
  });
  await user.type(
    dialog.getByRole("textbox", { name: "Why are you changing this?" }),
    "Looked again under the lamp",
  );
  await user.click(confirm);
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(calls("PATCH").map((c) => c.body)).toEqual([
    {
      outcome: "damaged",
      note: "Cap cracked",
      line_revision: 1,
      reason: "Looked again under the lamp",
    },
  ]);
});

it("clears an outcome only with a reason", async () => {
  const first = tube(1, { outcome: "matched", checked_by: ME, note: "Cold" });
  const { user } = await openReceipt(receipt(2, [first]));
  await user.click(line("TUB-000001").getByRole("button", { name: "Clear" }));
  const dialog = within(await screen.findByRole("dialog", { name: "Clear TUB-000001’s outcome?" }));
  await user.click(dialog.getByRole("button", { name: "Clear it" }));
  expect(dialog.getByRole("alert")).toHaveTextContent("Say why you’re making this change.");
  expect(calls("PATCH")).toHaveLength(0);
  answer(`PATCH ${api}/lines/${first.id}`, {
    found: "recorded",
    line: first.id,
    receipt: receipt(3, [{ ...first, outcome: null, line_revision: 2 }]),
  });
  await user.type(
    dialog.getByRole("textbox", { name: "Why are you changing this?" }),
    "Wrong tube",
  );
  await user.click(dialog.getByRole("button", { name: "Clear it" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(calls("PATCH").map((c) => c.body)).toEqual([
    // The saved note isn't sent: left out, it stays.
    { outcome: null, line_revision: 1, reason: "Wrong tube" },
  ]);
});

it("shows who changed a line first, keeps the reason, and sends again only when asked", async () => {
  const first = tube(1, { outcome: "matched", checked_by: ME });
  const { user } = await openReceipt(receipt(2, [first]));
  // The server names the line that changed, at its version now.
  answer(
    `PATCH ${api}/lines/${first.id}`,
    problem(409, "conflict", "This receipt line changed.", undefined, {
      affected_lines: [{ line_id: first.id, line_revision: 2 }],
    }),
  );
  const theirs = {
    ...first,
    outcome: "rejected" as const,
    note: "Thawed",
    line_revision: 2,
    checked_by: ADA,
    checked_at: "2026-01-12T09:30:00Z",
  };
  answer(`GET ${api}`, receipt(3, [theirs]));
  await user.click(line("TUB-000001").getByRole("button", { name: "Missing" }));
  const dialog = await why(user, "Not in the box after all", "Save the change");
  const alert = await dialog.findByText(/^TUB-000001 changed while you were looking at it/);
  expect(alert.closest("[role=alert]")).toHaveTextContent(
    "TUB-000001: Now rejected · note “Thawed”, checked by Ada Lovelace, 12 Jan 2026, 09:30 UTC.",
  );
  expect(calls("GET", `${api}/history`)).toHaveLength(0);
  expect(dialog.getByText("Rejected")).toBeVisible();
  expect(dialog.getByRole("textbox", { name: "Why are you changing this?" })).toHaveValue(
    "Not in the box after all",
  );
  expect(calls("PATCH")).toHaveLength(1);
  answer(`PATCH ${api}/lines/${first.id}`, {
    found: "recorded",
    line: first.id,
    receipt: receipt(4, [{ ...theirs, outcome: "missing", line_revision: 3 }]),
  });
  await user.click(dialog.getByRole("button", { name: "Save the change" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(calls("PATCH").map((c) => c.body)).toEqual([
    { outcome: "missing", line_revision: 1, reason: "Not in the box after all" },
    { outcome: "missing", line_revision: 2, reason: "Not in the box after all" },
  ]);
});

it("saves a first note on a checked line in one step, and asks why only to change a saved one", async () => {
  const first = tube(1, {
    outcome: "matched",
    checked_by: ME,
    checked_at: "2026-01-12T09:05:00Z",
  });
  const { user } = await openReceipt(receipt(2, [first]));
  const note = line("TUB-000001").getByRole("textbox", { name: /^Note on/ });
  await user.type(note, "Label smudged");
  answer(`PATCH ${api}/lines/${first.id}`, {
    found: "recorded",
    line: first.id,
    receipt: receipt(3, [{ ...first, note: "Label smudged", line_revision: 2 }]),
  });
  await user.click(line("TUB-000001").getByRole("button", { name: "Save note" }));
  await waitFor(() => {
    expect(calls("PATCH")).toHaveLength(1);
  });
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  // Clearing what was saved is a correction: it asks why, and sends null.
  await user.clear(note);
  await user.click(line("TUB-000001").getByRole("button", { name: "Save note" }));
  answer(`PATCH ${api}/lines/${first.id}`, {
    found: "recorded",
    line: first.id,
    receipt: receipt(4, [{ ...first, line_revision: 3 }]),
  });
  await why(user, "It was the next tube’s", "Save the change");
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(calls("PATCH").map((c) => c.body)).toEqual([
    { outcome: "matched", note: "Label smudged", line_revision: 1 },
    { outcome: "matched", note: null, line_revision: 2, reason: "It was the next tube’s" },
  ]);
});

it("shows a line’s history: who, when, before and after, and why", async () => {
  const first = tube(1, { outcome: "damaged", note: "Cap cracked", checked_by: ADA });
  const { user } = await openReceipt(receipt(3, [first]));
  answer(
    `GET ${api}/history`,
    page([
      event({
        id: "00000000-0000-7000-8000-0000000000e2",
        line_id: first.id,
        revision: 3,
        before: { outcome: "matched", note: "", code: "RC000001" },
        after: { outcome: "damaged", note: "Cap cracked", code: "RC000001" },
        reason: "Looked again under the lamp",
      }),
      event({
        line_id: first.id,
        actor: ME,
        before: null,
        after: { outcome: "matched", note: "" },
      }),
    ]),
  );
  const toggle = line("TUB-000001").getByRole("button", { name: "History of TUB-000001" });
  expect(toggle).toHaveAttribute("aria-expanded", "false");
  await user.click(toggle);
  expect(toggle).toHaveAttribute("aria-expanded", "true");
  const history = within(
    await screen.findByRole("list", { name: "History of TUB-000001, newest first" }),
  );
  const [corrected, recorded] = history.getAllByRole("listitem");
  expect(corrected).toHaveTextContent("Outcome changed");
  expect(corrected).toHaveTextContent("From Matched → to Damaged · note “Cap cracked”");
  expect(corrected).toHaveTextContent("12 Jan 2026, 09:30 UTC · Ada Lovelace");
  expect(corrected).toHaveTextContent("Reason: “Looked again under the lamp”");
  expect(recorded).toHaveTextContent("Outcome recorded");
  expect(recorded).toHaveTextContent("Rosalind");
  expect(calls("GET", `${api}/history`)[0]?.query.get("line_id")).toBe(first.id);
});

it("asks why only when handover text that was recorded changes", async () => {
  const { user } = await openReceipt(receipt(1, [tube(1)]));
  answer(`PATCH ${api}`, receipt(2, [tube(1)], { handed_over_by: "Courier, waybill 1Z9" }));
  await user.type(screen.getByRole("textbox", { name: "Handed over by" }), "Courier, waybill 1Z9");
  await user.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() => {
    expect(calls("PATCH", api)).toHaveLength(1);
  });
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  const field = screen.getByRole("textbox", { name: "Handed over by" });
  await user.clear(field);
  await user.type(field, "Courier, waybill 1Z8");
  answer(`PATCH ${api}`, receipt(3, [tube(1)], { handed_over_by: "Courier, waybill 1Z8" }));
  await user.click(screen.getByRole("button", { name: "Save" }));
  await why(user, "Misread the waybill", "Save the change");
  await waitFor(() => {
    expect(calls("PATCH", api)).toHaveLength(2);
  });
  expect(calls("PATCH", api).map((c) => c.body)).toEqual([
    { plan_revision: 1, handed_over_by: "Courier, waybill 1Z9" },
    { plan_revision: 2, handed_over_by: "Courier, waybill 1Z8", reason: "Misread the waybill" },
  ]);
});

it("removes an extra and cancels a receipt only with a reason", async () => {
  const extra = tube(5, {
    expected: false,
    outcome: "extra",
    code: "UNLABELLED-5",
    ref: null,
    entity: null,
  });
  const { user } = await openReceipt(receipt(2, [tube(1), extra]));
  answer(
    `DELETE ${api}/lines/${extra.id}`,
    receipt(3, [tube(1)], { removed_lines: [{ ...extra, line_revision: 2 }] }),
  );
  await user.click(line("UNLABELLED-5").getByRole("button", { name: "Remove" }));
  await why(user, "Belongs to another request", "Remove it");
  expect(await screen.findByRole("list", { name: "Removed extras" })).toHaveTextContent(
    "UNLABELLED-5",
  );
  expect(calls("DELETE").map((c) => c.body)).toEqual([
    { line_revision: 1, reason: "Belongs to another request" },
  ]);
  await user.click(screen.getByRole("button", { name: "Cancel this receipt" }));
  const dialog = within(await screen.findByRole("dialog", { name: "Cancel this receipt?" }));
  await user.click(dialog.getByRole("button", { name: "Cancel the receipt" }));
  expect(calls("POST", `${api}/cancel`)).toHaveLength(0);
  answer(`POST ${api}/cancel`, receipt(4, [tube(1)], { status: "cancelled" }));
  await user.type(
    dialog.getByRole("textbox", { name: "Why are you cancelling it?" }),
    "Sent to the wrong core",
  );
  await user.click(dialog.getByRole("button", { name: "Cancel the receipt" }));
  await waitFor(() => {
    expect(calls("POST", `${api}/cancel`)).toHaveLength(1);
  });
  expect(calls("POST", `${api}/cancel`)[0]?.body).toEqual({
    plan_revision: 3,
    reason: "Sent to the wrong core",
  });
});

// --- Which tube a scan identifies -------------------------------------------------------------

const CORE = { id: "00000000-0000-7000-8000-0000000000c1", name: "Protein Core" };
const LAB = { id: "00000000-0000-7000-8000-0000000000c2", name: "Kinase screen" };

function candidate(number: number, project: typeof CORE, position: string) {
  const serial = String(number).padStart(6, "0");
  return {
    id: `tube-${serial}`,
    hrid: `TUB-${serial}`,
    title: `Lysate ${String(number)}`,
    type: "acme/lims.Tube",
    owner_project: project,
    box: { id: "box-1", hrid: "BOX-000003", title: "Incoming" },
    position,
  };
}

it("sends what the scanner sent, untrimmed, and again with the tube chosen for it", async () => {
  const first = tube(1, { code: "SHARED" });
  const second = tube(2, { code: "SHARED" });
  const { user } = await openReceipt(receipt(1, [first, second]));
  answer(`POST ${api}/scan`, (call: { body: unknown }) => {
    const body = call.body as { choice?: string };
    if (!body.choice)
      return problem(
        409,
        "scan_ambiguous",
        "More than one readable record matches this scan. Choose the intended record.",
        undefined,
        { candidates: [candidate(1, CORE, "A1"), candidate(2, LAB, "A2")] },
      );
    return {
      found: "matched",
      line: second.id,
      receipt: receipt(2, [first, { ...second, outcome: "matched", checked_by: ME }]),
    };
  });
  // A scanner's code, with the spaces it sent around it.
  await user.type(scanBox(), "  SHARED {Enter}");
  const chooser = await screen.findByRole("region", {
    name: "“SHARED” matches more than one tube",
  });
  const choices = within(chooser).getAllByRole("button", { name: /^TUB-/ });
  expect(choices.map((c) => c.textContent)).toEqual([
    "TUB-000001 Lysate 1Protein CoreBOX-000003 Incoming, A1",
    "TUB-000002 Lysate 2Kinase screenBOX-000003 Incoming, A2",
  ]);
  // Nothing was recorded, and nothing is said to be.
  expect(line("TUB-000002").getByRole("button", { name: "Matched" })).toHaveAttribute(
    "aria-pressed",
    "false",
  );
  await user.click(within(chooser).getByRole("button", { name: /^TUB-000002/ }));
  expect(await screen.findByText("TUB-000002: matched.")).toBeVisible();
  expect(calls("POST", `${api}/scan`).map((c) => c.body)).toEqual([
    { code: "  SHARED " },
    { code: "  SHARED ", choice: second.entity },
  ]);
  expect(screen.queryByRole("region", { name: /matches more than one/ })).toBeNull();
});

it("says only that a scan can’t identify a tube, and takes its printed Iki ID", async () => {
  const first = tube(1);
  const { user } = await openReceipt(receipt(1, [first]));
  const refused =
    "This scan can’t identify a tube here. Use its printed Iki ID or ask an authorized reviewer.";
  answer(`POST ${api}/scan`, (call: { body: unknown }) => {
    const body = call.body as { mode?: string };
    if (body.mode !== "iki_id") return problem(409, "scan_unavailable", refused);
    return {
      found: "matched",
      line: first.id,
      receipt: receipt(2, [{ ...first, outcome: "matched", checked_by: ME }]),
    };
  });
  await user.type(scanBox(), "RC000001{Enter}");
  expect(await screen.findByText(refused)).toBeVisible();
  expect(screen.queryByText(/more than one/)).toBeNull();
  expect(screen.queryByRole("button", { name: /^TUB-/ })).toBeNull();
  const typed = screen.getByRole("textbox", { name: "Iki ID" });
  await user.type(typed, "TUB-000001{Enter}");
  expect(await screen.findByText("TUB-000001: matched.")).toBeVisible();
  expect(calls("POST", `${api}/scan`).at(-1)?.body).toEqual({
    code: "TUB-000001",
    mode: "iki_id",
  });
  expect(screen.queryByText(refused)).toBeNull();
});

it("says a typed Iki ID none has, or one you can’t read has, in the same words", async () => {
  const { user } = await openReceipt(receipt(1, [tube(1)]));
  answer(
    `POST ${api}/scan`,
    problem(
      409,
      "scan_unavailable",
      "This scan can’t identify a tube here. Use its printed Iki ID or ask an authorized reviewer.",
    ),
  );
  await user.click(screen.getByRole("button", { name: "Type its printed Iki ID instead" }));
  await user.type(screen.getByRole("textbox", { name: "Iki ID" }), "TUB-000099{Enter}");
  expect(
    await screen.findByText(
      "No tube you can open has this Iki ID. Check the ID, or ask an authorized reviewer.",
    ),
  ).toBeVisible();
  expect(screen.queryByText(/This scan can’t identify/)).toBeNull();
});

it("says why an extra several tubes’ label matches isn’t linked to any of them", async () => {
  const first = tube(1);
  const extra = tube(9, { entity: null, ref: null, code: "VENDOR-7", expected: false });
  const { user } = await openReceipt(receipt(1, [first]));
  answer(`POST ${api}/scan`, {
    found: "ambiguous_extra",
    line: extra.id,
    receipt: receipt(2, [first, { ...extra, outcome: "extra", checked_by: ME }]),
  });
  await user.type(scanBox(), "VENDOR-7{Enter}");
  expect(
    await screen.findByText(
      "VENDOR-7 wasn’t expected: added as extra, not linked to a record, because more than one tube you can read has this label. Say what it is in its note.",
    ),
  ).toBeVisible();
});

it("shows the scanner’s exact text in the history, and how it identified the tube", async () => {
  const first = tube(1, { outcome: "matched", checked_by: ME });
  const { user } = await openReceipt(receipt(2, [first]));
  answer(
    `GET ${api}/history`,
    page([
      event({
        line_id: first.id,
        actor: ME,
        before: { outcome: null, note: "", code: "RC000001" },
        after: {
          outcome: "matched",
          note: "",
          code: "RC000001",
          scan: { code: " RC000001 ", identity_basis: "vendor", scope: "receipt", chosen: true },
        },
      }),
    ]),
  );
  await user.click(line("TUB-000001").getByRole("button", { name: "History of TUB-000001" }));
  const history = within(
    await screen.findByRole("list", { name: "History of TUB-000001, newest first" }),
  );
  const [recorded] = history.getAllByRole("listitem");
  expect(recorded).toHaveTextContent(
    "Scanned RC000001 · identified by its vendor barcode on this checklist · chosen by the receiver",
  );
  // Exactly as sent: its spaces too.
  expect(recorded?.querySelector(".scan-raw")?.textContent).toBe(" RC000001 ");
  // Its evidence isn't said again as a field.
  expect(recorded).not.toHaveTextContent("scan {");
});

// --- Where each received tube goes ------------------------------------------------------------

const BOX_ID = "00000000-0000-7000-8000-0000000b0135";
const BOX_CODE = "IKI:000000000000700080000000000B0135";

/** BOX-000135, "Incoming": four positions in a row, A1 already taken. */
function incoming(): Schemas["ContainerOut"] {
  return {
    id: BOX_ID,
    hrid: "BOX-000135",
    title: "Incoming",
    type: "acme/lims.Box",
    archived: false,
    purged: false,
    project: "project",
    shape: { rows: 1, columns: 4, size: 4, labels: "letter_number", order: "by_row" },
    holds: ["acme/lims.Tube"],
    code: "BOX-000135",
    location: {
      path: [],
      hidden: false,
      stored: true,
      position: null,
      custodian: null,
      custody_since: null,
    },
    contents: [
      {
        id: "tube-older",
        ref: {
          hrid: "TUB-000099",
          title: "TUB-000099",
          type: "acme/lims.Tube",
          archived: false,
          purged: false,
        },
        row: 1,
        column: 1,
        position: "A1",
        code: "RC000099",
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
}

const ONE_BOX = {
  status: "one",
  candidates: [
    {
      id: BOX_ID,
      hrid: "BOX-000135",
      title: "Incoming",
      type: "acme/lims.Box",
      owner_project: CORE,
      box: null,
      position: null,
    },
  ],
};

/** Two received, one matched and one damaged, and one rejected, which isn't put away. */
function received(revision = 5) {
  return receipt(revision, [
    tube(1, { outcome: "matched", checked_by: ME }),
    tube(2, { outcome: "damaged", note: "Cap cracked", checked_by: ME }),
    tube(3, { outcome: "rejected", note: "Thawed", checked_by: ME }),
  ]);
}

function putaway() {
  return within(screen.getByRole("table", { name: /^Where each goes$| · \d+ free$/ }));
}

function where(hrid: string) {
  return putaway().getByRole("combobox", { name: `Where ${hrid} goes` });
}

function completeButton() {
  return screen.getByRole("button", { name: /^Complete receiving|^Completing/ });
}

async function showBox(user: UserEvent, code = "BOX-000135") {
  await user.type(screen.getByRole("textbox", { name: /^Box/ }), code);
  await user.click(screen.getByRole("button", { name: "Show its free positions" }));
}

it("sends each received tube’s decision as shown: the box’s Iki code and well, or custody when chosen", async () => {
  const { user } = await openReceipt(received());
  answer("POST /api/v1/storage/scan/resolve", ONE_BOX);
  answer(`GET /api/v1/storage/containers/${BOX_ID}`, incoming());
  // Undecided until a box is shown, or custody is chosen: nothing is decided for them.
  expect(where("TUB-000001")).toHaveValue("");
  expect(completeButton()).toHaveAttribute("aria-disabled", "true");
  await showBox(user);
  await screen.findByRole("table", { name: "BOX-000135 Incoming · 3 free" });
  // A1 is taken: the next free ones, said in full.
  expect(where("TUB-000001")).toHaveValue("A2");
  expect(where("TUB-000002")).toHaveValue("A3");
  expect(
    within(where("TUB-000001"))
      .getAllByRole("option")
      .map((o) => o.textContent),
  ).toEqual([
    "Put away in BOX-000135, A2",
    "Put away in BOX-000135, A3",
    "Put away in BOX-000135, A4",
    "Keep in my custody",
  ]);
  expect(putaway().queryByRole("combobox", { name: "Where TUB-000003 goes" })).toBeNull();
  // The map's filled “+” wells are in its key.
  expect(within(screen.getByRole("list", { name: "Key" })).getByText("Going here")).toBeVisible();
  await user.selectOptions(where("TUB-000001"), "A4");
  await user.selectOptions(where("TUB-000002"), "Keep in my custody");
  expect(
    screen.getByText("Ready: 1 put away in BOX-000135 and 1 kept in your custody."),
  ).toBeVisible();
  answer(`POST ${api}/complete`, { ...received(6), status: "completed" });
  await user.click(completeButton());
  await waitFor(() => {
    expect(calls("POST", `${api}/complete`)).toHaveLength(1);
  });
  expect(calls("POST", `${api}/complete`)[0]?.body).toEqual({
    plan_revision: 5,
    missing_rest: false,
    put_away: [{ line: "line-000001", to: BOX_CODE, position: "A4" }],
    keep_in_custody: ["line-000002"],
  });
});

it("never completes while the box is being found, and leaves the decision open when it can’t be read", async () => {
  const { user } = await openReceipt(received());
  let release = (): void => undefined;
  const held = new Promise<void>((done) => {
    release = done;
  });
  answer("POST /api/v1/storage/scan/resolve", async () => {
    await held;
    return ONE_BOX;
  });
  answer(
    `GET /api/v1/storage/containers/${BOX_ID}`,
    problem(403, "forbidden", "You can’t read this box’s positions."),
  );
  await user.selectOptions(where("TUB-000002"), "Keep in my custody");
  await showBox(user);
  expect(await screen.findByText("Finding the box and its free positions…")).toBeVisible();
  const button = completeButton();
  expect(button).toHaveAttribute("aria-disabled", "true");
  expect(button).toHaveAccessibleDescription("Finding the box and its free positions…");
  await user.click(button);
  expect(calls("POST", `${api}/complete`)).toHaveLength(0);
  await act(async () => {
    release();
    await held;
  });
  expect(await screen.findByText("You can’t read this box’s positions.")).toBeVisible();
  // Nothing allocated: TUB-000001 is still open, and its custody choice is kept for TUB-000002.
  expect(where("TUB-000001")).toHaveValue("");
  expect(where("TUB-000002")).toHaveValue("keep");
  expect(completeButton()).toHaveAttribute("aria-disabled", "true");
  expect(
    screen.getByText(
      "Decide for TUB-000001: put it away in a free position, or keep it in your custody.",
    ),
  ).toBeVisible();
  await user.click(completeButton());
  expect(calls("POST", `${api}/complete`)).toHaveLength(0);
  // A deliberate choice for the last one, and it completes with no box at all.
  await user.selectOptions(where("TUB-000001"), "Keep in my custody");
  answer(`POST ${api}/complete`, { ...received(6), status: "completed" });
  await user.click(completeButton());
  await waitFor(() => {
    expect(calls("POST", `${api}/complete`)).toHaveLength(1);
  });
  expect(calls("POST", `${api}/complete`)[0]?.body).toEqual({
    plan_revision: 5,
    missing_rest: false,
    put_away: [],
    keep_in_custody: ["line-000001", "line-000002"],
  });
});

it("waits for a box a scan can’t settle to be chosen, and puts the tubes in the one chosen", async () => {
  const { user } = await openReceipt(received());
  const other = { ...ONE_BOX.candidates[0], id: "box-other", hrid: "BOX-000136" };
  answer("POST /api/v1/storage/scan/resolve", {
    status: "ambiguous",
    candidates: [other, ...ONE_BOX.candidates],
  });
  answer(`GET /api/v1/storage/containers/${BOX_ID}`, incoming());
  await showBox(user, "INCOMING");
  const chooser = await screen.findByRole("region", {
    name: "“INCOMING” matches more than one box",
  });
  expect(completeButton()).toHaveAccessibleDescription("Choose which box “INCOMING” is, above.");
  await user.click(within(chooser).getByRole("button", { name: /^BOX-000135/ }));
  await screen.findByRole("table", { name: "BOX-000135 Incoming · 3 free" });
  expect(where("TUB-000001")).toHaveValue("A2");
  expect(completeButton()).not.toHaveAttribute("aria-disabled");
  // The box is looked up once; the one chosen is loaded as it is now.
  expect(calls("POST", "/api/v1/storage/scan/resolve")).toHaveLength(1);
  expect(calls("GET", `/api/v1/storage/containers/${BOX_ID}`)).toHaveLength(1);
});

it("shows each of the server’s refusals on its line, and keeps the plan editable", async () => {
  const { user } = await openReceipt(received());
  answer("POST /api/v1/storage/scan/resolve", ONE_BOX);
  answer(`GET /api/v1/storage/containers/${BOX_ID}`, incoming());
  await showBox(user);
  await screen.findByRole("table", { name: "BOX-000135 Incoming · 3 free" });
  await user.selectOptions(where("TUB-000002"), "Keep in my custody");
  // Meanwhile someone puts a tube in A2, the well shown for TUB-000001.
  const filled = incoming();
  const [older] = filled.contents;
  if (!older) throw new Error("No tube in A1.");
  filled.contents = [...filled.contents, { ...older, id: "tube-newer", column: 2, position: "A2" }];
  answer(`GET /api/v1/storage/containers/${BOX_ID}`, filled);
  answer(
    `POST ${api}/complete`,
    problem(422, "validation_failed", "Check the fields.", [
      { pointer: "#/put_away/0/position", detail: "A2 is taken." },
      {
        pointer: "#/keep_in_custody/0",
        detail: "must name a matched or damaged line of this receipt exactly once.",
      },
      { pointer: "#/lines/line-000001", detail: "needs a note: say why it's damaged." },
    ]),
  );
  await user.click(completeButton());
  const alert = await screen.findByText(
    "Nothing was completed. Check each of these, then complete it again:",
  );
  // Read first: focus goes to what was refused.
  expect(alert.closest(".complete-feedback")).toHaveFocus();
  expect(alert.closest("[role=alert]")).toHaveTextContent(
    "TUB-000001: A2 is taken.TUB-000002: must name a matched or damaged line of this receipt exactly once.TUB-000001: needs a note: say why it's damaged.",
  );
  // Each on its line: the placement and custody choice where they're made, the line’s own on it.
  // The refused well is read again: taken now, it's no longer offered, and TUB-000001 is open.
  await waitFor(() => {
    expect(calls("GET", `/api/v1/storage/containers/${BOX_ID}`)).toHaveLength(2);
  });
  await screen.findByRole("table", { name: "BOX-000135 Incoming · 2 free" });
  expect(where("TUB-000001")).toHaveValue("");
  expect(
    within(where("TUB-000001"))
      .getAllByRole("option")
      .map((o) => o.textContent),
  ).toEqual([
    "Not decided",
    "Put away in BOX-000135, A3",
    "Put away in BOX-000135, A4",
    "Keep in my custody",
  ]);
  expect(where("TUB-000002")).toHaveValue("keep");
  expect(completeButton()).toHaveAttribute("aria-disabled", "true");
  expect(where("TUB-000001")).toHaveAttribute("aria-invalid", "true");
  expect(where("TUB-000001")).toHaveAccessibleDescription("A2 is taken.");
  expect(where("TUB-000002")).toHaveAccessibleDescription(
    "must name a matched or damaged line of this receipt exactly once.",
  );
  expect(line("TUB-000001").getByRole("alert")).toHaveTextContent(
    "needs a note: say why it's damaged.",
  );
  // Still editable, and sent again only when asked.
  await user.selectOptions(where("TUB-000001"), "A3");
  answer(`POST ${api}/complete`, { ...received(6), status: "completed" });
  await user.click(completeButton());
  await waitFor(() => {
    expect(calls("POST", `${api}/complete`)).toHaveLength(2);
  });
  expect(calls("POST", `${api}/complete`)[1]?.body).toMatchObject({
    put_away: [{ line: "line-000001", to: BOX_CODE, position: "A3" }],
    keep_in_custody: ["line-000002"],
  });
});

it("reloads a receipt that changed since it was reviewed, and waits to be completed again", async () => {
  const { user } = await openReceipt(received());
  await user.selectOptions(where("TUB-000001"), "Keep in my custody");
  await user.selectOptions(where("TUB-000002"), "Keep in my custody");
  answer(
    `POST ${api}/complete`,
    problem(409, "conflict", "This receipt changed.", undefined, {
      affected_lines: [{ line_id: "line-000002", line_revision: 2 }],
    }),
  );
  const fresh = receipt(6, [
    tube(1, { outcome: "matched", checked_by: ME }),
    tube(2, {
      outcome: "rejected",
      note: "Cap cracked through",
      checked_by: ADA,
      checked_at: "2026-01-12T09:30:00Z",
      line_revision: 2,
    }),
    tube(3, { outcome: "rejected", note: "Thawed", checked_by: ME }),
  ]);
  // Unchanged when it's read just before completing; changed by the time it's sent.
  let reads = 0;
  answer(`GET ${api}`, () => (++reads === 1 ? received() : fresh));
  await user.click(completeButton());
  const stale = await screen.findByText(/^The checklist changed while you were looking at it/);
  expect(stale.closest(".complete-feedback")).toHaveFocus();
  expect(stale.closest("[role=alert]")).toHaveTextContent(
    "TUB-000002: Now rejected · note “Cap cracked through”, checked by Ada Lovelace",
  );
  // The plan as it is now: TUB-000002 isn't received, so it has no decision to send.
  expect(putaway().queryByRole("combobox", { name: "Where TUB-000002 goes" })).toBeNull();
  expect(calls("POST", `${api}/complete`)).toHaveLength(1);
  answer(`POST ${api}/complete`, { ...fresh, plan_revision: 7, status: "completed" });
  await user.click(completeButton());
  await waitFor(() => {
    expect(calls("POST", `${api}/complete`)).toHaveLength(2);
  });
  expect(calls("POST", `${api}/complete`)[1]?.body).toEqual({
    plan_revision: 6,
    missing_rest: false,
    put_away: [],
    keep_in_custody: ["line-000001"],
  });
});

it("says which tubes were received without a location, and which are still waiting to be put away", async () => {
  const done = receipt(
    7,
    [
      tube(1, {
        outcome: "matched",
        put_away: {
          id: BOX_ID,
          hrid: "BOX-000135",
          title: "Incoming",
          type: "acme/lims.Box",
          position: "A2",
          archived: false,
          purged: false,
        },
      }),
      tube(2, { outcome: "matched", received_unplaced: true }),
      // Put away in a box this reader can’t see: not said to be unplaced.
      tube(3, { outcome: "damaged", note: "Cap cracked" }),
    ],
    {
      status: "completed",
      completed_at: "2026-01-12T10:00:00Z",
      received_by: ME,
      handed_over_by: "Courier 104",
    },
  );
  withTypes();
  answer(`GET /api/v1/entities/${ask}/receipts`, {
    receipts: [done],
    expected: 0,
    can_receive: true,
    why_not: null,
  });
  const { user } = await renderApp(`/storage/receive/${ask}/`);
  await user.click(await screen.findByText(/by Rosalind Franklin, from Courier 104/));
  const rows = within(screen.getByRole("table", { name: "What this receipt checked" }));
  const placed = (hrid: string) =>
    rows.getByRole("row", { name: new RegExp(`^${hrid}`) }).querySelectorAll("td")[2];
  expect(placed("TUB-000001")).toHaveTextContent("Incoming, A2");
  expect(placed("TUB-000002")).toHaveTextContent("Not put away: kept in custody");
  expect(placed("TUB-000003")).toHaveTextContent("—");
});

it("lists what was received without a location on the Storage page", async () => {
  withTypes();
  answer("GET /api/v1/storage/places", []);
  answer("GET /api/v1/storage/checked-out", []);
  answer("GET /api/v1/storage/awaiting-put-away", [
    {
      id: "tube-000002",
      hrid: "TUB-000002",
      title: "Lysate 2",
      type: "acme/lims.Tube",
      archived: false,
      purged: false,
      custodian: ADA,
      custody_since: "2026-01-12T10:00:00Z",
    },
  ]);
  await renderApp("/storage/");
  const section = within(await screen.findByRole("region", { name: "Received, not yet put away" }));
  const row = await section.findByRole("row", { name: /^TUB-000002/ });
  expect(row).toHaveTextContent("TUB-000002Lysate 2Ada Lovelace12 Jan 2026");
  expect(within(row).getByRole("link", { name: "TUB-000002" })).toHaveAttribute(
    "href",
    "/entities/TUB-000002/",
  );
});

it("waits while the Box field names another box than the one whose wells are shown", async () => {
  const { user } = await openReceipt(received());
  answer("POST /api/v1/storage/scan/resolve", ONE_BOX);
  answer(`GET /api/v1/storage/containers/${BOX_ID}`, incoming());
  await showBox(user);
  await screen.findByRole("table", { name: "BOX-000135 Incoming · 3 free" });
  expect(completeButton()).not.toHaveAttribute("aria-disabled");
  const field = screen.getByRole("textbox", { name: /^Box/ });
  await user.clear(field);
  await user.type(field, "BOX-000014");
  expect(completeButton()).toHaveAttribute("aria-disabled", "true");
  expect(completeButton()).toHaveAccessibleDescription(
    "Show the free positions in BOX-000014 first, or put the text back to BOX-000135.",
  );
  // The rows still name the box they'd go in.
  expect(where("TUB-000001")).toHaveDisplayValue("Put away in BOX-000135, A2");
  await user.click(completeButton());
  expect(calls("POST", `${api}/complete`)).toHaveLength(0);
  // Put back, it completes as shown.
  await user.clear(field);
  await user.type(field, "BOX-000135");
  expect(completeButton()).not.toHaveAttribute("aria-disabled");
  // Kept in custody, the box doesn't matter: it completes whatever the field says.
  await user.type(field, "9");
  await user.selectOptions(where("TUB-000001"), "Keep in my custody");
  await user.selectOptions(where("TUB-000002"), "Keep in my custody");
  expect(completeButton()).toHaveAccessibleDescription("Ready: 2 kept in your custody.");
  answer(`POST ${api}/complete`, { ...received(6), status: "completed" });
  await user.click(completeButton());
  await waitFor(() => {
    expect(calls("POST", `${api}/complete`)).toHaveLength(1);
  });
  expect(calls("POST", `${api}/complete`)[0]?.body).toMatchObject({
    put_away: [],
    keep_in_custody: ["line-000001", "line-000002"],
  });
});

it("says a saved handover beside its Save, without moving the page", async () => {
  const { user } = await openReceipt(received());
  answer(`PATCH ${api}`, { ...received(6), handed_over_by: "Courier 104" });
  await user.type(screen.getByRole("textbox", { name: "Handed over by" }), "Courier 104");
  await user.click(screen.getByRole("button", { name: "Save" }));
  const said = await screen.findByText("Saved.");
  expect(said.closest(".actions")).toContainElement(screen.getByRole("button", { name: "Save" }));
  expect(document.querySelector(".messages")).toBeEmptyDOMElement();
});

/** A response held back until the test lets it go. */
function holding<T>(value: T) {
  let release = (): void => undefined;
  const gate = new Promise<void>((done) => {
    release = done;
  });
  return {
    answer: async () => {
      await gate;
      return value;
    },
    release: async () => {
      await act(async () => {
        release();
        await gate;
      });
    },
  };
}

it("completes nothing on a press held while the box’s wells appeared, only on a fresh one", async () => {
  const { user } = await openReceipt(received());
  const box = holding(ONE_BOX);
  answer("POST /api/v1/storage/scan/resolve", box.answer);
  answer(`GET /api/v1/storage/containers/${BOX_ID}`, incoming());
  answer(`POST ${api}/complete`, { ...received(6), status: "completed" });
  await showBox(user);
  await screen.findByText("Finding the box and its free positions…");
  // Pressed while it can't complete; the wells appear under the press; then released.
  await user.pointer({ keys: "[MouseLeft>]", target: completeButton() });
  await box.release();
  await screen.findByRole("table", { name: "BOX-000135 Incoming · 3 free" });
  expect(completeButton()).not.toHaveAttribute("aria-disabled");
  await user.pointer({ keys: "[/MouseLeft]", target: completeButton() });
  expect(calls("POST", `${api}/complete`)).toHaveLength(0);
  // A press held while the plan changed is the same: the well chosen under it isn't approved.
  await user.pointer({ keys: "[MouseLeft>]", target: completeButton() });
  await user.selectOptions(where("TUB-000001"), "A4");
  await user.pointer({ keys: "[/MouseLeft]", target: completeButton() });
  expect(calls("POST", `${api}/complete`)).toHaveLength(0);
  // So is a key held down across it.
  await user.keyboard("{Escape}");
  completeButton().focus();
  await user.keyboard("[Space>]");
  await user.selectOptions(where("TUB-000002"), "Keep in my custody");
  completeButton().focus();
  await user.keyboard("[/Space]");
  expect(calls("POST", `${api}/complete`)).toHaveLength(0);
  // A fresh press completes the plan it was pressed on.
  await user.click(completeButton());
  await waitFor(() => {
    expect(calls("POST", `${api}/complete`)).toHaveLength(1);
  });
  expect(calls("POST", `${api}/complete`)[0]?.body).toMatchObject({
    put_away: [{ line: "line-000001", to: BOX_CODE, position: "A4" }],
    keep_in_custody: ["line-000002"],
  });
});

function refOf(number: number) {
  const { ref } = tube(number);
  if (!ref) throw new Error("A linked tube has its record.");
  return ref;
}

const TUBE_TYPE = {
  ...PROTEIN,
  name: "acme/lims.Tube",
  plugin: "acme/lims",
  label: "Tube",
  label_plural: "Tubes",
  extends: "acme/lims.Vessel",
};
const VESSEL_TYPE = {
  ...PROTEIN,
  name: "acme/lims.Vessel",
  plugin: "acme/lims",
  label: "Vessel",
  label_plural: "Vessels",
  abstract: true,
};

it.each([
  [
    "archived",
    { archived: true },
    "BOX-000135 is archived: choose another box, or keep them in your custody.",
  ],
  [
    "one you can’t put things in",
    { can: { keep: false, label: true } },
    "Putting things in BOX-000135 needs Editor in its project: choose another box, or keep them in your custody.",
  ],
])("offers no wells in a box that’s %s, and says why", async (_, changes, why) => {
  const { user } = await openReceipt(received());
  answer("POST /api/v1/storage/scan/resolve", ONE_BOX);
  answer(`GET /api/v1/storage/containers/${BOX_ID}`, { ...incoming(), ...changes });
  await showBox(user);
  expect(await screen.findByText(why)).toBeVisible();
  for (const hrid of ["TUB-000001", "TUB-000002"]) {
    expect(where(hrid)).toHaveValue("");
    expect(
      within(where(hrid))
        .getAllByRole("option")
        .map((o) => o.textContent),
    ).toEqual(["Not decided", "Keep in my custody"]);
  }
  expect(completeButton()).toHaveAttribute("aria-disabled", "true");
  expect(completeButton()).toHaveAccessibleDescription(
    "Decide for TUB-000001 and TUB-000002: put each away in a free position, or keep them in your custody.",
  );
  await user.selectOptions(where("TUB-000001"), "Keep in my custody");
  await user.selectOptions(where("TUB-000002"), "Keep in my custody");
  answer(`POST ${api}/complete`, { ...received(6), status: "completed" });
  await user.click(completeButton());
  await waitFor(() => {
    expect(calls("POST", `${api}/complete`)).toHaveLength(1);
  });
  expect(calls("POST", `${api}/complete`)[0]?.body).toMatchObject({
    put_away: [],
    keep_in_custody: ["line-000001", "line-000002"],
  });
});

it("offers no well for an archived tube, or one the box doesn’t hold, and says why on its row", async () => {
  const shown = receipt(5, [
    tube(1, { outcome: "matched", checked_by: ME }),
    tube(2, {
      outcome: "matched",
      checked_by: ME,
      ref: { ...refOf(2), archived: true },
    }),
    tube(3, {
      outcome: "matched",
      checked_by: ME,
      ref: { ...refOf(3), type: "acme/lims.Plate" },
    }),
  ]);
  const { user } = await openReceipt(shown, [
    TUBE_TYPE,
    VESSEL_TYPE,
    { ...PROTEIN, name: "acme/lims.Plate", plugin: "acme/lims", label: "Plate" },
  ]);
  answer("POST /api/v1/storage/scan/resolve", ONE_BOX);
  // It holds vessels: a tube is one, so it's offered a well; a plate isn't.
  answer(`GET /api/v1/storage/containers/${BOX_ID}`, {
    ...incoming(),
    holds: ["acme/lims.Vessel"],
  });
  await showBox(user);
  await screen.findByRole("table", { name: "BOX-000135 Incoming · 3 free" });
  expect(where("TUB-000001")).toHaveValue("A2");
  expect(where("TUB-000002")).toHaveValue("");
  expect(where("TUB-000002")).toHaveAccessibleDescription(
    "TUB-000002 is archived: restore it to put it away, or keep it in your custody.",
  );
  expect(where("TUB-000003")).toHaveAccessibleDescription(
    "BOX-000135 doesn’t hold a Plate: choose another box, or keep it in your custody.",
  );
  for (const hrid of ["TUB-000002", "TUB-000003"])
    expect(
      within(where(hrid))
        .getAllByRole("option")
        .map((o) => o.textContent),
    ).toEqual(["Not decided", "Keep in my custody"]);
  // The wells they'd have had stay free for the one that can go there.
  expect(within(where("TUB-000001")).getAllByRole("option")).toHaveLength(4);
  expect(completeButton()).toHaveAccessibleDescription(
    "Decide for TUB-000002 and TUB-000003: put each away in a free position, or keep them in your custody.",
  );
});

it("waits for each write on its way before completing, and says which", async () => {
  const first = tube(1, { outcome: "matched", checked_by: ME });
  const second = tube(2);
  const extra = tube(5, {
    expected: false,
    outcome: "extra",
    code: "UNLABELLED-5",
    ref: null,
    entity: null,
  });
  const { user } = await openReceipt(receipt(5, [first, second, extra]));
  await user.selectOptions(where("TUB-000001"), "Keep in my custody");
  expect(completeButton()).not.toHaveAttribute("aria-disabled");
  // A scan on its way.
  const matched = { ...second, outcome: "matched" as const, checked_by: ME, line_revision: 2 };
  const scan = holding({
    found: "matched",
    line: second.id,
    receipt: receipt(6, [first, matched, extra]),
  });
  answer(`POST ${api}/scan`, scan.answer);
  await user.type(scanBox(), "RC000002{Enter}");
  expect(completeButton()).toHaveAttribute("aria-disabled", "true");
  expect(completeButton()).toHaveAccessibleDescription(
    "Waiting for the scan of RC000002 to be saved.",
  );
  await user.click(completeButton());
  expect(calls("POST", `${api}/complete`)).toHaveLength(0);
  await scan.release();
  await screen.findByText("TUB-000002: matched.");
  await user.selectOptions(where("TUB-000002"), "Keep in my custody");
  // A note, and the handover, on their way together.
  const noted = receipt(7, [first, { ...matched, note: "Label smudged", line_revision: 3 }, extra]);
  const note = holding({ found: "recorded", line: second.id, receipt: noted });
  answer(`PATCH ${api}/lines/${second.id}`, note.answer);
  await user.type(line("TUB-000002").getByRole("textbox", { name: /^Note on/ }), "Label smudged");
  await user.click(line("TUB-000002").getByRole("button", { name: "Save note" }));
  expect(completeButton()).toHaveAccessibleDescription(
    "Waiting for the note on TUB-000002 to be saved.",
  );
  const handed = holding({ ...noted, plan_revision: 8, handed_over_by: "Courier 104" });
  answer(`PATCH ${api}`, handed.answer);
  await user.type(screen.getByRole("textbox", { name: "Handed over by" }), "Courier 104");
  await user.click(screen.getByRole("button", { name: "Save" }));
  expect(completeButton()).toHaveAccessibleDescription("Waiting for 2 changes to be saved.");
  await note.release();
  expect(completeButton()).toHaveAccessibleDescription("Waiting for the handover to be saved.");
  await handed.release();
  await screen.findByText("Saved.");
  // An extra's removal, with its reason.
  const removed = receipt(9, [first, { ...matched, note: "Label smudged", line_revision: 3 }], {
    handed_over_by: "Courier 104",
    removed_lines: [{ ...extra, line_revision: 2 }],
  });
  const removal = holding(removed);
  answer(`DELETE ${api}/lines/${extra.id}`, removal.answer);
  await user.click(line("UNLABELLED-5").getByRole("button", { name: "Remove" }));
  await why(user, "Belongs to another request", "Remove it");
  // Behind the reason's dialog while it's sent.
  expect(
    screen.getByRole("button", { name: "Complete receiving", hidden: true }),
  ).toHaveAccessibleDescription("Waiting for the removal of UNLABELLED-5 to be saved.");
  await removal.release();
  await screen.findByRole("list", { name: "Removed extras" });
  // Read again first, then sent with the revision that includes every write.
  answer(`GET ${api}`, removed);
  answer(`POST ${api}/complete`, { ...removed, plan_revision: 10, status: "completed" });
  expect(completeButton()).toHaveAccessibleDescription("Ready: 2 kept in your custody.");
  await user.click(completeButton());
  await waitFor(() => {
    expect(calls("POST", `${api}/complete`)).toHaveLength(1);
  });
  expect(calls("POST", `${api}/complete`)[0]?.body).toMatchObject({
    plan_revision: 9,
    keep_in_custody: ["line-000001", "line-000002"],
  });
});

it("reads the receipt again before completing, and sends nothing if it changed since it was shown", async () => {
  const { user } = await openReceipt(received());
  await user.selectOptions(where("TUB-000001"), "Keep in my custody");
  await user.selectOptions(where("TUB-000002"), "Keep in my custody");
  const fresh = receipt(6, [
    tube(1, { outcome: "matched", checked_by: ME }),
    tube(2, { outcome: "damaged", note: "Cap cracked through", checked_by: ADA, line_revision: 2 }),
    tube(3, { outcome: "rejected", note: "Thawed", checked_by: ME }),
  ]);
  answer(`GET ${api}`, fresh);
  answer(`POST ${api}/complete`, { ...fresh, plan_revision: 7, status: "completed" });
  await user.click(completeButton());
  const stale = await screen.findByText(/^The checklist changed while you were looking at it/);
  expect(stale.closest(".complete-feedback")).toHaveFocus();
  expect(stale.closest("[role=alert]")).toHaveTextContent(
    "TUB-000002: Now damaged · note “Cap cracked through”, checked by Ada Lovelace",
  );
  expect(calls("POST", `${api}/complete`)).toHaveLength(0);
  // Reviewed, it completes with the revision now shown.
  await user.click(completeButton());
  await waitFor(() => {
    expect(calls("POST", `${api}/complete`)).toHaveLength(1);
  });
  expect(calls("POST", `${api}/complete`)[0]?.body).toMatchObject({ plan_revision: 6 });
});
