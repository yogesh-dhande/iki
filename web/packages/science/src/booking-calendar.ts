/** A booking is drawn on each local day it covers in the viewer's time zone, from its start
 * to its exclusive end: a booking from 22:00 to 02:00 is on both days. */
export interface Booking {
  id: string;
  title: string;
  startsAt: string;
  endsAt?: string;
  instrument?: string;
  /** Whom the time is for, when it's someone the reader should be told of. */
  bookedFor?: string;
  /** What the time is for, such as a request: its ID and title, as the host names it. */
  work?: string;
  /** Cancelled: kept, but holding nothing. Drawn struck through and labelled so. */
  cancelled?: boolean;
}

export interface BookingCalendarData {
  bookings: readonly Booking[];
  label?: string;
  loading?: boolean;
  limited?: boolean;
  error?: string | null;
  /**
   * Whether cancelled bookings are shown. Given, the calendar offers a "Show cancelled"
   * switch and draws cancelled bookings only when it's on, as a resource calendar drops a
   * cancelled meeting; it says when it's switched (`cancelled-change`), so a host that
   * finds bookings by search can find them too. Left out, it draws whatever it's given.
   */
  showCancelled?: boolean;
}

/** ISO instants for the displayed month, from inclusive local midnight to exclusive local midnight. */
export interface BookingMonth {
  start: string;
  end: string;
}

export interface BookingOpen {
  id: string;
}

export interface BookingCancelledChange {
  shown: boolean;
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
  li button.cancelled { border-style: dashed; }
  .tag { font-weight: 700; }
  label { display: inline-flex; gap: 0.35rem; align-items: center; }
  input:focus-visible { outline: 3px solid var(--focus, Highlight); outline-offset: 2px; }
  label[hidden] { display: none; }
`;

const DAYS = 7;
const time = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" });
const when = new Intl.DateTimeFormat(undefined, {
  day: "numeric",
  month: "short",
  hour: "2-digit",
  minute: "2-digit",
});
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

function midnight(date: Date): Date {
  const start = new Date(date);
  start.setHours(0, 0, 0, 0);
  return start;
}

/** The local days of the month from `first` to `end` that a booking from `start` to
 * `until` covers: its start's day, and each later day it's still on at midnight. */
function daysCovered(start: Date, until: Date | null, first: Date, end: Date): number[] {
  if (!Number.isFinite(start.getTime())) return [];
  const last = until && until > start ? until : null;
  if (start >= end || (last ? last <= first : start < first)) return [];
  const days: number[] = [];
  const at = midnight(start < first ? first : start);
  do {
    days.push(at.getDate());
    at.setDate(at.getDate() + 1);
  } while (last && at < last && at < end);
  return days;
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
  readonly #switch = document.createElement("label");
  readonly #showing = document.createElement("input");

  constructor() {
    super();
    const root = this.attachShadow({ mode: "open" });
    const style = document.createElement("style");
    style.textContent = STYLE;
    const bar = document.createElement("div");
    bar.className = "bar";
    this.#showing.type = "checkbox";
    this.#showing.addEventListener("change", () => {
      this.dispatchEvent(
        new CustomEvent<BookingCancelledChange>("cancelled-change", {
          detail: { shown: this.#showing.checked },
          bubbles: true,
          composed: true,
        }),
      );
    });
    this.#switch.append(this.#showing, "Show cancelled");
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
      this.#switch,
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

  #booking(booking: Booking, start: Date, end: Date | null): HTMLLIElement {
    // A booking over more than one day says when it starts and ends, by date.
    const format = end && midnight(end) > midnight(start) ? when : time;
    const hours = end ? `${format.format(start)}–${format.format(end)}` : time.format(start);
    const text =
      `${hours} ${booking.title}${booking.instrument ? ` (${booking.instrument})` : ""}` +
      (booking.bookedFor ? `, for ${booking.bookedFor}` : "") +
      (booking.work ? ` · ${booking.work}` : "");
    const open = button(text, () => {
      this.dispatchEvent(
        new CustomEvent<BookingOpen>("booking-open", {
          detail: { id: booking.id },
          bubbles: true,
          composed: true,
        }),
      );
    });
    if (booking.cancelled) {
      // Said in words, not only by its strike-through: what it was, and that it's off.
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = "Cancelled";
      const was = document.createElement("s");
      was.textContent = text;
      open.className = "cancelled";
      open.replaceChildren(tag, " ", was);
    }
    open.setAttribute(
      "aria-label",
      `Open ${booking.id}: ${booking.cancelled ? `Cancelled: ${text}` : text}`,
    );
    const item = document.createElement("li");
    item.append(open);
    return item;
  }

  #draw(): void {
    const { bookings, label = "Bookings", error, loading, limited, showCancelled } = this.#data;
    const offered = showCancelled !== undefined;
    this.#switch.hidden = !offered;
    this.#showing.checked = showCancelled === true;
    const first = new Date(this.#month);
    const end = firstOfMonth(first, 1);
    const entries = new Map<number, HTMLLIElement[]>();
    let count = 0;
    let cancelled = 0;
    for (const booking of bookings) {
      const start = new Date(booking.startsAt);
      const until = new Date(booking.endsAt ?? "");
      const finish = Number.isFinite(until.getTime()) ? until : null;
      const days = daysCovered(start, finish, first, end);
      if (!days.length) continue;
      if (booking.cancelled && offered && !showCancelled) continue;
      if (booking.cancelled) cancelled++;
      else count++;
      for (const date of days) {
        const items = entries.get(date) ?? [];
        items.push(this.#booking(booking, start, finish));
        entries.set(date, items);
      }
    }
    const month = monthName.format(first);
    this.#status.textContent =
      error ??
      `${String(count)} booking${count === 1 ? "" : "s"} in ${month}` +
        (cancelled ? `, ${String(cancelled)} cancelled` : "") +
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
