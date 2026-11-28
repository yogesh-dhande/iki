// The host supplies these records through their declared, permission-scoped searches.
// Opening a record and running an action both go through the host's normal pages.

import * as iki from "@ikisci/component";

const STYLE = `
  :host {
    display: block; min-width: 0; padding: .5rem; color-scheme: light dark;
    --text: light-dark(#1d1f21, #e8e9eb); --surface: light-dark(#fff, #1a1b1e);
    --muted: light-dark(#5d6064, #a3a6ab); --border: light-dark(#e4e4df, #2d2f33);
    --input-border: light-dark(#8d9095, #6c6f75); --focus: light-dark(#0b6e5f, #5cc8be);
    --warning: light-dark(#8a5300, #f2b75a); --warning-bg: light-dark(#fdf5e6, #2a2111);
    --error: light-dark(#b42318, #ff8f85); --error-bg: light-dark(#fdf0ef, #2b1715);
    --radius-sm: 6px;
    color: var(--text); background: var(--surface); overflow-wrap: anywhere;
    font: 14px/1.4 "Inter Variable", system-ui, sans-serif;
  }
  h2, h3, dl, dd, p, ul { margin: 0; }
  h2 { font-size: 1rem; }
  h3 { font-size: .925rem; }
  p, ul { margin-top: .4rem; }
  ul { padding: 0; list-style: none; }
  li { padding: .55rem 0; border-bottom: 1px solid var(--border); }
  dl { display: grid; grid-template-columns: fit-content(40%) minmax(0, 1fr); gap: .1rem .65rem; margin-top: .35rem; }
  dt { color: var(--muted); }
  time { font-variant-numeric: tabular-nums; }
  button {
    font: inherit; color: inherit; background: inherit; border: 1px solid var(--input-border);
    border-radius: var(--radius-sm); min-height: 28px; padding: .15rem .55rem; cursor: pointer;
  }
  button:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
  .row-head, .actions { display: flex; align-items: center; flex-wrap: wrap; gap: .35rem .55rem; }
  .row-head { justify-content: space-between; }
  .actions { margin-top: .55rem; }
  .warning, .danger { display: inline-block; border-radius: var(--radius-sm); padding: .1rem .35rem; }
  .warning { color: var(--warning); background: var(--warning-bg); }
  .danger { color: var(--error); background: var(--error-bg); }
`;

const LABELS = {
  calibration: "Calibration",
  maintenance: "Maintenance",
  in_tolerance: "In tolerance",
  out_of_tolerance: "Out of tolerance",
  completed: "Completed",
  requires_repair: "Requires repair",
  scheduled: "Scheduled",
  cancelled: "Cancelled",
  recorded: "Recorded",
  signed_off: "Signed off",
  submitted: "Submitted",
  reviewed: "Independently reviewed",
  released: "QA released",
  unaffected: "Unaffected",
  repeat_required: "Repeat required",
  invalidated: "Invalidated",
  in_service: "In service",
  out_of_service: "Out of service",
  retired: "Retired",
  due: "Due",
  calibrated: "Calibrated",
  failed: "Failed",
  not_required: "Not required",
};
const SIGNED = new Set(["signed_off", "released"]);
const DATE = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeZone: "UTC" });
const DATETIME = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
  timeZone: "UTC",
});

function node(tag, ...children) {
  const element = document.createElement(tag);
  element.append(...children);
  return element;
}

function date(value) {
  if (!value || !Number.isFinite(Date.parse(value))) return "Unrecorded";
  const dateOnly = value.length === 10;
  const time = node("time", (dateOnly ? DATE : DATETIME).format(new Date(value)));
  time.dateTime = value;
  if (!dateOnly) time.append(" UTC");
  return time;
}

function details(entries) {
  return node("dl", ...entries.flatMap(([label, value]) => [node("dt", label), node("dd", value)]));
}

function marked(text, tone) {
  const label = node("span", text);
  label.className = tone;
  return label;
}

function reference(item, field, revision) {
  const ref = item.refs?.[item.values[field]];
  const title = ref?.hrid ?? "Unavailable reference";
  return revision == null ? title : `${title} · revision ${revision}`;
}

function scheduledStatus(dueOn, now) {
  const today = now.toISOString().slice(0, 10);
  if (dueOn < today) return marked("Overdue", "danger");
  if (dueOn === today) return marked("Due today", "warning");
  return "Scheduled";
}

function serviceReadiness(work, kind, now) {
  const calibration = kind === "calibration";
  if (work?.outcome === "out_of_tolerance" || work?.outcome === "requires_repair") {
    return marked(LABELS[work.outcome], "danger");
  }
  if (work && !SIGNED.has(work.review)) return marked("Awaiting sign-off", "warning");
  if (work && now < Date.parse(work.valid_until)) {
    return node("span", calibration ? "Valid until " : "Next due ", date(work.valid_until));
  }
  // Maintenance whose procedure sets no interval is done when it's signed, not due again.
  if (work && !calibration && !work.valid_until) {
    return node("span", "Completed ", date(work.performed_at));
  }
  return marked(calibration ? "Calibration due" : "Maintenance due", "warning");
}

class EquipmentRecords extends HTMLElement {
  #records = { items: [], loading: true };
  #available = [];
  subject;

  constructor() {
    super();
    this.attachShadow({ mode: "open" });
  }

  connectedCallback() {
    this.draw();
  }

  set entities(entities) {
    this.#records = entities;
    this.draw();
  }

  set entity(entity) {
    this.subject = entity;
    this.draw();
  }

  set place(place) {
    this.#available = place?.runs ?? [];
    this.draw();
  }

  button(text, ask, label) {
    const button = node("button", text);
    button.type = "button";
    if (label) button.setAttribute("aria-label", label);
    button.addEventListener("click", ask);
    return button;
  }

  row(item, title, entries) {
    const heading = node(
      "div",
      node("h3", title),
      this.button(item.hrid, () => iki.open(this, item.hrid), `Open ${item.hrid}`),
    );
    heading.className = "row-head";
    return node("li", heading, details(entries));
  }

  draw() {
    const { items, loading, error, limited, more } = this.#records;
    const now = new Date();
    const content = [node("style", STYLE), node("h2", this.heading)];
    if (!loading && !error) content.push(...this.summary(items, now, !more && !limited));
    if (items.length) {
      const list = node("ul", ...items.map((item) => this.buildRow(item, now)));
      list.setAttribute("role", "list"); // Keep list semantics with markers hidden in WebKit.
      content.push(list);
    }
    let message = error;
    if (!message && loading) message = "Loading records…";
    if (!message && limited) {
      message = `Showing the first ${items.length} ${items.length === 1 ? "record" : "records"}.`;
    }
    if (!message && !items.length) message = this.empty;
    if (message) {
      const status = node("p", message);
      status.setAttribute("role", "status");
      content.push(status);
    }

    const controls = [];
    if (more && !loading && !limited && !error) {
      controls.push(this.button("Show more records", () => iki.more(this)));
    }
    for (const [name, label] of this.actions) {
      const action = `iki/equipment.${name}`;
      if (this.#available.includes(action))
        controls.push(this.button(label, () => iki.run(this, action)));
    }
    if (controls.length) {
      const bar = node("div", ...controls);
      bar.className = "actions";
      content.push(bar);
    }
    this.shadowRoot.replaceChildren(...content);
  }

  summary() {
    return [];
  }
}

class EquipmentService extends EquipmentRecords {
  heading = "Equipment service";
  empty = "No service records are visible for this instrument.";
  actions = [
    ["schedule_service", "Schedule service"],
    ["record_instrument_use", "Record instrument use"],
    ["trace_failed_calibrations", "Trace failed calibrations"],
  ];

  summary(items, now, complete) {
    const { service, calibration: flag } = this.subject?.values ?? {};
    const entries = service ? [["Service state", LABELS[service] ?? service]] : [];
    if (flag) {
      const label = LABELS[flag] ?? flag;
      entries.push(["Calibration state", flag === "failed" ? marked(label, "danger") : label]);
    }
    if (complete) {
      const history = items
        .map(({ values }) => values)
        .filter((values) => Date.parse(values.performed_at) <= now)
        .sort((a, b) => Date.parse(b.performed_at) - Date.parse(a.performed_at));
      const calibration = history.find((values) => values.kind === "calibration");
      // An instrument outside the calibration program isn't due for one.
      if (calibration || flag !== "not_required") {
        entries.push(["Calibration evidence", serviceReadiness(calibration, "calibration", now)]);
      }
      const maintenance = history.find((values) => values.kind === "maintenance");
      if (maintenance) {
        entries.push(["Maintenance evidence", serviceReadiness(maintenance, "maintenance", now)]);
      }
    }
    return entries.length ? [details(entries)] : [];
  }

  buildRow(item, now) {
    const v = item.values;
    let review = LABELS[v.review] ?? "Unrecorded";
    if (v.review === "scheduled") {
      review = v.performed_at ? "Awaiting sign-off" : scheduledStatus(v.due_on, now);
    }
    const entries = [
      ["Scheduled date", date(v.due_on)],
      ["Review", review],
      ["Procedure", v.procedure ?? "Unrecorded"],
    ];
    if (v.performed_at) entries.push(["Performed", date(v.performed_at)]);
    if (v.performed_by)
      entries.push(["Performed by", item.people?.[v.performed_by]?.name ?? "Unavailable"]);
    if (v.outcome) entries.push(["Outcome", LABELS[v.outcome] ?? "Unrecorded"]);
    if (v.valid_until) {
      entries.push([v.kind === "calibration" ? "Valid until" : "Next due", date(v.valid_until)]);
    }
    if (v.suspect_since) entries.push(["Review measurements since", date(v.suspect_since)]);
    if (v.report) entries.push(["Report", "Attached"]);
    return this.row(item, LABELS[v.kind] ?? item.title, entries);
  }
}

class CalibrationImpacts extends EquipmentRecords {
  heading = "Calibration impact";
  empty = "No impact records are visible for this record.";
  actions = [];

  buildRow(item) {
    const v = item.values;
    let disposition = marked("Pending disposition", "warning");
    if (SIGNED.has(v.review) && LABELS[v.disposition]) {
      const tone = v.disposition === "invalidated" ? "danger" : "warning";
      disposition =
        v.disposition === "unaffected"
          ? LABELS[v.disposition]
          : marked(LABELS[v.disposition], tone);
    }
    const entries = [
      ["Measured", date(v.measurement_at)],
      ["Failed calibration", reference(item, "failure", v.failure_revision)],
    ];
    if (v.opened === "manually") {
      // No instrument use was recorded: say who opened the case, and why.
      const by = item.people?.[v.opened_by]?.name ?? "Unavailable";
      entries.push(
        ["Instrument evidence", marked("None recorded", "warning")],
        ["Opened manually by", by],
        ["Why", v.opened_reason ?? "Unrecorded"],
      );
    } else {
      entries.push(["Instrument evidence", reference(item, "use")]);
    }
    entries.push(["Disposition", disposition], ["Review", LABELS[v.review] ?? "Unrecorded"]);
    return this.row(item, reference(item, "result"), entries);
  }
}

class InstrumentUses extends EquipmentRecords {
  heading = "Instrument-use evidence";
  empty = "No instrument-use records are visible for this record.";
  actions = [];

  buildRow(item) {
    const v = item.values;
    return this.row(item, reference(item, "result", v.result_revision), [
      ["Instrument", reference(item, "instrument")],
      ["Measured", date(v.measurement_at)],
      ["Calibration", reference(item, "calibration", v.calibration_revision)],
    ]);
  }
}

customElements.define("iki-equipment-service", EquipmentService);
customElements.define("iki-calibration-impacts", CalibrationImpacts);
customElements.define("iki-instrument-uses", InstrumentUses);
