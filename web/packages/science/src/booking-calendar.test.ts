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
  it("draws bookings on their local days and emits only the selected host ID", () => {
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
    // The lens inspection runs into 1 March, so it's there too, by its dates.
    const [overnight, maintenance] = Array.from(
      root.querySelectorAll("li"),
      (li) => li.textContent,
    );
    expect(overnight).toContain("Lens inspection");
    expect(maintenance).toMatch(/Maintenance, for Example engineer · EQS-000002 Optics service$/);
    expect(root.querySelector('[role="status"]')?.textContent).toMatch(/^2 bookings in /);
    element.month = "2025-01";
    expect(ranges).toHaveLength(1);
    expect(element.range.start).toBe(new Date(2025, 0, 1).toISOString());
    expect(() => {
      element.month = "2025-13";
    }).toThrow(RangeError);
  });

  it("draws a booking on each day it covers, up to its exclusive end", () => {
    const element = document.createElement("iki-booking-calendar");
    element.month = "2024-02";
    element.data = {
      bookings: [
        // A three-day run from the last evening of January.
        {
          id: "run",
          title: "Long read run",
          startsAt: "2024-01-31T20:00:00",
          endsAt: "2024-02-03T06:00:00",
        },
        // To midnight: on its own day only.
        {
          id: "late",
          title: "Late scan",
          startsAt: "2024-02-10T21:00:00",
          endsAt: "2024-02-11T00:00:00",
        },
        // Before the month: on no day of it.
        {
          id: "old",
          title: "Old run",
          startsAt: "2024-01-30T09:00:00",
          endsAt: "2024-01-31T09:00:00",
        },
      ],
    };
    const root = element.shadowRoot;
    assert(root);
    const days = (title: string) =>
      Array.from(root.querySelectorAll("li button"))
        .filter((b) => b.textContent.includes(title))
        .map((b) => b.closest("td")?.querySelector(".day")?.textContent);
    expect(days("Long read run")).toEqual(["1", "2", "3"]);
    expect(days("Late scan")).toEqual(["10"]);
    expect(days("Old run")).toEqual([]);
    expect(root.querySelector('[role="status"]')?.textContent).toMatch(/^2 bookings in /);
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

  it("leaves cancelled bookings out until asked, then strikes them through and says so", () => {
    const element = document.body.appendChild(document.createElement("iki-booking-calendar"));
    element.month = "2024-02";
    const bookings = [
      { id: "run", title: "Amplicon run", startsAt: "2024-02-12T09:00:00" },
      { id: "off", title: "Metagenomics run", startsAt: "2024-02-13T09:00:00", cancelled: true },
    ];
    element.data = { bookings, showCancelled: false };
    const root = element.shadowRoot;
    assert(root);
    const toggle = root.querySelector<HTMLInputElement>('input[type="checkbox"]');
    assert(toggle);
    expect(toggle.closest("label")?.hidden).toBe(false);
    expect(toggle.closest("label")?.textContent).toBe("Show cancelled");
    expect(toggle.checked).toBe(false);
    expect(root.querySelectorAll("li button")).toHaveLength(1);
    expect(root.querySelector('[role="status"]')?.textContent).toMatch(/^1 booking in [^,]*$/);
    const asked: unknown[] = [];
    element.addEventListener("cancelled-change", (event) => {
      asked.push((event as CustomEvent<unknown>).detail);
      expect(event.composed && event.bubbles).toBe(true);
    });
    toggle.click();
    expect(asked).toEqual([{ shown: true }]);

    element.data = { bookings, showCancelled: true };
    const off = root.querySelector<HTMLButtonElement>("li button.cancelled");
    assert(off);
    expect(off.querySelector(".tag")?.textContent).toBe("Cancelled");
    expect(off.querySelector("s")?.textContent).toMatch(/Metagenomics run$/);
    expect(off.getAttribute("aria-label")).toMatch(/^Open off: Cancelled: .*Metagenomics run$/);
    expect(root.querySelector('[role="status"]')?.textContent).toMatch(
      /^1 booking in .*, 1 cancelled$/,
    );

    // A host that doesn't offer the switch gets what it gives, still marked cancelled.
    element.data = { bookings };
    expect(toggle.closest("label")?.hidden).toBe(true);
    expect(root.querySelectorAll("li button.cancelled")).toHaveLength(1);
    element.remove();
  });
});
