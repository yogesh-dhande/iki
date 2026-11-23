import { act, screen, waitFor, within } from "@testing-library/react";
import type { UserEvent } from "@testing-library/user-event";
import { expect, it } from "vitest";

import { renderApp, withTypes } from "./entity-fixtures";
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

async function openReceipt(open: Receipt) {
  withTypes();
  answer(`GET /api/v1/entities/${ask}/receipts`, {
    receipts: [open],
    expected: open.lines.length,
    can_receive: true,
    why_not: null,
  });
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
