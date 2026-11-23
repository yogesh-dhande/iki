import { act, screen, waitFor, within } from "@testing-library/react";
import { expect, it } from "vitest";

import { ALICE, entity, KINASE, renderApp, withTypes } from "./entity-fixtures";
import type { Receipt, ReceiptLine } from "./storage";
import { answer, calls, problem } from "./test-server";

const request = entity({ hrid: "REQ-000007", title: "Receiving" });
const box = {
  id: "00000000-0000-7000-8000-000000000abc",
  hrid: "BOX-000014",
  title: "Incoming",
  shape: { rows: 1, columns: 1, labels: "letter_number", order: "by_row", size: 1 },
  holds: ["acme/lims.Tube"],
  can: { keep: true },
  contents: [],
};
const boxPath = "/api/v1/storage/containers/" + box.hrid;
const receiptId = "00000000-0000-7000-8000-000000000002";
const completePath = "/api/v1/receipts/" + receiptId + "/complete";

function line(n: number, outcome: ReceiptLine["outcome"]): ReceiptLine {
  const number = String(n);
  const id = "00000000-0000-7000-8000-00000000001" + number;
  const ref = entity({
    id: "00000000-0000-7000-8000-00000000002" + number,
    hrid: "TUBE-00000" + number,
    type: "acme/lims.Tube",
    title: "Tube " + number,
  });
  return {
    id,
    entity: ref.id,
    ref: { ...ref, purged: false },
    code: ref.hrid,
    expected: true,
    outcome,
    note: outcome === "damaged" ? "Cracked cap" : "",
    checked_by: ALICE,
    checked_at: "2026-11-23T00:00:00Z",
    received_unplaced: false,
    put_away: null,
  };
}

async function receive(...lines: ReceiptLine[]) {
  withTypes();
  const receipt: Receipt = {
    id: receiptId,
    status: "open",
    project: KINASE.id,
    request: { ...request, purged: false, position: null },
    outcomes: {},
    received_by: null,
    completed_at: null,
    started_by: ALICE,
    started_at: "2026-11-23T00:00:00Z",
    handed_over_by: "Courier",
    note: "",
    can_receive: true,
    lines,
  };
  answer("GET /api/v1/entities/" + request.hrid, request);
  answer("GET /api/v1/entities/" + request.hrid + "/receipts", {
    expected: lines.length,
    receipts: [receipt],
    can_receive: true,
    why_not: null,
  });
  answer("POST " + completePath, receipt);
  const { user } = await renderApp("/storage/receive/" + request.hrid + "/");
  const finish = () => screen.getByRole("button", { name: "Complete receiving" });
  const input = screen.getByRole("textbox", { name: /^Box/ });
  const show = screen.getByRole("button", { name: "Show its free positions" });
  async function chooseBox(code: string) {
    await user.clear(input);
    await user.type(input, code);
    await user.click(show);
  }
  return { user, finish, input, chooseBox };
}

it("waits for the selected box, and invalidates its plan as soon as the box is edited", async () => {
  let release!: (value: typeof box) => void;
  answer(
    "GET " + boxPath,
    () =>
      new Promise<typeof box>((resolve) => {
        release = resolve;
      }),
  );
  const { user, finish, input, chooseBox } = await receive(line(1, "matched"));
  await chooseBox(box.hrid);
  await waitFor(() => {
    expect(calls("GET", boxPath)).toHaveLength(1);
  });
  expect(finish()).toHaveAttribute("aria-disabled", "true");
  await user.click(finish());
  expect(calls("POST", completePath)).toHaveLength(0);

  act(() => {
    release(box);
  });
  await waitFor(() => expect(finish()).not.toHaveAttribute("aria-disabled"));
  expect(screen.getByRole("combobox", { name: "Position for TUBE-000001" })).toHaveValue("A1");
  await user.type(input, "5");
  expect(finish()).toHaveAttribute("aria-disabled", "true");
  await user.click(finish());
  expect(calls("POST", completePath)).toHaveLength(0);
});

it("requires an explicit custody choice for a tube without a free position", async () => {
  answer("GET " + boxPath, box);
  const matched = line(1, "matched");
  const damaged = line(2, "damaged");
  const { user, finish, chooseBox } = await receive(matched, damaged, line(3, "missing"));
  await chooseBox(box.hrid);
  const table = await screen.findByRole("table", { name: /^Where each goes in Incoming/ });
  expect(within(table).getByRole("combobox", { name: "Position for TUBE-000001" })).toHaveValue(
    "A1",
  );
  expect(finish()).toHaveAttribute("aria-disabled", "true");
  const select = within(table).getByRole("combobox", { name: "Position for TUBE-000002" });
  await user.selectOptions(select, within(select).getByRole("option", { name: "Keep in custody" }));
  await user.click(finish());
  await waitFor(() => {
    expect(calls("POST", completePath).map((c) => c.body)).toEqual([
      {
        missing_rest: false,
        put_away: [{ line: matched.id, to: box.id, position: "A1" }],
        keep_in_custody: [damaged.id],
      },
    ]);
  });
});

it("supports explicit custody without a box and keeps a failed box lookup uncommittable", async () => {
  answer("GET " + boxPath, problem(404, "not_found", "No such box"));
  const matched = line(1, "matched");
  const { user, finish, input, chooseBox } = await receive(matched);
  expect(finish()).toHaveAttribute("aria-disabled", "true");
  await chooseBox(box.hrid);
  await screen.findByText("No such box");
  expect(finish()).toHaveAttribute("aria-disabled", "true");
  await user.clear(input);
  const select = screen.getByRole("combobox", { name: "Position for TUBE-000001" });
  await user.selectOptions(select, within(select).getByRole("option", { name: "Keep in custody" }));
  await user.click(finish());
  await waitFor(() => {
    expect(calls("POST", completePath).map((c) => c.body)).toEqual([
      {
        missing_rest: false,
        put_away: [],
        keep_in_custody: [matched.id],
      },
    ]);
  });
});

it.each([
  { ...box, shape: null },
  { ...box, can: { keep: false } },
  { ...box, archived: true },
  { ...box, holds: ["acme/lims.Box"] },
])("keeps an unusable box from allocating a tube", async (invalid) => {
  answer("GET " + boxPath, invalid);
  const { user, finish, chooseBox } = await receive(line(1, "matched"));
  await chooseBox(box.hrid);
  await waitFor(() => {
    expect(calls("GET", boxPath)).toHaveLength(1);
  });
  const select = screen.getByRole("combobox", { name: "Position for TUBE-000001" });
  expect(within(select).queryByRole("option", { name: "A1" })).toBeNull();
  expect(finish()).toHaveAttribute("aria-disabled", "true");
  await user.click(finish());
  expect(calls("POST", completePath)).toHaveLength(0);
});

it("preserves an explicit custody choice when choosing a box for the remaining tubes", async () => {
  const other = {
    ...box,
    hrid: "BOX-000015",
    title: "Spare",
    shape: { ...box.shape, columns: 2, size: 2 },
  };
  answer("GET " + boxPath, box);
  answer("GET /api/v1/storage/containers/" + other.hrid, other);
  const { user, finish, chooseBox } = await receive(line(1, "matched"), line(2, "matched"));
  await chooseBox(box.hrid);
  const select = screen.getByRole("combobox", { name: "Position for TUBE-000002" });
  await user.selectOptions(select, within(select).getByRole("option", { name: "Keep in custody" }));
  await chooseBox(other.hrid);
  const table = await screen.findByRole("table", { name: /^Where each goes in Spare/ });
  expect(within(table).getByRole("combobox", { name: "Position for TUBE-000002" })).toHaveValue(
    "custody",
  );
  expect(finish()).not.toHaveAttribute("aria-disabled");
});
