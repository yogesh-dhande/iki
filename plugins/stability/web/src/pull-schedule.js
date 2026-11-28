import * as iki from "@ikisci/component";

const STYLE = `
  :host {
    color-scheme: light dark;
    --text: light-dark(#1d1f21, #e8e9eb); --surface: light-dark(#fff, #1a1b1e);
    --muted: light-dark(#5d6064, #a3a6ab); --border: light-dark(#e4e4df, #2d2f33);
    --input-border: light-dark(#8d9095, #6c6f75); --focus: light-dark(#0b6e5f, #5cc8be);
    --warning: light-dark(#8a5300, #f2b75a); --success: light-dark(#116d3a, #6fd49a);
    display: block; padding: .625rem; overflow-wrap: anywhere;
    color: var(--text); background: var(--surface);
    font: .875rem/1.45 "Inter Variable", system-ui, sans-serif;
  }
  [hidden] { display: none !important; }
  h2, h3, p, ol, dl, dd { margin: 0; }
  h2 { font-size: 1rem; } h3 { font-size: .875rem; }
  ol { list-style: none; padding: 0; }
  li { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: .3rem .75rem;
    border-bottom: 1px solid var(--border); padding: .55rem 0; align-items: start; }
  dl { display: grid; grid-template-columns: auto minmax(0,1fr); gap: 0 .65rem; }
  dt, .muted { color: var(--muted); }
  .filter { display: flex; flex-wrap: wrap; align-items: end; gap: .45rem .65rem; margin: .55rem 0; }
  label { display: grid; gap: .15rem; min-width: 0; }
  button, select { font: inherit; min-height: 1.75rem; max-width: 100%; padding: .15rem .5rem;
    background: var(--surface); color: inherit; border: 1px solid var(--input-border);
    border-radius: 6px; cursor: pointer; }
  button:focus-visible, select:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
  button:disabled { cursor: default; color: var(--muted); }
  .done { color: var(--success); } .warning { color: var(--warning); }
  .more { margin-top: .55rem; }
`;
const SCHEDULE = "iki/stability.schedule_pulls";
// The planned amount's UCUM unit as it's written for people.
const UNITS = { uL: "µL", mL: "mL", ug: "µg", mg: "mg" };

const STATUSES = {
  upcoming: "Upcoming",
  open: "Window open",
  overdue: "Overdue",
  pulled: "Pulled",
  early: "Pulled early",
  late: "Pulled late",
  missed: "Missed",
};
const dateFormat = new Intl.DateTimeFormat(undefined, {
  year: "numeric",
  month: "short",
  day: "numeric",
  timeZone: "UTC",
});

function node(tag, ...children) {
  const element = document.createElement(tag);
  element.append(...children);
  return element;
}

function date(iso) {
  if (!iso) return "—";
  const day = iso.slice(0, 10);
  const element = node("time", dateFormat.format(new Date(`${day}T00:00:00Z`)));
  element.dateTime = day;
  return element;
}

function statusOf(values, today) {
  if (values.status === "missed") return "missed";
  if (values.status === "pulled") {
    const actual = values.pulled_on?.slice(0, 10);
    if (actual && actual < values.opens_on) return "early";
    if (actual && actual > values.closes_on) return "late";
    return "pulled";
  }
  if (today > values.closes_on) return "overdue";
  return today < values.opens_on ? "upcoming" : "open";
}

function options(select, choices) {
  const previous = select.value;
  select.replaceChildren(
    ...choices.map(([value, label]) => {
      const option = node("option", label);
      option.value = value;
      return option;
    }),
  );
  if (choices.some(([value]) => value === previous)) select.value = previous;
}

function button(label, onClick) {
  const element = node("button", label);
  element.type = "button";
  element.addEventListener("click", onClick);
  return element;
}

class PullSchedule extends HTMLElement {
  #data = { items: [], loading: true };
  #study;
  #runs = [];
  #condition = node("select");
  #status = node("select");
  #notice = node("p");
  #rows = node("ol");
  #schedule = button("Schedule pulls", () => {
    iki.run(this, SCHEDULE);
  });
  #more = button("Show more pulls", () => {
    iki.more(this);
  });

  constructor() {
    super();
    options(this.#status, [["", "All statuses"], ...Object.entries(STATUSES)]);
    this.#status.addEventListener("change", () => this.#draw());
    this.#condition.addEventListener("change", () => {
      const value = this.#condition.value;
      iki.refine(this, value ? { where: { field: "condition", op: "eq", value } } : null);
      this.#draw();
    });
    this.#notice.className = "muted";
    this.#notice.setAttribute("role", "status");
    this.#more.className = "more";
    this.#rows.setAttribute("role", "list");
    const filters = node(
      "div",
      node("label", "Condition", this.#condition),
      node("label", "Pull status", this.#status),
      this.#schedule,
    );
    filters.className = "filter";
    this.attachShadow({ mode: "open" }).append(
      node("style", STYLE),
      node("h2", "Pull schedule"),
      filters,
      this.#notice,
      this.#rows,
      this.#more,
    );
    this.#draw();
  }

  set entity(study) {
    this.#study = study;
    this.#conditions();
    this.#draw();
  }

  set entities(data) {
    this.#data = data;
    this.#conditions();
    this.#draw();
  }

  set place(place) {
    this.#runs = place?.runs ?? [];
    this.#draw();
  }

  #conditions() {
    const choices = new Map(
      this.#study?.values?.conditions?.map(({ code, label }) => [code, label]),
    );
    for (const { values } of this.#data.items) {
      choices.set(values.condition, values.condition_label);
    }
    options(this.#condition, [["", "All conditions"], ...choices]);
  }

  #row(item, status) {
    const values = item.values;
    const tube = item.refs?.[values.tube]?.hrid ?? "Unavailable";
    const replicate = values.replicate > 1 ? ` · Replicate ${values.replicate}` : "";
    const heading = node("h3", `${values.condition_label} · ${values.time_point}${replicate}`);
    const state = node("span", STATUSES[status]);
    if (["late", "early", "missed", "overdue"].includes(status)) state.className = "warning";
    if (status === "pulled") state.className = "done";
    const detail = node(
      "dl",
      node("dt", "Due"),
      node("dd", date(values.due_on)),
      node("dt", "Window"),
      node("dd", date(values.opens_on), " – ", date(values.closes_on)),
      node("dt", "Tube"),
      node("dd", tube),
    );
    if (values.amount != null) {
      const unit = UNITS[values.amount_unit] ?? values.amount_unit;
      detail.append(
        node("dt", values.status === "pulled" ? "Drawn" : "Planned amount"),
        node("dd", `${values.amount} ${unit}`),
      );
    }
    if (values.tests?.length) {
      detail.append(node("dt", "Tests"), node("dd", values.tests.join(", ")));
    }
    if (values.status === "pulled") {
      detail.append(node("dt", "Actual pull"), node("dd", date(values.pulled_on)));
      if (values.window_note)
        detail.append(node("dt", "Deviation"), node("dd", values.window_note));
    }
    const open = button(item.hrid, () => {
      iki.open(this, item.hrid);
    });
    open.setAttribute("aria-label", `Open pull ${item.hrid}`);
    const about = node("div", heading, state, detail);
    return node("li", about, open);
  }

  #draw() {
    const { items, loading, more, limited, error } = this.#data;
    const today = new Date().toISOString().slice(0, 10);
    const condition = this.#condition.value;
    const status = this.#status.value;
    const rows = items.flatMap((item) => {
      const found = statusOf(item.values, today);
      if ((condition && condition !== item.values.condition) || (status && status !== found))
        return [];
      return [this.#row(item, found)];
    });
    this.#rows.replaceChildren(...rows);
    this.#schedule.hidden =
      !this.#runs.includes(SCHEDULE) || Boolean(this.#study?.values?.scheduled);
    this.#more.hidden = !more || Boolean(error);
    this.#more.disabled = loading;
    this.#notice.textContent =
      error ||
      (loading
        ? "Loading pulls…"
        : limited
          ? `Showing ${rows.length} matching pulls from the first ${items.length}.`
          : more
            ? `Showing ${rows.length} matching pulls from ${items.length} loaded.`
            : rows.length
              ? ""
              : "No scheduled pulls match these filters.");
  }
}

customElements.define("stability-pull-schedule", PullSchedule);
