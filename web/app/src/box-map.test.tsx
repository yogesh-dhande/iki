/**
 * A container's map (ADR 0180): its positions named as the box labels them, moved through
 * with the arrow keys, opened, selected and marked; and the free positions in order.
 */

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { BoxMap, slotKey, slotName } from "./box-map";
import {
  freeSlots,
  letters,
  positionLabel,
  resolutionsFor,
  type Occupant,
  type Shape,
} from "./storage";

const NINE: Shape = { rows: 9, columns: 9, labels: "letter_number", order: "by_row", size: 81 };
const RACK: Shape = { rows: 5, columns: 4, labels: "number", order: "by_column", size: 20 };

function tube(row: number, column: number, more: Partial<Occupant> = {}): Occupant {
  const n = String(row * 10 + column).padStart(6, "0");
  return {
    id: `00000000-0000-7000-8000-000000${n}`,
    ref: {
      hrid: `TUBE-${n}`,
      title: `Lysate ${String(row)}.${String(column)}`,
      type: "acme/lims.Tube",
      archived: false,
      purged: false,
    },
    row,
    column,
    position: positionLabel(NINE, row, column),
    code: null,
    custodian: null,
    custody_since: null,
    container: false,
    count: 0,
    ...more,
  };
}

describe("positions", () => {
  it("names a box's as A1 to I9, and a rack's by number in its order", () => {
    expect(letters(1)).toBe("A");
    expect(letters(26)).toBe("Z");
    expect(letters(27)).toBe("AA");
    expect(positionLabel(NINE, 1, 1)).toBe("A1");
    expect(positionLabel(NINE, 8, 12)).toBe("H12");
    // Numbered down each column first.
    expect(positionLabel(RACK, 1, 1)).toBe("1");
    expect(positionLabel(RACK, 2, 1)).toBe("2");
    expect(positionLabel(RACK, 1, 2)).toBe("6");
    expect(positionLabel(RACK, 5, 4)).toBe("20");
  });

  it("finds the free ones in the grid's order", () => {
    const free = freeSlots(NINE, [tube(1, 1), tube(1, 3)]);
    expect(free.slice(0, 3).map((s) => s.label)).toEqual(["A2", "A4", "A5"]);
    expect(free).toHaveLength(79);
    const rack = freeSlots(RACK, []);
    expect(rack.slice(0, 3).map((s) => s.label)).toEqual(["1", "2", "3"]);
  });

  it("offers what each finding is resolved as, as the server allows", () => {
    const line = {
      id: "l",
      entity: "e",
      ref: null,
      code: "",
      expected: "A1",
      found: "A2",
      note: "",
      resolution: null,
      resolution_stale: false,
      resolved_by: null,
      line_revision: 1,
      observation: null,
      reviewed_observation: null,
    };
    expect(resolutionsFor({ ...line, finding: "wrong_slot" })).toEqual(["moved", "kept"]);
    expect(resolutionsFor({ ...line, finding: "missing" })).toEqual(["removed", "kept"]);
    expect(resolutionsFor({ ...line, finding: "unexpected" })).toEqual(["placed", "kept"]);
    expect(resolutionsFor({ ...line, entity: null, finding: "unexpected" })).toEqual(["kept"]);
    expect(resolutionsFor({ ...line, finding: "matched" })).toEqual([]);
    expect(resolutionsFor({ ...line, finding: "unchecked" })).toEqual([]);
  });
});

describe("the box map", () => {
  it("is a grid with its rows and columns headed, each position saying what's in it", () => {
    render(
      <BoxMap
        label="Kinase retains’ positions"
        shape={NINE}
        contents={[
          tube(1, 1),
          tube(2, 3, {
            custodian: {
              id: "u",
              name: "Ada",
              email: "ada@example.com",
              active: true,
              kind: "person",
              imported_from: "",
            },
          }),
          { ...tube(3, 3), id: null, ref: null },
        ]}
      />,
    );
    const grid = screen.getByRole("grid", { name: "Kinase retains’ positions" });
    expect(within(grid).getAllByRole("columnheader")).toHaveLength(10);
    expect(
      within(grid)
        .getAllByRole("rowheader")
        .map((h) => h.textContent),
    ).toEqual(Array.from("ABCDEFGHI"));
    expect(within(grid).getAllByRole("gridcell")).toHaveLength(81);
    expect(screen.getByRole("gridcell", { name: "A1, TUBE-000011 Lysate 1.1" })).toBeTruthy();
    expect(screen.getByRole("gridcell", { name: "A2, empty" })).toBeTruthy();
    expect(
      screen.getByRole("gridcell", { name: "B3, TUBE-000023 Lysate 2.3, checked out by Ada" }),
    ).toBeTruthy();
    expect(
      screen.getByRole("gridcell", { name: "C3, taken by a record you can’t read" }),
    ).toBeTruthy();
    // One tab stop: the first position, until another's moved to.
    expect(
      within(grid)
        .getAllByRole("gridcell")
        .filter((c) => c.tabIndex === 0),
    ).toHaveLength(1);
  });

  it.each([
    ["TUBE-000003", "3"],
    ["TUBE-000000", "0"],
    ["TUBE-9007199254740993", "9007199254740993"],
  ])("shows the unpadded digits of %s on the map", (hrid, digits) => {
    const ref = { hrid, title: hrid, type: "acme/lims.Tube", archived: false, purged: false };
    render(<BoxMap label="Box" shape={NINE} contents={[tube(1, 1, { ref })]} />);
    const cell = screen.getByRole("gridcell", { name: `A1, ${hrid}` });
    expect(cell).toHaveAttribute("title", `A1, ${hrid}`);
    expect(within(cell).getByText(digits)).toBeVisible();
  });

  it("moves between positions with the arrow keys, Home, End and Page keys, and opens one", async () => {
    const user = userEvent.setup();
    const opened = vi.fn();
    render(<BoxMap label="Box" shape={NINE} contents={[tube(2, 2)]} onActivate={opened} />);
    const a1 = screen.getByRole("gridcell", { name: "A1, empty" });
    a1.focus();
    await user.keyboard("{ArrowRight}{ArrowDown}");
    const b2 = screen.getByRole("gridcell", { name: /^B2, TUBE/ });
    expect(document.activeElement).toBe(b2);
    expect(b2.tabIndex).toBe(0);
    expect(a1.tabIndex).toBe(-1);
    await user.keyboard("{Enter}");
    expect(opened).toHaveBeenCalledWith(expect.objectContaining({ label: "B2" }));
    await user.keyboard("{End}");
    expect(document.activeElement).toBe(screen.getByRole("gridcell", { name: "B9, empty" }));
    await user.keyboard("{PageDown}");
    expect(document.activeElement).toBe(screen.getByRole("gridcell", { name: "I9, empty" }));
    await user.keyboard("{ArrowDown}{ArrowRight}");
    // At the edge it stays.
    expect(document.activeElement).toBe(screen.getByRole("gridcell", { name: "I9, empty" }));
    await user.keyboard("{Control>}{Home}{/Control}");
    expect(document.activeElement).toBe(a1);
  });

  it("selects several, saying which are selected", async () => {
    const user = userEvent.setup();
    const chosen = new Set([slotKey(1, 1)]);
    const toggled = vi.fn();
    render(
      <BoxMap
        label="Box"
        shape={NINE}
        contents={[tube(1, 1), tube(1, 2)]}
        mode="select"
        selected={chosen}
        onActivate={toggled}
      />,
    );
    expect(screen.getByRole("grid").getAttribute("aria-multiselectable")).toBe("true");
    expect(screen.getByRole("gridcell", { name: /^A1,/ }).getAttribute("aria-selected")).toBe(
      "true",
    );
    expect(screen.getByRole("gridcell", { name: /^A2,/ }).getAttribute("aria-selected")).toBe(
      "false",
    );
    await user.click(screen.getByRole("gridcell", { name: /^A2,/ }));
    expect(toggled).toHaveBeenCalledWith(expect.objectContaining({ label: "A2" }));
    screen.getByRole("gridcell", { name: /^A2,/ }).focus();
    await user.keyboard(" ");
    expect(toggled).toHaveBeenCalledTimes(2);
  });

  it("marks a record's own position, which is where focus starts", () => {
    render(
      <BoxMap label="Box" shape={NINE} contents={[tube(4, 5)]} marked={{ row: 4, column: 5 }} />,
    );
    const d5 = screen.getByRole("gridcell", { name: /^D5,/ });
    expect(d5.getAttribute("aria-current")).toBe("location");
    expect(d5.tabIndex).toBe(0);
  });

  it("says each finding, and a numbered rack has no row or column headers", () => {
    render(
      <BoxMap
        label="Rack"
        shape={RACK}
        contents={[]}
        findings={new Map([[slotKey(1, 2), "missing"]])}
      />,
    );
    expect(screen.queryAllByRole("columnheader")).toHaveLength(0);
    expect(screen.getByRole("gridcell", { name: "6, empty: Missing" })).toBeTruthy();
  });

  it("names a position as its tooltip does", () => {
    expect(slotName({ row: 1, column: 1, label: "A1", occupant: undefined })).toBe("A1, empty");
  });

  it("moves focus to a position asked for, as a scan does, even the same one again", () => {
    const { rerender } = render(<BoxMap label="Box" shape={NINE} contents={[tube(3, 4)]} />);
    rerender(<BoxMap label="Box" shape={NINE} contents={[tube(3, 4)]} focusKey="3:4#1" />);
    const c4 = screen.getByRole("gridcell", { name: /^C4,/ });
    expect(document.activeElement).toBe(c4);
    (document.activeElement as HTMLElement).blur();
    rerender(<BoxMap label="Box" shape={NINE} contents={[tube(3, 4)]} focusKey="3:4#2" />);
    expect(document.activeElement).toBe(c4);
  });
});
