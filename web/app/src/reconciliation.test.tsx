import { act, screen, waitFor, within } from "@testing-library/react";
import { expect, it } from "vitest";

import { queryClient } from "./api";
import { renderApp, withTypes } from "./entity-fixtures";
import { reconciliationQuery, type Reconciliation, type ReconciliationLine } from "./storage";
import { answer, calls, ME, problem } from "./test-server";

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
    ...changes,
  };
}

function inventory(revision: number, ...lines: ReconciliationLine[]): Reconciliation {
  return {
    id,
    plan_revision: revision,
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

it.each(["matched", "no_longer_found"] as const)(
  "clears a stale decision explicitly when the finding is %s",
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
    expect(await screen.findByText("Could not clear this decision.")).toBeVisible();
    expect(complete()).toHaveAttribute("aria-disabled", "true");
    const remaining = finding === "matched" ? [current, checkedOut] : [checkedOut];
    answer(`PATCH ${endpoint}`, inventory(2, ...remaining));
    await user.click(row.getByRole("button", { name: "Clear previous decision" }));
    await waitFor(() => expect(complete()).not.toHaveAttribute("aria-disabled", "true"));
    expect(row.queryByRole("button", { name: "Clear previous decision" })).not.toBeInTheDocument();
    expect(calls("PATCH", endpoint).at(-1)?.body).toEqual({
      plan_revision: 1,
      resolution: null,
      note: "",
    });
  },
);

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
    inventory(2, { ...moved, note: "Checked A2" }, unchecked),
  );
  const row = decision("TUB-000001");
  await user.type(row.getByRole("textbox", { name: /Note/ }), "Checked A2");
  await user.click(row.getByRole("button", { name: "Resolve" }));
  await waitFor(() => {
    expect(calls("PATCH", endpoint)).toHaveLength(1);
  });

  // A colleague scanned the tube in A3 instead and separately approved that move.
  const newer = inventory(3, { ...moved, found: "A3" }, unchecked);
  answer(`GET ${api}`, newer);
  await act(async () => {
    await queryClient.invalidateQueries({
      queryKey: reconciliationQuery(id).queryKey,
      exact: true,
    });
  });
  expect(await screen.findByRole("button", { name: "Review latest count" })).toBeVisible();
  expect(screen.queryByDisplayValue("Verified the move to A2")).not.toBeInTheDocument();
  expect(screen.queryByRole("checkbox", { checked: true })).not.toBeInTheDocument();

  await finishResolve();
  await waitFor(() => {
    expect(queryClient.isMutating()).toBe(0);
  });
  expect(queryClient.getQueryData(reconciliationQuery(id).queryKey)).toEqual(newer);
  expect(screen.getByRole("button", { name: "Review latest count" })).toBeVisible();
  expect(
    screen.queryByRole("button", { name: "Complete the reconciliation" }),
  ).not.toBeInTheDocument();
  expect(calls("POST", `${api}/complete`)).toHaveLength(0);
});

it("requires review after a stale line decision is refused, without retrying its resolution", async () => {
  const moved = tube(1, { finding: "wrong_slot", expected: "A4", found: "A2" });
  const { user } = await openInventory(moved);
  const endpoint = `${api}/lines/${moved.id}`;
  answer(`PATCH ${endpoint}`, problem(409, "conflict", "The count changed."));
  const row = decision("TUB-000001");
  await user.selectOptions(row.getByRole("combobox"), "moved");
  await user.type(row.getByRole("textbox", { name: /Note/ }), "Verified at A2");
  await user.click(row.getByRole("button", { name: "Resolve" }));
  expect(await screen.findByRole("button", { name: "Review latest count" })).toBeVisible();
  expect(row.queryByRole("button", { name: "Resolve" })).not.toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "Complete the reconciliation" }),
  ).not.toBeInTheDocument();
  expect(calls("PATCH", endpoint).map((call) => call.body)).toEqual([
    { plan_revision: 1, resolution: "moved", note: "Verified at A2" },
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
  const reviewed = inventory(2, { ...observed, note: "", resolution_stale: false });
  answer(`PATCH ${endpoint}`, reviewed);
  await user.selectOptions(row.getByRole("combobox"), "moved");
  await user.click(row.getByRole("button", { name: "Resolve" }));
  expect(await screen.findByRole("list", { name: "Planned destinations" })).toHaveTextContent(
    "TUB-000001 → BOX-000001, A3",
  );
  expect(calls("PATCH", endpoint).at(-1)?.body).toMatchObject({ resolution: "moved", note: "" });
  expect(complete()).not.toHaveAttribute("aria-disabled", "true");
  const done = { ...reviewed, status: "completed", can_change: false };
  answer(`GET ${api}`, done);
  answer(`POST ${api}/complete`, done);
  await user.click(complete());
  expect(await screen.findByRole("heading", { name: "Completed" })).toBeVisible();
  expect(calls("POST", `${api}/complete`).map((call) => call.body)).toEqual([
    { plan_revision: 2, missing_rest: false, note: "" },
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
  answer(`POST ${api}/scans`, inventory(2, { ...first, found: "A1" }, second));
  await user.type(screen.getByRole("textbox", { name: "What’s in A1" }), `${first.code}{Enter}`);
  expect(await rows.findByText("Recorded at A4, found at A1.")).toBeVisible();
  expect(rows.getByRole("combobox", { name: "What to do about TUB-000001" })).toHaveValue("");
  expect(rows.queryByDisplayValue("A3 needs checking")).not.toBeInTheDocument();
  expect(rows.getByRole("combobox", { name: "What to do about TUB-000002" })).toHaveValue("kept");
  expect(rows.getByDisplayValue("Ask about the tube at A4")).toBeVisible();
  expect(calls("PATCH")).toHaveLength(0);
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
      { ...moved, resolution: "kept", note: "Keep the recorded slot" },
      placed,
      unchecked,
      empty,
    ),
  );
  const row = decision("TUB-000001");
  await user.selectOptions(row.getByRole("combobox"), "kept");
  await user.type(row.getByRole("textbox", { name: /Note/ }), "Keep the recorded slot");
  await user.click(row.getByRole("button", { name: "Resolve" }));
  await waitFor(() => {
    expect(calls("PATCH", endpoint)).toHaveLength(1);
  });
  expect(complete()).toHaveAttribute("aria-disabled", "true");
  await user.click(complete());
  expect(calls("POST", `${api}/complete`)).toHaveLength(0);
  await finishResolve();
  await waitFor(() => expect(checkbox()).not.toBeChecked());
  expect(note()).toHaveValue("");
  expect(complete()).toHaveAttribute("aria-disabled", "true");
  await user.click(checkbox());
  expect(complete()).not.toHaveAttribute("aria-disabled", "true");
  const remaining = finish.getByRole("list", { name: "Planned destinations" });
  expect(within(remaining).getAllByRole("listitem")).toHaveLength(1);
  expect(remaining).toHaveTextContent("TUB-000002 → BOX-000001, A2");
});
