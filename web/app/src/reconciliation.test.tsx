import { act, screen, waitFor, within } from "@testing-library/react";
import type { UserEvent } from "@testing-library/user-event";
import { expect, it } from "vitest";

import { queryClient } from "./api";
import { renderApp, withTypes } from "./entity-fixtures";
import {
  reconciliationQuery,
  type ObservationEvent,
  type Reconciliation,
  type ReconciliationLine,
} from "./storage";
import { answer, calls, made, ME, page, problem } from "./test-server";

const id = "00000000-0000-7000-8000-000000000001";
const api = `/api/v1/reconciliations/${id}`;
const path = `/storage/reconciliations/${id}/`;

function tube(number: number, changes: Partial<ReconciliationLine> = {}): ReconciliationLine {
  const serial = String(number).padStart(6, "0");
  return {
    id: `line-${serial}`,
    entity: `tube-${serial}`,
    ref: {
      hrid: `TUB-${serial}`,
      type: "acme/lims.Tube",
      title: "",
      archived: false,
      purged: false,
    },
    code: `LAB-${serial}`,
    expected: "A1",
    found: null,
    finding: "missing",
    resolution: null,
    resolution_stale: false,
    note: "",
    resolved_by: null,
    revision: 1,
    observation: null,
    reviewed_observation: null,
    ...changes,
  };
}

function inventory(revision: number, ...lines: ReconciliationLine[]): Reconciliation {
  return {
    id,
    revision: revision,
    container: {
      id: "box",
      hrid: "BOX-000001",
      type: "acme/lims.Box",
      title: "Inventory box",
      position: null,
      archived: false,
      purged: false,
    },
    shape: { rows: 1, columns: 4, size: 4, labels: "letter_number", order: "by_row" },
    status: "open",
    note: "",
    can_change: true,
    started_at: "2026-01-12T09:00:00Z",
    started_by: ME,
    completed_at: null,
    completed_by: null,
    cancelled_by: null,
    cancel_reason: "",
    findings: {},
    lines,
  };
}

async function openInventory(...lines: ReconciliationLine[]) {
  withTypes();
  answer(`GET ${api}`, inventory(1, ...lines));
  return renderApp(path);
}

function complete() {
  return screen.getByRole("button", { name: "Complete the reconciliation" });
}

function decision(hrid: string) {
  const row = screen.getByRole("link", { name: hrid }).closest("li");
  if (!row) throw new Error(`Missing finding for ${hrid}.`);
  return within(row);
}

/** The server has a result, but the browser will receive it only when released. */
function holdResponse(route: string, result: Reconciliation) {
  let release = (): void => undefined;
  const pending = new Promise<void>((done) => {
    release = done;
  });
  answer(route, async () => {
    await pending;
    return result;
  });
  return () =>
    act(async () => {
      release();
      await pending;
    });
}

/** Someone else at the bench. */
const ADA = {
  ...ME,
  id: "00000000-0000-7000-8000-000000000002",
  name: "Ada Lovelace",
  email: "ada@example.com",
};

function event(changes: Partial<ObservationEvent>): ObservationEvent {
  return {
    id: "00000000-0000-7000-8000-0000000000e1",
    operation: "00000000-0000-7000-8000-0000000000f1",
    kind: "scan",
    receipt_id: null,
    reconciliation_id: id,
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

/** Says why in the dialog that asks, and confirms. */
async function giveReason(user: UserEvent, reason: string, confirm: string) {
  const dialog = within(await screen.findByRole("dialog"));
  await user.type(dialog.getByRole("textbox", { name: /^Why are you/ }), reason);
  await user.click(dialog.getByRole("button", { name: confirm }));
  return dialog;
}

it.each(["matched", "no_longer_found"] as const)(
  "clears a stale decision explicitly, saying why, when the finding is %s",
  async (finding) => {
    const current = tube(1, {
      finding,
      expected: finding === "matched" ? "A1" : null,
      found: finding === "matched" ? "A1" : null,
    });
    const checkedOut = tube(2, { finding: "checked_out", expected: "A2" });
    const stale: ReconciliationLine = {
      ...current,
      resolution: finding === "matched" ? "moved" : "placed",
      note: "Move to the previous scan",
      resolved_by: ME,
      resolution_stale: true,
    };
    const { user } = await openInventory(stale, checkedOut);
    const row = within(screen.getByRole("region", { name: "To resolve" }));
    expect(row.getByText("The scan changed. Review this finding and choose again.")).toBeVisible();
    expect(row.queryByText(/Move it to where it was found/)).not.toBeInTheDocument();
    expect(screen.queryByDisplayValue(stale.note)).not.toBeInTheDocument();
    expect(complete()).toHaveAttribute("aria-disabled", "true");
    await user.click(complete());
    expect(calls("POST", `${api}/complete`)).toHaveLength(0);
    const endpoint = `${api}/lines/${current.id}`;
    answer(`PATCH ${endpoint}`, problem(422, "invalid", "Could not clear this decision."));
    await user.click(row.getByRole("button", { name: "Clear previous decision" }));
    const dialog = await giveReason(user, "The tube was rescanned", "Clear it");
    expect(await dialog.findByText("Could not clear this decision.")).toBeVisible();
    const remaining = finding === "matched" ? [current, checkedOut] : [checkedOut];
    answer(`PATCH ${endpoint}`, inventory(2, ...remaining));
    await user.click(dialog.getByRole("button", { name: "Clear it" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(complete()).not.toHaveAttribute("aria-disabled", "true"));
    expect(row.queryByRole("button", { name: "Clear previous decision" })).not.toBeInTheDocument();
    expect(calls("PATCH", endpoint).map(made)).toEqual([
      // Clearing a decision clears its note: none is sent.
      { "If-Match": '"1"', resolution: null, reason: "The tube was rescanned" },
      { "If-Match": '"1"', resolution: null, reason: "The tube was rescanned" },
    ]);
  },
);

it("can’t change a saved decision without a reason", async () => {
  const moved = tube(1, {
    finding: "wrong_slot",
    expected: "A4",
    found: "A2",
    resolution: "moved",
    resolved_by: ME,
  });
  const { user } = await openInventory(moved);
  const row = decision("TUB-000001");
  await user.selectOptions(row.getByRole("combobox"), "kept");
  await user.type(row.getByRole("textbox", { name: /Note/ }), "Leave it at A4 for now");
  await user.click(row.getByRole("button", { name: "Resolve" }));
  const dialog = within(
    await screen.findByRole("dialog", { name: "Change the decision on TUB-000001?" }),
  );
  expect(dialog.getByRole("textbox", { name: "Why are you changing this?" })).toHaveFocus();
  expect(dialog.getByText("Move it to where it was found")).toBeVisible();
  await user.click(dialog.getByRole("button", { name: "Save the decision" }));
  expect(dialog.getByRole("alert")).toHaveTextContent("Say why you’re making this change.");
  expect(dialog.getByRole("textbox", { name: "Why are you changing this?" })).toHaveFocus();
  await user.type(dialog.getByRole("textbox", { name: "Why are you changing this?" }), "   ");
  await user.click(dialog.getByRole("button", { name: "Save the decision" }));
  expect(calls("PATCH")).toHaveLength(0);
  await user.click(dialog.getByRole("button", { name: "Don’t change it" }));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(row.getByRole("button", { name: "Resolve" })).toHaveFocus();
});

it("requires review after a newer count arrives and an older resolution response lands last", async () => {
  const moved = tube(1, {
    finding: "wrong_slot",
    expected: "A4",
    found: "A2",
    resolution: "moved",
    resolved_by: ME,
  });
  const unchecked = tube(2, { finding: "unchecked", expected: "A1" });
  const { user } = await openInventory(moved, unchecked);
  const finish = within(screen.getByRole("region", { name: "Complete" }));
  await user.click(finish.getByRole("checkbox", { name: /Count the 1 not scanned/ }));
  await user.type(finish.getByRole("textbox", { name: /Note/ }), "Verified the move to A2");
  expect(complete()).not.toHaveAttribute("aria-disabled", "true");

  const endpoint = `${api}/lines/${moved.id}`;
  const finishResolve = holdResponse(
    `PATCH ${endpoint}`,
    inventory(2, { ...moved, note: "Checked A2", revision: 2 }, unchecked),
  );
  const row = decision("TUB-000001");
  await user.type(row.getByRole("textbox", { name: /Note/ }), "Checked A2");
  // The decision stands: its first note is added in one step, with no reason.
  await user.click(row.getByRole("button", { name: "Resolve" }));
  await waitFor(() => {
    expect(calls("PATCH", endpoint)).toHaveLength(1);
  });
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

  // A colleague scanned the tube in A3 instead and separately approved that move.
  const newer = inventory(3, { ...moved, found: "A3", revision: 3 }, unchecked);
  answer(`GET ${api}`, newer);
  await act(async () => {
    await queryClient.invalidateQueries({
      queryKey: reconciliationQuery(id).queryKey,
      exact: true,
    });
  });
  await finishResolve();
  await waitFor(() => {
    expect(queryClient.isMutating()).toBe(0);
  });
  expect(queryClient.getQueryData(reconciliationQuery(id).queryKey)).toEqual(newer);
  expect(await screen.findByRole("button", { name: "Review latest count" })).toBeVisible();
  expect(screen.queryByDisplayValue("Verified the move to A2")).not.toBeInTheDocument();
  expect(screen.queryByRole("checkbox", { checked: true })).not.toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "Complete the reconciliation" }),
  ).not.toBeInTheDocument();
  expect(calls("POST", `${api}/complete`)).toHaveLength(0);
  expect(calls("PATCH", endpoint).map(made)[0]).toEqual({
    "If-Match": '"1"',
    resolution: "moved",
    note: "Checked A2",
  });
});

it("reviews a refused completion's count only once the latest one is shown", async () => {
  const moved = tube(1, {
    finding: "wrong_slot",
    expected: "A2",
    found: "A3",
    resolution: "moved",
    resolved_by: ME,
  });
  const { user } = await openInventory(moved);
  const finish = within(screen.getByRole("region", { name: "Complete" }));
  expect(finish.getByRole("list", { name: "Planned destinations" })).toHaveTextContent(
    "TUB-000001 → BOX-000001, A3",
  );
  // A colleague found it in A4 meanwhile, and moved it there. The refusal comes back before
  // the count as it is now does.
  answer(`POST ${api}/complete`, problem(412, "stale", "The count changed."));
  const latest = inventory(2, { ...moved, found: "A4", revision: 2 });
  const showLatest = holdResponse(`GET ${api}`, latest);
  answer(
    `GET ${api}/observations`,
    page([
      event({
        line_id: moved.id,
        before: { position: "A3", observed: true, code: "LAB-000001" },
        after: { position: "A4", observed: true, code: "LAB-000001" },
        reason: "It was in A4",
      }),
    ]),
  );
  await user.click(complete());
  expect(await finish.findByText(/^The count changed while you were looking at it/)).toBeVisible();
  const review = finish.getByRole("button", { name: "Review latest count" });
  // What changed is shown, but the count that has it isn't yet: there's nothing to review.
  await waitFor(() => {
    expect(finish.getByText(/Scan changed by Ada Lovelace/)).toBeVisible();
  });
  expect(review).toHaveAttribute("aria-disabled", "true");
  await user.click(review);
  expect(finish.queryByRole("list", { name: "Planned destinations" })).not.toBeInTheDocument();

  await showLatest();
  await waitFor(() => {
    expect(review).not.toHaveAttribute("aria-disabled", "true");
  });
  await user.click(review);
  expect(finish.getByRole("list", { name: "Planned destinations" })).toHaveTextContent(
    "TUB-000001 → BOX-000001, A4",
  );
  answer(`POST ${api}/complete`, { ...latest, status: "completed", can_change: false });
  answer(`GET ${api}`, { ...latest, status: "completed", can_change: false });
  await user.click(complete());
  expect(await screen.findByRole("heading", { name: "Completed" })).toBeVisible();
  expect(calls("POST", `${api}/complete`).map(made)).toEqual([
    { "If-Match": '"1"', missing_rest: false, note: "" },
    { "If-Match": '"2"', missing_rest: false, note: "" },
  ]);
});

it("shows what changed, and who changed it, when a first decision is refused as stale", async () => {
  const moved = tube(1, { finding: "wrong_slot", expected: "A4", found: "A2" });
  const { user } = await openInventory(moved);
  const endpoint = `${api}/lines/${moved.id}`;
  answer(
    `PATCH ${endpoint}`,
    problem(409, "count_lines_changed", "The count evidence changed.", undefined, {
      affected_lines: [{ line_id: moved.id, revision: 2 }],
    }),
  );
  answer(`GET ${api}`, inventory(2, { ...moved, found: "A3", revision: 2 }));
  answer(
    `GET ${api}/observations`,
    page([
      event({
        line_id: moved.id,
        before: { position: "A2", observed: true, code: "LAB-000001" },
        after: { position: "A3", observed: true, code: "LAB-000001" },
        reason: "It was in A3",
      }),
    ]),
  );
  const row = decision("TUB-000001");
  await user.selectOptions(row.getByRole("combobox"), "moved");
  await user.type(row.getByRole("textbox", { name: /Note/ }), "Verified at A2");
  await user.click(row.getByRole("button", { name: "Resolve" }));
  const changed = decision("TUB-000001");
  const alert = await changed.findByText(/^TUB-000001 changed while you were looking at it/);
  expect(alert.closest("[role=alert]")).toHaveTextContent(
    "Scan changed by Ada Lovelace, 12 Jan 2026, 09:30 UTC: LAB-000001 at A3 (“It was in A3”).",
  );
  expect(changed.getByText("Recorded at A4, found at A3.")).toBeVisible();
  expect(calls("PATCH", endpoint).map(made)).toEqual([
    { "If-Match": '"1"', resolution: "moved", note: "Verified at A2" },
  ]);
  expect(calls("POST", `${api}/complete`)).toHaveLength(0);
});

it("requires a new decision for a moved tube even when moving is still a legal choice", async () => {
  const observed = tube(1, {
    finding: "wrong_slot",
    expected: "A1",
    found: "A3",
    resolution: "moved",
    resolved_by: ME,
    note: "Approved A2",
    resolution_stale: true,
  });
  const { user } = await openInventory(observed);
  const row = decision("TUB-000001");
  expect(row.getByRole("combobox")).toHaveValue("");
  expect(row.getByRole("textbox", { name: /Note/ })).toHaveValue("");
  expect(screen.queryByRole("list", { name: "Planned destinations" })).not.toBeInTheDocument();
  expect(complete()).toHaveAttribute("aria-disabled", "true");
  const endpoint = `${api}/lines/${observed.id}`;
  const reviewed = inventory(2, {
    ...observed,
    note: "",
    resolution_stale: false,
    revision: 2,
  });
  answer(`PATCH ${endpoint}`, reviewed);
  await user.selectOptions(row.getByRole("combobox"), "moved");
  await user.click(row.getByRole("button", { name: "Resolve" }));
  await giveReason(user, "It was rescanned in A3", "Save the decision");
  expect(await screen.findByRole("list", { name: "Planned destinations" })).toHaveTextContent(
    "TUB-000001 → BOX-000001, A3",
  );
  expect(calls("PATCH", endpoint).map(made).at(-1)).toEqual({
    "If-Match": '"1"',
    resolution: "moved",
    // The stale decision's note isn't carried over to the new one.
    note: null,
    reason: "It was rescanned in A3",
  });
  expect(complete()).not.toHaveAttribute("aria-disabled", "true");
  const done = { ...reviewed, status: "completed", can_change: false } as const;
  answer(`GET ${api}`, done);
  answer(`POST ${api}/complete`, done);
  await user.click(complete());
  expect(await screen.findByRole("heading", { name: "Completed" })).toBeVisible();
  expect(calls("POST", `${api}/complete`).map(made)).toEqual([
    { "If-Match": '"2"', missing_rest: false, note: "" },
  ]);
});

it("discards only the draft whose scan position changed, even if its finding is still wrong position", async () => {
  const first = tube(1, { expected: "A4", found: "A3", finding: "wrong_slot" });
  const second = tube(2, { expected: "A2", found: "A4", finding: "wrong_slot" });
  const { user } = await openInventory(first, second);
  const rows = within(screen.getByRole("region", { name: "To resolve" }));
  const firstDraft = decision("TUB-000001");
  const secondDraft = decision("TUB-000002");
  await user.selectOptions(firstDraft.getByRole("combobox"), "kept");
  await user.type(firstDraft.getByRole("textbox", { name: /Note/ }), "A3 needs checking");
  await user.selectOptions(secondDraft.getByRole("combobox"), "kept");
  await user.type(secondDraft.getByRole("textbox", { name: /Note/ }), "Ask about the tube at A4");
  answer(`POST ${api}/scans`, inventory(2, { ...first, found: "A1", revision: 2 }, second));
  // The tube was scanned at A3: seeing it at A1 replaces that, and asks why.
  await user.type(screen.getByRole("textbox", { name: "What’s in A1" }), `${first.code}{Enter}`);
  const dialog = within(
    await screen.findByRole("dialog", { name: "Replace what’s recorded at A1?" }),
  );
  expect(dialog.getByRole("listitem")).toHaveTextContent(
    "TUB-000001 was scanned at A3 → , now at A1",
  );
  await giveReason(user, "Moved during the count", "Record the new scan");
  expect(await rows.findByText("Recorded at A4, found at A1.")).toBeVisible();
  expect(calls("POST", `${api}/scans`).map((c) => c.body)).toEqual([
    {
      scans: [{ position: "A1", code: first.code }],
      reviewed_lines: [{ line_id: first.id, revision: 1 }],
      reason: "Moved during the count",
    },
  ]);
  expect(rows.getByRole("combobox", { name: "What to do about TUB-000001" })).toHaveValue("");
  expect(rows.queryByDisplayValue("A3 needs checking")).not.toBeInTheDocument();
  expect(rows.getByRole("combobox", { name: "What to do about TUB-000002" })).toHaveValue("kept");
  expect(rows.getByDisplayValue("Ask about the tube at A4")).toBeVisible();
  expect(calls("PATCH")).toHaveLength(0);
});

it("sends a fresh scan in one step, and the same scan again as reviewed evidence", async () => {
  const matched = tube(1, {
    expected: "A1",
    found: "A1",
    finding: "matched",
    observation: {
      id: "00000000-0000-7000-8000-0000000000e9",
      line_id: "line-000001",
      position: "A1",
      code: "LAB-000001",
      identity_basis: "label",
      scanned_by: ADA,
      scanned_at: "2026-01-12T09:10:00Z",
    },
  });
  const missing = tube(2, { expected: "A2", finding: "missing" });
  const { user } = await openInventory(matched, missing);
  answer(
    `POST ${api}/scans`,
    inventory(2, matched, { ...missing, found: "A2", finding: "matched" }),
  );
  await user.click(screen.getByRole("gridcell", { name: /^A2,/ }));
  await user.type(screen.getByRole("textbox", { name: "What’s in A2" }), "LAB-000002{Enter}");
  await waitFor(() => {
    expect(calls("POST", `${api}/scans`)).toHaveLength(1);
  });
  await user.click(screen.getByRole("gridcell", { name: /^A1,/ }));
  expect(screen.getByText(/^Recorded at A1: TUB-000001, scanned by Ada Lovelace/)).toBeVisible();
  await user.type(screen.getByRole("textbox", { name: "What’s in A1" }), "LAB-000001{Enter}");
  await waitFor(() => {
    expect(calls("POST", `${api}/scans`)).toHaveLength(2);
  });
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(calls("POST", `${api}/scans`).map((c) => c.body)).toEqual([
    { scans: [{ position: "A2", code: "LAB-000002" }] },
    {
      scans: [{ position: "A1", code: "LAB-000001" }],
      reviewed_lines: [{ line_id: matched.id, revision: 1 }],
    },
  ]);
});

it("asks why a scan replaces an empty position, and keeps the reason when it’s refused", async () => {
  const empty = tube(9, {
    expected: null,
    found: "A3",
    finding: "empty",
    code: "",
    entity: null,
    ref: null,
  });
  const { user } = await openInventory(empty);
  answer(
    `POST ${api}/scans`,
    problem(409, "count_lines_changed", "The count evidence changed.", undefined, {
      affected_lines: [{ line_id: empty.id, revision: 2 }],
    }),
  );
  answer(`GET ${api}`, inventory(2, { ...empty, revision: 2 }));
  answer(
    `GET ${api}/observations`,
    page([
      event({
        line_id: empty.id,
        before: { position: "A3", observed: true, code: null },
        after: { position: "A3", observed: true, code: null },
        reason: "Checked again",
      }),
    ]),
  );
  await user.click(screen.getByRole("gridcell", { name: /^A3,/ }));
  await user.type(screen.getByRole("textbox", { name: "What’s in A3" }), "LAB-000077{Enter}");
  const dialog = within(
    await screen.findByRole("dialog", { name: "Replace what’s recorded at A3?" }),
  );
  expect(dialog.getByRole("listitem")).toHaveTextContent("A3: Empty → , replaced by LAB-000077");
  const why = dialog.getByRole("textbox", { name: "Why are you changing this?" });
  await user.type(why, "A tube was behind the rack");
  await user.click(dialog.getByRole("button", { name: "Record the new scan" }));
  expect(await dialog.findByText(/^A3 changed while you were looking at it/)).toBeVisible();
  expect(dialog.getByRole("alert")).toHaveTextContent("Scan changed by Ada Lovelace");
  expect(why).toHaveValue("A tube was behind the rack");
  expect(calls("POST", `${api}/scans`)).toHaveLength(1);
  answer(
    `POST ${api}/scans`,
    inventory(3, { ...empty, code: "LAB-000077", finding: "unexpected" }),
  );
  await user.click(dialog.getByRole("button", { name: "Record the new scan" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(calls("POST", `${api}/scans`).map((c) => c.body)).toEqual([
    {
      scans: [{ position: "A3", code: "LAB-000077" }],
      reviewed_lines: [{ line_id: empty.id, revision: 1 }],
      reason: "A tube was behind the rack",
    },
    {
      scans: [{ position: "A3", code: "LAB-000077" }],
      reviewed_lines: [{ line_id: empty.id, revision: 2 }],
      reason: "A tube was behind the rack",
    },
  ]);
});

it("asks why before cancelling, and says who cancelled it and why", async () => {
  const { user } = await openInventory(tube(1));
  await user.click(screen.getByRole("button", { name: "Cancel it" }));
  const cancelled = {
    ...inventory(2, tube(1)),
    status: "cancelled",
    can_change: false,
    cancelled_by: ADA,
    cancel_reason: "Box was moved to another freezer",
    completed_at: "2026-01-12T10:00:00Z",
  } as const;
  answer(`POST ${api}/cancel`, cancelled);
  answer(`GET ${api}`, cancelled);
  await giveReason(user, "Box was moved to another freezer", "Cancel it");
  expect(await screen.findByRole("heading", { name: "Cancelled" })).toBeVisible();
  expect(screen.getByText(/By Ada Lovelace .*: “Box was moved to another freezer”/)).toBeVisible();
  expect(calls("POST", `${api}/cancel`).map(made)).toEqual([
    { "If-Match": '"1"', reason: "Box was moved to another freezer" },
  ]);
});

it("lists confirmed move and placement destinations and blocks completion during scans and resolutions", async () => {
  const moved = tube(1, {
    expected: "A4",
    found: "A1",
    finding: "wrong_slot",
    resolution: "moved",
    resolved_by: ME,
  });
  const placed = tube(2, {
    expected: null,
    found: "A2",
    finding: "unexpected",
    resolution: "placed",
    resolved_by: ME,
  });
  const unchecked = tube(3, { expected: "A3", finding: "unchecked" });
  const { user } = await openInventory(moved, placed, unchecked);
  const finish = within(screen.getByRole("region", { name: "Complete" }));
  const checkbox = () => finish.getByRole("checkbox", { name: /Count the 1 not scanned/ });
  const note = () => finish.getByRole("textbox", { name: /Note/ });
  const destinations = within(finish.getByRole("list", { name: "Planned destinations" }));
  expect(destinations.getAllByRole("listitem").map((li) => li.textContent)).toEqual([
    "TUB-000001 → BOX-000001, A1",
    "TUB-000002 → BOX-000001, A2",
  ]);
  expect(complete()).toHaveAttribute("aria-disabled", "true");
  await user.click(checkbox());
  await user.type(note(), "Approved before checking A4");
  expect(complete()).not.toHaveAttribute("aria-disabled", "true");

  const empty = tube(4, {
    expected: null,
    found: "A4",
    finding: "empty",
    code: "",
    entity: null,
    ref: null,
  });
  const finishScan = holdResponse(
    `POST ${api}/scans`,
    inventory(2, moved, placed, unchecked, empty),
  );
  await user.click(screen.getByRole("gridcell", { name: /^A4,/ }));
  await user.click(screen.getByRole("button", { name: "A4 is empty" }));
  await waitFor(() => {
    expect(calls("POST", `${api}/scans`)).toHaveLength(1);
  });
  expect(complete()).toHaveAttribute("aria-disabled", "true");
  await user.click(complete());
  expect(calls("POST", `${api}/complete`)).toHaveLength(0);
  await finishScan();
  // The unchecked set is identical, but its earlier approval belonged to the old plan.
  await waitFor(() => expect(checkbox()).not.toBeChecked());
  expect(note()).toHaveValue("");
  expect(complete()).toHaveAttribute("aria-disabled", "true");
  await user.click(checkbox());
  await user.type(note(), "Reviewed both destinations");

  const endpoint = `${api}/lines/${moved.id}`;
  const finishResolve = holdResponse(
    `PATCH ${endpoint}`,
    inventory(
      3,
      { ...moved, resolution: "kept", note: "Keep the recorded slot", revision: 2 },
      placed,
      unchecked,
      empty,
    ),
  );
  const row = decision("TUB-000001");
  await user.selectOptions(row.getByRole("combobox"), "kept");
  await user.type(row.getByRole("textbox", { name: /Note/ }), "Keep the recorded slot");
  await user.click(row.getByRole("button", { name: "Resolve" }));
  await giveReason(user, "A4 is reserved", "Save the decision");
  await waitFor(() => {
    expect(calls("PATCH", endpoint)).toHaveLength(1);
  });
  const hidden = screen.getByRole("button", { name: "Complete the reconciliation", hidden: true });
  expect(hidden).toHaveAttribute("aria-disabled", "true");
  await finishResolve();
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  await waitFor(() => expect(checkbox()).not.toBeChecked());
  expect(calls("POST", `${api}/complete`)).toHaveLength(0);
  expect(note()).toHaveValue("");
  expect(complete()).toHaveAttribute("aria-disabled", "true");
  await user.click(checkbox());
  expect(complete()).not.toHaveAttribute("aria-disabled", "true");
  const remaining = finish.getByRole("list", { name: "Planned destinations" });
  expect(within(remaining).getAllByRole("listitem")).toHaveLength(1);
  expect(remaining).toHaveTextContent("TUB-000002 → BOX-000001, A2");
});

it("asks which tube a shared label is, apart from a conflict with what someone recorded", async () => {
  const first = tube(1, { expected: "A1", finding: "unchecked" });
  const second = tube(2, { expected: "A2", finding: "unchecked" });
  const { user } = await openInventory(first, second);
  const candidates = [first, second].map((line, i) => ({
    id: line.entity ?? "",
    hrid: line.ref?.hrid ?? "",
    title: line.ref?.title ?? "",
    type: "acme/lims.Tube",
    owner_project: { id: `project-${String(i)}`, name: i ? "Supplier" : "Kinase screen" },
    box: { id: "box", hrid: "BOX-000001", title: "Retains" },
    position: line.expected,
  }));
  answer(`POST ${api}/scans`, (call: { body: unknown }) => {
    const body = call.body as { scans: { choice?: string }[] };
    if (!body.scans[0]?.choice)
      return problem(
        409,
        "scan_ambiguous",
        "More than one readable record matches this scan. Choose the intended record.",
        undefined,
        { candidates, index: 0 },
      );
    return inventory(2, { ...first, found: "A4", finding: "wrong_slot" }, second);
  });
  await user.click(screen.getByRole("gridcell", { name: /^A4,/ }));
  await user.type(screen.getByRole("textbox", { name: "What’s in A4" }), "VENDOR-9{Enter}");
  const chooser = await screen.findByRole("region", {
    name: "“VENDOR-9” matches more than one tube",
  });
  // Not a stale count: nothing is read again, and no reason is asked for.
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(screen.queryByText(/changed while you were/)).toBeNull();
  expect(calls("GET", api)).toHaveLength(1);
  await user.click(within(chooser).getByRole("button", { name: /^TUB-000001/ }));
  await waitFor(() => {
    expect(calls("POST", `${api}/scans`)).toHaveLength(2);
  });
  expect(calls("POST", `${api}/scans`).map((c) => c.body)).toEqual([
    { scans: [{ position: "A4", code: "VENDOR-9" }] },
    { scans: [{ position: "A4", code: "VENDOR-9", choice: first.entity }] },
  ]);
});

it("says only that a count scan can’t identify a tube, and takes a typed Iki ID", async () => {
  const first = tube(1, { expected: "A1", finding: "unchecked" });
  const { user } = await openInventory(first);
  const refused =
    "This scan can’t identify a tube here. Use its printed Iki ID or ask an authorized reviewer.";
  answer(`POST ${api}/scans`, (call: { body: unknown }) => {
    const body = call.body as { scans: { mode?: string }[] };
    if (body.scans[0]?.mode !== "iki_id") return problem(409, "scan_unavailable", refused);
    return inventory(2, { ...first, found: "A1", finding: "matched" });
  });
  await user.click(screen.getByRole("gridcell", { name: /^A1,/ }));
  await user.type(screen.getByRole("textbox", { name: "What’s in A1" }), "VENDOR-9{Enter}");
  expect(await screen.findByText(refused)).toBeVisible();
  expect(screen.queryByText(/more than one/)).toBeNull();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  await user.type(screen.getByRole("textbox", { name: "Iki ID" }), "TUB-000001{Enter}");
  await waitFor(() => {
    expect(calls("POST", `${api}/scans`)).toHaveLength(2);
  });
  expect(calls("POST", `${api}/scans`).map(made).at(-1)).toEqual({
    scans: [{ position: "A1", code: "TUB-000001", mode: "iki_id" }],
  });
});

it("says a typed Iki ID none has, or one you can’t read has, in the same words", async () => {
  const first = tube(1, { expected: "A1", finding: "unchecked" });
  const { user } = await openInventory(first);
  answer(
    `POST ${api}/scans`,
    problem(
      409,
      "scan_unavailable",
      "This scan can’t identify a tube here. Use its printed Iki ID or ask an authorized reviewer.",
    ),
  );
  await user.click(screen.getByRole("gridcell", { name: /^A1,/ }));
  await user.click(screen.getByRole("button", { name: "Type its printed Iki ID instead" }));
  await user.type(screen.getByRole("textbox", { name: "Iki ID" }), "TUB-000099{Enter}");
  expect(
    await screen.findByText(
      "No tube you can open has this Iki ID. Check the ID, or ask an authorized reviewer.",
    ),
  ).toBeVisible();
  expect(screen.queryByText(/This scan can’t identify/)).toBeNull();
});

// --- An unregistered label seen in more than one slot ------------------------------------------

const LABEL = "BUFFER A";

/** A tube no readable record has, scanned at `found` by Ada. */
function unregisteredAt(found: string, number: number, revision = 1): ReconciliationLine {
  const serial = String(number).padStart(6, "0");
  return tube(number, {
    id: `00000000-0000-4000-8000-${serial.padStart(12, "0")}`,
    entity: null,
    ref: null,
    code: LABEL,
    expected: null,
    found,
    finding: "unexpected",
    revision: revision,
    observation: {
      id: `00000000-0000-7000-8000-0000000${serial.slice(1)}e`,
      line_id: `00000000-0000-4000-8000-${serial.padStart(12, "0")}`,
      position: found,
      code: LABEL,
      identity_basis: null,
      scanned_by: ADA,
      scanned_at: "2026-01-12T09:10:00Z",
    },
  });
}

function choiceAt(position: string, ...sources: ReconciliationLine[]) {
  return problem(
    409,
    "count_unregistered_choice",
    "Is this an additional unregistered tube, or did a previously observed tube move?",
    undefined,
    {
      index: 0,
      position,
      observations: sources.map((line) => ({
        line_id: line.id,
        revision: line.revision,
        position: line.found,
      })),
    },
  );
}

async function scanAt(user: UserEvent, position: string, code: string) {
  await user.click(screen.getByRole("gridcell", { name: new RegExp(`^${position},`) }));
  await user.type(screen.getByRole("textbox", { name: `What’s in ${position}` }), `${code}{Enter}`);
}

function scans() {
  return calls("POST", `${api}/scans`).map((c) => c.body);
}

it("asks whether an unregistered label seen in other slots is another tube, and records that", async () => {
  const a1 = unregisteredAt("A1", 1);
  const a2 = unregisteredAt("A2", 2);
  const { user } = await openInventory(a1, a2);
  answer(`POST ${api}/scans`, (call: { body: unknown }) => {
    const body = call.body as { scans: { unregistered?: unknown }[] };
    return body.scans[0]?.unregistered
      ? inventory(2, a1, a2, { ...unregisteredAt("A3", 3), revision: 1 })
      : choiceAt("A3", a1, a2);
  });
  await scanAt(user, "A3", LABEL);
  const chooser = await screen.findByRole("region", { name: "‘BUFFER A’ at A3 is…" });
  await waitFor(() => {
    expect(within(chooser).getByRole("heading")).toHaveFocus();
  });
  // Not a stale count: nothing is read again, and no reason is asked for yet.
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(calls("GET", api)).toHaveLength(1);
  const options = within(chooser).getAllByRole("button");
  expect(options.map((o) => o.textContent)).toEqual([
    "Another tubeA separate tube with the same label",
    expect.stringMatching(/^The tube from A1, moved hereScanned at A1 by Ada Lovelace/),
    expect.stringMatching(/^The tube from A2, moved hereScanned at A2 by Ada Lovelace/),
    "Cancel",
  ]);
  // Two lines read “BUFFER A”: each is named with where it was scanned.
  expect(screen.getByRole("combobox", { name: "What to do about BUFFER A at A1" })).toBeVisible();
  expect(screen.getByRole("combobox", { name: "What to do about BUFFER A at A2" })).toBeVisible();

  await user.click(within(chooser).getByRole("button", { name: /^Another tube/ }));
  await waitFor(() => {
    expect(screen.getByText("A3: BUFFER A, recorded as another tube.")).toBeVisible();
  });
  expect(screen.queryByRole("region", { name: /at A3 is…$/ })).not.toBeInTheDocument();
  expect(scans()).toEqual([
    { scans: [{ position: "A3", code: LABEL }] },
    { scans: [{ position: "A3", code: LABEL, unregistered: { action: "additional" } }] },
  ]);
});

it("leaves the count as it was when the choice is cancelled", async () => {
  const a1 = unregisteredAt("A1", 1);
  const { user } = await openInventory(a1);
  answer(`POST ${api}/scans`, choiceAt("A2", a1));
  await scanAt(user, "A2", LABEL);
  const chooser = await screen.findByRole("region", { name: "‘BUFFER A’ at A2 is…" });
  await user.click(within(chooser).getByRole("button", { name: "Cancel" }));
  expect(chooser).not.toBeInTheDocument();
  expect(screen.getByText("Nothing was recorded at A2.")).toBeVisible();
  // Still at A2: the person scans it again from where they were.
  expect(screen.getByRole("textbox", { name: "What’s in A2" })).toBeVisible();
  expect(scans()).toEqual([{ scans: [{ position: "A2", code: LABEL }] }]);
});

it("moves only the named tube, with its reviewed version and a reason", async () => {
  const a1 = unregisteredAt("A1", 1);
  const a2 = unregisteredAt("A2", 2);
  const { user } = await openInventory(a1, a2);
  answer(`POST ${api}/scans`, (call: { body: unknown }) => {
    const body = call.body as { scans: { unregistered?: unknown }[]; reason?: string };
    if (!body.scans[0]?.unregistered) return choiceAt("A3", a1, a2);
    if (!body.reason)
      return problem(422, "invalid", "Check the fields.", [
        { pointer: "#/reason", detail: "Say why the evidence changed." },
      ]);
    return inventory(2, a1, { ...a2, found: "A3", revision: 2 });
  });
  await scanAt(user, "A3", LABEL);
  const chooser = await screen.findByRole("region", { name: "‘BUFFER A’ at A3 is…" });
  const fromA2 = within(chooser).getByRole("button", { name: /^The tube from A2, moved here/ });
  await user.click(fromA2);
  let dialog = within(await screen.findByRole("dialog", { name: "Move the tube from A2 to A3?" }));
  expect(dialog.getByText("‘BUFFER A’ at A3: the tube from A2, moved here.")).toBeVisible();
  expect(dialog.getByRole("listitem")).toHaveTextContent(
    /^BUFFER A was scanned at A2, scanned by Ada Lovelace .*, now at A3$/,
  );
  expect(
    dialog.getByText(/Any other tube with this label stays where it was scanned/),
  ).toBeVisible();
  const why = dialog.getByRole("textbox", { name: "Why are you changing this?" });
  expect(why).toHaveFocus();
  // Not changing it leaves the choice where it was, and the count as it is.
  await user.click(dialog.getByRole("button", { name: "Don’t change it" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(fromA2).toHaveFocus();
  expect(scans()).toHaveLength(1);

  await user.click(fromA2);
  dialog = within(await screen.findByRole("dialog", { name: "Move the tube from A2 to A3?" }));
  await user.click(dialog.getByRole("button", { name: "Record the move" }));
  expect(dialog.getByRole("alert")).toHaveTextContent("Say why you’re making this change.");
  expect(scans()).toHaveLength(1);
  // A reason the server still refuses is said on its field.
  const reason = "Took the tube from A2 to A3 while counting";
  await user.type(dialog.getByRole("textbox", { name: "Why are you changing this?" }), reason);
  answer(
    `POST ${api}/scans`,
    problem(422, "invalid", "Check the fields.", [
      { pointer: "#/reason", detail: "Say why the evidence changed." },
    ]),
  );
  await user.click(dialog.getByRole("button", { name: "Record the move" }));
  expect(await dialog.findByText("Say why the evidence changed.")).toBeVisible();
  expect(dialog.getByRole("textbox", { name: "Why are you changing this?" })).toHaveAttribute(
    "aria-invalid",
    "true",
  );
  answer(`POST ${api}/scans`, inventory(2, a1, { ...a2, found: "A3", revision: 2 }));
  await user.click(dialog.getByRole("button", { name: "Record the move" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(screen.getByText("A3: BUFFER A, moved from A2.")).toBeVisible();
  const move = {
    scans: [{ position: "A3", code: LABEL, unregistered: { action: "move", line_id: a2.id } }],
    reviewed_lines: [{ line_id: a2.id, revision: 1 }],
    reason,
  };
  expect(scans()).toEqual([{ scans: [{ position: "A3", code: LABEL }] }, move, move]);
});

it("reviews what's recorded where the moved tube lands, and asks there which tube it is", async () => {
  const a1 = unregisteredAt("A1", 1);
  const empty = tube(9, {
    expected: null,
    found: "A3",
    finding: "empty",
    code: "",
    entity: null,
    ref: null,
    revision: 4,
  });
  const { user } = await openInventory(a1, empty);
  answer(`POST ${api}/scans`, (call: { body: unknown }) => {
    const body = call.body as { scans: { unregistered?: unknown }[] };
    return body.scans[0]?.unregistered ? inventory(2, { ...a1, found: "A3" }) : choiceAt("A3", a1);
  });
  await scanAt(user, "A3", LABEL);
  let dialog = within(
    await screen.findByRole("dialog", { name: "Replace what’s recorded at A3?" }),
  );
  const reason = "The tube was in A3 all along";
  await user.type(dialog.getByRole("textbox", { name: "Why are you changing this?" }), reason);
  await user.click(dialog.getByRole("button", { name: "Record the new scan" }));
  // Which tube it is, asked in the dialog, with the reason kept as typed.
  const chooser = within(await dialog.findByRole("region", { name: "‘BUFFER A’ at A3 is…" }));
  expect(dialog.getByRole("button", { name: "Record the new scan" })).toHaveAttribute(
    "aria-disabled",
    "true",
  );
  expect(screen.queryByText(/changed while you were/)).not.toBeInTheDocument();
  await user.click(chooser.getByRole("button", { name: /^The tube from A1, moved here/ }));
  dialog = within(screen.getByRole("dialog", { name: "Move the tube from A1 to A3?" }));
  expect(dialog.getByText("‘BUFFER A’ at A3: the tube from A1, moved here.")).toHaveFocus();
  expect(dialog.getAllByRole("listitem").map((li) => li.textContent)).toEqual([
    expect.stringMatching(/^BUFFER A was scanned at A1/),
    expect.stringMatching(/^A3: Empty/),
  ]);
  expect(dialog.getByRole("textbox", { name: "Why are you changing this?" })).toHaveValue(reason);
  await user.click(dialog.getByRole("button", { name: "Record the move" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(scans()).toEqual([
    {
      scans: [{ position: "A3", code: LABEL }],
      reviewed_lines: [{ line_id: empty.id, revision: 4 }],
      reason,
    },
    {
      scans: [{ position: "A3", code: LABEL, unregistered: { action: "move", line_id: a1.id } }],
      reviewed_lines: [
        { line_id: a1.id, revision: 1 },
        { line_id: empty.id, revision: 4 },
      ],
      reason,
    },
  ]);
});

it("shows who changed the tube chosen as moved, and asks again", async () => {
  const a1 = unregisteredAt("A1", 1);
  const a2 = unregisteredAt("A2", 2);
  const reread = { ...unregisteredAt("A2", 2, 2) };
  const { user } = await openInventory(a1, a2);
  answer(`POST ${api}/scans`, choiceAt("A3", a1, a2));
  await scanAt(user, "A3", LABEL);
  const chooser = await screen.findByRole("region", { name: "‘BUFFER A’ at A3 is…" });
  await user.click(within(chooser).getByRole("button", { name: /^The tube from A2/ }));
  const dialog = within(
    await screen.findByRole("dialog", { name: "Move the tube from A2 to A3?" }),
  );
  const reason = "Moved during the count";
  await user.type(dialog.getByRole("textbox", { name: "Why are you changing this?" }), reason);
  // Meanwhile, Ada reads A2 again.
  answer(
    `POST ${api}/scans`,
    problem(409, "count_lines_changed", "The count evidence changed.", undefined, {
      affected_lines: [{ line_id: a2.id, revision: 2 }],
    }),
  );
  answer(`GET ${api}`, inventory(2, a1, reread));
  answer(
    `GET ${api}/observations`,
    page([
      event({
        line_id: a2.id,
        before: { position: "A2", code: LABEL },
        after: { position: "A2", code: LABEL },
      }),
    ]),
  );
  await user.click(dialog.getByRole("button", { name: "Record the move" }));
  const alert = await dialog.findByText(/^A2 changed while you were looking at it/);
  expect(alert).toBeVisible();
  expect(dialog.getByRole("alert")).toHaveTextContent(
    "BUFFER A at A2: Scan changed by Ada Lovelace",
  );
  // The choice is asked again; the reason stays as typed.
  const again = within(await dialog.findByRole("region", { name: "‘BUFFER A’ at A3 is…" }));
  expect(dialog.getByRole("textbox", { name: "Why are you changing this?" })).toHaveValue(reason);
  answer(`POST ${api}/scans`, inventory(3, a1, { ...reread, found: "A3", revision: 3 }));
  await user.click(again.getByRole("button", { name: /^The tube from A2/ }));
  await user.click(dialog.getByRole("button", { name: "Record the move" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  const move = (revision: number) => ({
    scans: [{ position: "A3", code: LABEL, unregistered: { action: "move", line_id: a2.id } }],
    reviewed_lines: [{ line_id: a2.id, revision: revision }],
    reason,
  });
  expect(scans().slice(1)).toEqual([move(1), move(2)]);
});

it("refreshes the count when the tube chosen can no longer have moved, and offers what's left", async () => {
  const a1 = unregisteredAt("A1", 1);
  const a2 = unregisteredAt("A2", 2);
  const { user } = await openInventory(a1, a2);
  answer(`POST ${api}/scans`, choiceAt("A3", a1, a2));
  await scanAt(user, "A3", LABEL);
  const chooser = await screen.findByRole("region", { name: "‘BUFFER A’ at A3 is…" });
  await user.click(within(chooser).getByRole("button", { name: /^The tube from A2/ }));
  const dialog = within(
    await screen.findByRole("dialog", { name: "Move the tube from A2 to A3?" }),
  );
  await user.type(dialog.getByRole("textbox", { name: "Why are you changing this?" }), "Moved");
  // Meanwhile, Ada scanned A2's tube at A4.
  const elsewhere = { ...a2, found: "A4", revision: 2 };
  answer(`POST ${api}/scans`, choiceAt("A3", a1));
  answer(`GET ${api}`, inventory(2, a1, elsewhere));
  answer(
    `GET ${api}/observations`,
    page([
      event({
        line_id: a2.id,
        before: { position: "A2", code: LABEL },
        after: { position: "A4", code: LABEL },
        reason: "Found it at A4",
      }),
    ]),
  );
  await user.click(dialog.getByRole("button", { name: "Record the move" }));
  expect(await dialog.findByText(/^A2 changed while you were looking at it/)).toBeVisible();
  expect(dialog.getByRole("alert")).toHaveTextContent(
    "BUFFER A at A4: Scan changed by Ada Lovelace, 12 Jan 2026, 09:30 UTC: BUFFER A at A4 (“Found it at A4”)",
  );
  const again = within(await dialog.findByRole("region", { name: "‘BUFFER A’ at A3 is…" }));
  expect(again.getAllByRole("button").map((b) => b.textContent)).toEqual([
    "Another tubeA separate tube with the same label",
    expect.stringMatching(/^The tube from A1, moved here/),
  ]);
  expect(dialog.getByRole("button", { name: "Record the new scan" })).toHaveAttribute(
    "aria-disabled",
    "true",
  );
  expect(calls("GET", api)).toHaveLength(2);
  expect(scans()).toHaveLength(2);
});

it("keeps the registered-tube chooser and the neutral refusal apart from this choice", async () => {
  const a1 = unregisteredAt("A1", 1);
  const { user } = await openInventory(a1);
  answer(
    `POST ${api}/scans`,
    problem(
      409,
      "scan_unavailable",
      "This scan can’t identify a tube here. Use its printed Iki ID or ask an authorized reviewer.",
    ),
  );
  await scanAt(user, "A2", LABEL);
  expect(await screen.findByText(/^This scan can’t identify a tube here/)).toBeVisible();
  expect(screen.queryByRole("region", { name: /is…$/ })).not.toBeInTheDocument();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  // A rescan where it was is the same evidence again: no choice, no reason.
  answer(`POST ${api}/scans`, inventory(2, { ...a1, revision: 2 }));
  await scanAt(user, "A1", LABEL);
  await waitFor(() => {
    expect(scans()).toHaveLength(2);
  });
  expect(scans()[1]).toEqual({
    scans: [{ position: "A1", code: LABEL }],
    reviewed_lines: [{ line_id: a1.id, revision: 1 }],
  });
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});
