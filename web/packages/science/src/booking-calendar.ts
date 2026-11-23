/** A booking's local day is determined by its start time in the viewer's time zone. */
export interface Booking {
  id: string;
  title: string;
  startsAt: string;
  endsAt?: string;
  instrument?: string;
}

export interface BookingCalendarData {
  bookings: readonly Booking[];
  label?: string;
  loading?: boolean;
  limited?: boolean;
  error?: string | null;
}

/** ISO instants for the displayed month, from inclusive local midnight to exclusive local midnight. */
export interface BookingMonth {
  start: string;
  end: string;
}

export interface BookingOpen {
  id: string;
}

const STYLE = `
  :host { display: block; min-width: 0; color: var(--text, CanvasText); font: var(--text-base, 0.95rem) var(--font, system-ui, sans-serif); }
  .bar { display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center; margin-bottom: 0.5rem; }
  .bar p { margin: 0 0 0 auto; }
  button { font: inherit; color: inherit; background: var(--surface, Canvas); border: 1px solid var(--input-border, GrayText); border-radius: var(--radius-sm, 4px); padding: 0.25rem 0.6rem; cursor: pointer; }
  button:focus-visible, .scroll:focus-visible { outline: 3px solid var(--focus, Highlight); outline-offset: 2px; }
  .scroll { overflow-x: auto; }
  table { width: 100%; min-width: 42rem; border-collapse: collapse; table-layout: fixed; }
  caption { font-weight: 700; font-size: 1.1rem; text-align: start; padding-bottom: 0.5rem; }
  th { font-weight: 600; padding: 0.25rem; text-align: start; }
  td { border: 1px solid var(--input-border, GrayText); vertical-align: top; height: 5.5rem; padding: 0.25rem; }
  td.outside .day { opacity: 0.75; }
  td[aria-current] .day { font-weight: 700; text-decoration: underline; }
  ul { list-style: none; margin: 0.25rem 0 0; padding: 0; display: grid; gap: 0.25rem; }
  li button { width: 100%; text-align: start; font-size: 0.8rem; padding: 0.15rem 0.3rem; overflow-wrap: anywhere; }
`;

const DAYS = 7;
const time = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" });
const day = new Intl.DateTimeFormat(undefined, { weekday: "long", day: "numeric", month: "long" });
const monthName = new Intl.DateTimeFormat(undefined, { month: "long", year: "numeric" });
const weekday = new Intl.DateTimeFormat(undefined, { weekday: "short" });
const fullWeekday = new Intl.DateTimeFormat(undefined, { weekday: "long" });

function firstOfMonth(date: Date, offset = 0): Date {
  const first = new Date(date);
  first.setDate(1);
  first.setMonth(first.getMonth() + offset);
  first.setHours(0, 0, 0, 0);
  return first;
}

function button(text: string, clicked: () => void): HTMLButtonElement {
  const element = document.createElement("button");
  element.type = "button";
  element.textContent = text;
  element.addEventListener("click", clicked);
  return element;
}

/** A Monday-first month calendar. Importing this module registers `<iki-booking-calendar>`. */
export class BookingCalendar extends HTMLElement {
  #month = firstOfMonth(new Date());
  #data: BookingCalendarData = { bookings: [] };
  readonly #status = document.createElement("p");
  readonly #scroll = document.createElement("div");

  constructor() {
    super();
    const root = this.attachShadow({ mode: "open" });
    const style = document.createElement("style");
    style.textContent = STYLE;
    const bar = document.createElement("div");
    bar.className = "bar";
    this.#status.setAttribute("role", "status");
    bar.append(
      button("Previous month", () => {
        this.#show(firstOfMonth(this.#month, -1));
      }),
      button("This month", () => {
        this.#show(firstOfMonth(new Date()));
      }),
      button("Next month", () => {
        this.#show(firstOfMonth(this.#month, 1));
      }),
      this.#status,
    );
    this.#scroll.className = "scroll";
    this.#scroll.setAttribute("role", "region");
    this.#scroll.tabIndex = 0;
    root.append(style, bar, this.#scroll);
    this.#draw();
  }

  get data(): BookingCalendarData {
    return this.#data;
  }

  set data(data: BookingCalendarData) {
    this.#data = data;
    this.#draw();
  }

  /** The displayed calendar month as YYYY-MM. Assigning a month does not emit a navigation event. */
  get month(): string {
    return `${String(this.#month.getFullYear()).padStart(4, "0")}-${String(this.#month.getMonth() + 1).padStart(2, "0")}`;
  }

  set month(value: string) {
    if (!/^\d{4}-(?:0[1-9]|1[0-2])$/.test(value))
      throw new RangeError("Expected a month in YYYY-MM format");
    const date = new Date();
    date.setFullYear(Number(value.slice(0, 4)), Number(value.slice(5)) - 1, 1);
    this.#month = firstOfMonth(date);
    this.#draw();
  }

  get range(): BookingMonth {
    return { start: this.#month.toISOString(), end: firstOfMonth(this.#month, 1).toISOString() };
  }

  #show(month: Date): void {
    this.#month = month;
    this.#draw();
    this.dispatchEvent(
      new CustomEvent<BookingMonth>("month-change", {
        detail: this.range,
        bubbles: true,
        composed: true,
      }),
    );
  }

  #booking(booking: Booking, start: Date): HTMLLIElement {
    const end = new Date(booking.endsAt ?? "");
    const hours = Number.isFinite(end.getTime())
      ? `${time.format(start)}–${time.format(end)}`
      : time.format(start);
    const text = `${hours} ${booking.title}${booking.instrument ? ` (${booking.instrument})` : ""}`;
    const open = button(text, () => {
      this.dispatchEvent(
        new CustomEvent<BookingOpen>("booking-open", {
          detail: { id: booking.id },
          bubbles: true,
          composed: true,
        }),
      );
    });
    open.setAttribute("aria-label", `Open ${booking.id}: ${text}`);
    const item = document.createElement("li");
    item.append(open);
    return item;
  }

  #draw(): void {
    const { bookings, label = "Bookings", error, loading, limited } = this.#data;
    const first = new Date(this.#month);
    const end = firstOfMonth(first, 1);
    const entries = new Map<number, HTMLLIElement[]>();
    let count = 0;
    for (const booking of bookings) {
      const start = new Date(booking.startsAt);
      if (!(start >= first && start < end)) continue;
      const date = start.getDate();
      const items = entries.get(date) ?? [];
      items.push(this.#booking(booking, start));
      entries.set(date, items);
      count++;
    }
    const month = monthName.format(first);
    this.#status.textContent =
      error ??
      `${String(count)} booking${count === 1 ? "" : "s"} in ${month}` +
        (loading ? ", finding more…" : "") +
        (limited ? `: the first ${String(bookings.length)} it can show` : "");
    this.#scroll.setAttribute("aria-label", `${label} calendar`);
    const table = document.createElement("table");
    table.createCaption().textContent = `${label}: ${month}`;
    const names = table.createTHead().insertRow();
    first.setDate(1 - ((first.getDay() + 6) % DAYS));
    const at = new Date(first);
    for (let i = 0; i < DAYS; i++) {
      const th = document.createElement("th");
      th.scope = "col";
      th.abbr = fullWeekday.format(at);
      th.textContent = weekday.format(at);
      names.append(th);
      at.setDate(at.getDate() + 1);
    }
    const body = table.createTBody();
    const today = new Date().toDateString();
    for (const date = new Date(first); date < end;) {
      const week = body.insertRow();
      for (let i = 0; i < DAYS; i++) {
        const cell = week.insertCell();
        const outside = date.getMonth() !== this.#month.getMonth();
        if (outside) cell.className = "outside";
        if (date.toDateString() === today) cell.setAttribute("aria-current", "date");
        const number = document.createElement("span");
        number.className = "day";
        number.textContent = String(date.getDate());
        number.setAttribute("aria-label", day.format(date));
        cell.append(number);
        const items = outside ? undefined : entries.get(date.getDate());
        if (items) {
          const list = document.createElement("ul");
          list.append(...items);
          cell.append(list);
        }
        date.setDate(date.getDate() + 1);
      }
    }
    this.#scroll.replaceChildren(table);
  }
}

declare global {
  interface HTMLElementTagNameMap {
    "iki-booking-calendar": BookingCalendar;
  }
}

if (!customElements.get("iki-booking-calendar"))
  customElements.define("iki-booking-calendar", BookingCalendar);
