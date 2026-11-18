// A calendar of bookings, a month at a time (ADR 0117). Iki sets `place` (where it is),
// and `entities`: the bookings its place's search finds, as the person viewing it may
// read them, with the fields the manifest lists. It asks for a month with an
// `iki-refine` event, for the rest of a month with `iki-more`, and for a booking's page
// with `iki-open`. It runs in a sandboxed frame: it reaches nothing, and changes nothing.

const STYLE = `
  :host { display: block; color-scheme: light dark; color: CanvasText; font: 0.95rem system-ui, sans-serif; }
  .bar { display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center; margin-bottom: 0.5rem; }
  .bar p { margin: 0 0 0 auto; }
  button { font: inherit; color: inherit; background: Canvas; border: 1px solid GrayText; border-radius: 4px; padding: 0.25rem 0.6rem; cursor: pointer; }
  button:focus-visible { outline: 3px solid Highlight; outline-offset: 2px; }
  .scroll { overflow-x: auto; }
  table { width: 100%; min-width: 42rem; border-collapse: collapse; table-layout: fixed; }
  caption { font-weight: 700; font-size: 1.1rem; text-align: start; padding-bottom: 0.5rem; }
  th { font-weight: 600; padding: 0.25rem; text-align: start; }
  td { border: 1px solid GrayText; vertical-align: top; height: 5.5rem; padding: 0.25rem; }
  td.outside .day { opacity: 0.75; }
  td[aria-current] .day { font-weight: 700; text-decoration: underline; }
  ul { list-style: none; margin: 0.25rem 0 0; padding: 0; display: grid; gap: 0.25rem; }
  li button { width: 100%; text-align: start; font-size: 0.8rem; padding: 0.15rem 0.3rem; overflow-wrap: anywhere; }
`;

const DAYS = 7;
const time = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" });
const day = new Intl.DateTimeFormat(undefined, { weekday: "long", day: "numeric", month: "long" });
const monthName = new Intl.DateTimeFormat(undefined, { month: "long", year: "numeric" });
const weekday = (date, style) =>
  new Intl.DateTimeFormat(undefined, { weekday: style }).format(date);

/** The first of the month `date` is in, at midnight where the person is. */
function startOf(date) {
  return new Date(date.getFullYear(), date.getMonth(), 1);
}

function addMonths(date, n) {
  return new Date(date.getFullYear(), date.getMonth() + n, 1);
}

function sameDay(a, b) {
  return (
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate()
  );
}

class BookingCalendar extends HTMLElement {
  #month = startOf(new Date());
  #asked = false;
  #place = { slot: "", label: "Bookings" };
  #entities = { items: [], more: false, limited: false, loading: true, error: null };

  constructor() {
    super();
    this.attachShadow({ mode: "open" });
  }

  set place(place) {
    this.#place = place;
    // Its place gives it every booking it shows; it shows one month of them at a time.
    if (!this.#asked) this.#show(this.#month);
    this.#draw();
  }

  set entities(entities) {
    this.#entities = entities;
    // The rest of the month, a page at a time, up to what it may hold.
    if (entities.more && !entities.loading) this.#send("iki-more");
    this.#draw();
  }

  #send(name, detail) {
    this.dispatchEvent(new CustomEvent(name, { detail, bubbles: true }));
  }

  #show(month) {
    this.#month = month;
    this.#asked = true;
    this.#send("iki-refine", {
      where: {
        all: [
          { field: "starts_at", op: "gte", value: month.toISOString() },
          { field: "starts_at", op: "lt", value: addMonths(month, 1).toISOString() },
        ],
      },
      sort: { field: "starts_at", descending: false },
    });
  }

  #button(text, onClick, label) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = text;
    if (label) button.setAttribute("aria-label", label);
    button.addEventListener("click", onClick);
    return button;
  }

  #status() {
    const { items, more, limited, loading, error } = this.#entities;
    const status = document.createElement("p");
    status.setAttribute("role", "status");
    if (error) {
      status.textContent = error;
      return status;
    }
    const month = monthName.format(this.#month);
    const count = `${items.length} booking${items.length === 1 ? "" : "s"} in ${month}`;
    status.textContent =
      count +
      (loading || more ? ", finding more…" : "") +
      (limited ? `: the first ${items.length} it can show` : "");
    return status;
  }

  #booking(booking) {
    const { purpose, starts_at, ends_at, instrument } = booking.values;
    const what = booking.refs[instrument]?.title ?? "";
    const from = time.format(new Date(starts_at));
    const to = ends_at ? time.format(new Date(ends_at)) : "";
    const text = `${from}–${to} ${purpose ?? booking.title}${what ? ` (${what})` : ""}`;
    const item = document.createElement("li");
    item.append(
      this.#button(
        text,
        () => this.#send("iki-open", { entity: booking.hrid }),
        `Open ${booking.hrid}: ${text}`,
      ),
    );
    return item;
  }

  #table() {
    const table = document.createElement("table");
    const caption = document.createElement("caption");
    caption.textContent = `${this.#place.label}: ${monthName.format(this.#month)}`;
    table.append(caption);
    // Weeks start on Monday.
    const first = new Date(this.#month);
    first.setDate(1 - ((first.getDay() + 6) % DAYS));
    const head = document.createElement("thead");
    const names = document.createElement("tr");
    for (let i = 0; i < DAYS; i += 1) {
      const date = new Date(first.getFullYear(), first.getMonth(), first.getDate() + i);
      const th = document.createElement("th");
      th.scope = "col";
      th.abbr = weekday(date, "long");
      th.textContent = weekday(date, "short");
      names.append(th);
    }
    head.append(names);
    table.append(head);
    const body = document.createElement("tbody");
    const today = new Date();
    const next = addMonths(this.#month, 1);
    const items = [...this.#entities.items];
    for (let at = new Date(first); at < next;) {
      const week = document.createElement("tr");
      for (let i = 0; i < DAYS; i += 1) {
        const cell = document.createElement("td");
        if (at.getMonth() !== this.#month.getMonth()) cell.className = "outside";
        if (sameDay(at, today)) cell.setAttribute("aria-current", "date");
        const number = document.createElement("span");
        number.className = "day";
        number.textContent = String(at.getDate());
        number.setAttribute("aria-label", day.format(at));
        cell.append(number);
        const those = items.filter((b) => sameDay(new Date(b.values.starts_at), at));
        if (those.length) {
          const list = document.createElement("ul");
          for (const booking of those) list.append(this.#booking(booking));
          cell.append(list);
        }
        week.append(cell);
        at = new Date(at.getFullYear(), at.getMonth(), at.getDate() + 1);
      }
      body.append(week);
    }
    table.append(body);
    const scroll = document.createElement("div");
    scroll.className = "scroll";
    scroll.append(table);
    return scroll;
  }

  #draw() {
    const style = document.createElement("style");
    style.textContent = STYLE;
    const bar = document.createElement("div");
    bar.className = "bar";
    bar.append(
      this.#button("Previous month", () => this.#show(addMonths(this.#month, -1))),
      this.#button("This month", () => this.#show(startOf(new Date()))),
      this.#button("Next month", () => this.#show(addMonths(this.#month, 1))),
      this.#status(),
    );
    this.shadowRoot.replaceChildren(style, bar, this.#table());
  }
}

customElements.define("lims-booking-calendar", BookingCalendar);
