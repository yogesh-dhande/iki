import { assert, expect, it, vi } from "vitest";
import "./box-map.js";

it("retains the active cell across updates and reports the current occupant", () => {
  const box = document.createElement("iki-box-map");
  const shape = { rows: 1, columns: 2, labels: "number", order: "by_row" } as const;
  box.data = { label: "Retains", shape, contents: [] };
  document.body.append(box);
  try {
    box.focusPosition(1, 2);
    const cell = document.activeElement;
    assert(cell instanceof HTMLElement);
    expect(cell.title).toBe("2, empty");
    const occupant = { row: 1, column: 2, identifier: "Blank" };
    box.data = { label: "Retains", shape, contents: [occupant] };
    expect(document.activeElement).toBe(cell);
    const opened = vi.fn();
    document.body.addEventListener("slot-activate", opened, { once: true });
    cell.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    expect(opened).toHaveBeenCalledWith(
      expect.objectContaining({
        composed: true,
        detail: { row: 1, column: 2, label: "2", occupant },
      }),
    );
    box.data = undefined;
    expect(box.childElementCount).toBe(0);
  } finally {
    box.remove();
  }
});
