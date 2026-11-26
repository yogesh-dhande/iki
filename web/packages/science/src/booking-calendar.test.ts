import { assert, describe, expect, it } from "vitest";
import "./booking-calendar.js";

function calendar() {
  const element = document.createElement("iki-booking-calendar");
  element.month = "2024-02";
  element.data = {
    label: "Schedule",
    bookings: [
      {
        id: "optics",
        title: "Lens inspection",
        startsAt: "2024-02-29T12:00:00",
        endsAt: "2024-03-01T08:00:00",
        instrument: "Microscope",
      },
      {
        id: "maintenance",
        title: "Maintenance",
        startsAt: "2024-03-01T12:00:00",
        bookedFor: "Example engineer",
        work: "EQS-000002 Optics service",
      },
      { id: "invalid", title: "No valid start", startsAt: "invalid" },
    ],
  };
  return element;
}

describe("booking calendar", () => {
  it("groups bookings by their local start day and emits only the selected host ID", () => {
    const element = calendar();
    const root = element.shadowRoot;
    assert(root);
    expect(root.querySelector("table caption")?.textContent).toMatch(/Schedule.*2024/);
    expect(root.querySelector('[role="status"]')?.textContent).toMatch(/^1 booking in /);
    const buttons = root.querySelectorAll<HTMLButtonElement>("li button");
    expect(buttons).toHaveLength(1);
    expect(buttons[0]?.closest("td")?.querySelector(".day")?.textContent).toBe("29");
    expect(buttons[0]?.getAttribute("aria-label")).toContain("Lens inspection (Microscope)");
    let selected: unknown;
    element.addEventListener("booking-open", (event) => {
      selected = (event as CustomEvent<unknown>).detail;
      expect(event.composed && event.bubbles).toBe(true);
    });
    buttons[0]?.click();
    expect(selected).toEqual({ id: "optics" });
  });

  it("redraws on navigation with static data and reports exclusive local month bounds", () => {
    const element = calendar();
    const root = element.shadowRoot;
    assert(root);
    const ranges: unknown[] = [];
    element.addEventListener("month-change", (event) => {
      ranges.push((event as CustomEvent<unknown>).detail);
    });
    const next = Array.from(root.querySelectorAll("button")).find(
      (b) => b.textContent === "Next month",
    );
    next?.click();
    expect(ranges).toEqual([
      { start: new Date(2024, 2, 1).toISOString(), end: new Date(2024, 3, 1).toISOString() },
    ]);
    expect(element.month).toBe("2024-03");
    expect(root.querySelector("li")?.textContent).toMatch(
      /Maintenance, for Example engineer · EQS-000002 Optics service$/,
    );
    expect(root.querySelector("li")?.textContent).not.toContain("Lens");
    element.month = "2025-01";
    expect(ranges).toHaveLength(1);
    expect(element.range.start).toBe(new Date(2025, 0, 1).toISOString());
    expect(() => {
      element.month = "2025-13";
    }).toThrow(RangeError);
  });

  it("renders labels as text and surfaces a host-supplied loading or error state", () => {
    const element = calendar();
    element.data = { bookings: [], label: "<img>", loading: true };
    const root = element.shadowRoot;
    assert(root);
    expect(root.querySelector("img")).toBeNull();
    expect(root.querySelector("caption")?.textContent).toContain("<img>");
    expect(root.querySelector('[role="status"]')?.textContent).toContain("finding more");
    element.data = { bookings: [], error: "Unable to load bookings" };
    expect(root.querySelector('[role="status"]')?.textContent).toBe("Unable to load bookings");
  });
});
