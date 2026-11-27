const STYLE = `
  :host {
    color-scheme: light dark;
    --text: light-dark(#1d1f21, #e8e9eb); --surface: light-dark(#fff, #1a1b1e);
    --muted: light-dark(#5d6064, #a3a6ab); --border: light-dark(#e4e4df, #2d2f33);
    --input-border: light-dark(#8d9095, #6c6f75); --focus: light-dark(#0b6e5f, #5cc8be);
    display: block; padding: .625rem; overflow-wrap: anywhere;
    color: var(--text); background: var(--surface);
    font: .875rem/1.45 "Inter Variable", system-ui, sans-serif;
  }
  [hidden] { display: none !important; }
  h2, h3, p, figure { margin: 0; }
  h2 { font-size: 1rem; } h3 { font-size: .95rem; }
  .muted { color: var(--muted); }
  .filter { display: grid; grid-template-columns: fit-content(35%) minmax(0, 1fr);
    gap: .45rem .65rem; margin: .55rem 0; max-width: 42rem; }
  label { display: grid; gap: .15rem; min-width: 0; }
  button, select { font: inherit; min-height: 1.75rem; min-width: 0; max-width: 100%;
    padding: .15rem .5rem; background: var(--surface); color: inherit;
    border: 1px solid var(--input-border); border-radius: 6px; cursor: pointer; }
  button:focus-visible, select:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
  button:disabled { cursor: default; color: var(--muted); }
  canvas { display: block; width: 100%; max-width: 36rem; height: auto; }
  table { border-collapse: collapse; width: 100%; max-width: 42rem; }
  th, td { padding: .35rem .375rem; border-bottom: 1px solid var(--border); text-align: left; }
  th[scope=row] { font-weight: 500; }
  .number { text-align: right; font-variant-numeric: tabular-nums; }
  .more { margin-top: .55rem; }
`;
const FINAL = new Set(["signed", "released"]);
const measurementNumber = new Intl.NumberFormat(undefined, { maximumSignificantDigits: 21 });
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

function date(iso) {
  const day = iso.slice(0, 10);
  const element = node("time", dateFormat.format(new Date(`${day}T00:00:00Z`)));
  element.dateTime = day;
  return element;
}

function groups(items) {
  const series = new Map();
  for (const item of items) {
    const values = item.values;
    if (!FINAL.has(values.review)) continue;
    const { condition, condition_label, test, method, unit, kind } = values;
    const key = JSON.stringify([condition, test, method, unit, kind]);
    if (!series.has(key)) {
      // A conforming test's series is its results over time; a number's, in its unit.
      const measure = kind === "conforms" ? "conforms" : unit;
      series.set(key, { label: `${test} · ${method} · ${measure} · ${condition_label}`, rows: [] });
    }
    series.get(key).rows.push(item);
  }
  return series;
}

// Plot each reported measurement at its scheduled time from the start, in days, so time
// points counted in months, weeks or days share one axis, each labelled by its code. A
// point is an observation; the table retains its exact value, actual pull date and link
// back to the signed result.
function plot(canvas, points) {
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  const drawingWidth = 560;
  const drawingHeight = 220;
  canvas.width = drawingWidth * 2;
  canvas.height = drawingHeight * 2;
  ctx.scale(2, 2);
  const dark = document.defaultView.matchMedia("(prefers-color-scheme: dark)").matches;
  const colors = dark
    ? { grid: "#3a3c41", text: "#e8e9eb", accent: "#3fb8ad" }
    : { grid: "#d6d6d0", text: "#1d1f21", accent: "#0b6e5f" };
  const largest = Math.max(1, ...points.map(({ elapsed_days }) => elapsed_days));
  const codes = new Map(points.map(({ elapsed_days, time_point }) => [elapsed_days, time_point]));
  const rawLow = Math.min(...points.map(({ value }) => value));
  const rawHigh = Math.max(...points.map(({ value }) => value));
  // Scale before taking differences so both ends of the finite float range fit the plot.
  const scale = Math.max(Math.abs(rawLow), Math.abs(rawHigh)) || 1;
  const low = rawLow / scale;
  const high = rawHigh / scale;
  const left = 90;
  const right = 540;
  const top = 18;
  const bottom = 159;
  const x = (days) => left + (days / largest) * (right - left);
  const y = (value) => {
    const fraction = high === low ? 0.5 : (value / scale - low) / (high - low);
    return bottom - (0.1 + 0.8 * fraction) * (bottom - top);
  };
  ctx.font = '17px "Inter Variable", system-ui, sans-serif';
  ctx.textBaseline = "middle";
  ctx.fillStyle = colors.text;
  ctx.textAlign = "right";
  ctx.strokeStyle = colors.grid;
  for (const value of new Set([rawLow, rawHigh])) {
    const lineY = y(value);
    ctx.beginPath();
    ctx.moveTo(left, lineY);
    ctx.lineTo(right, lineY);
    ctx.stroke();
    ctx.fillText(String(value), left - 8, lineY, left - 12);
  }
  ctx.strokeStyle = colors.text;
  ctx.beginPath();
  ctx.moveTo(left, top);
  ctx.lineTo(left, bottom);
  ctx.lineTo(right, bottom);
  ctx.stroke();
  ctx.textAlign = "center";
  const ticks = [...codes.keys()].sort((a, b) => a - b);
  for (const days of new Set([ticks[0], ticks[Math.floor((ticks.length - 1) / 2)], ticks.at(-1)])) {
    ctx.fillText(codes.get(days), x(days), bottom + 17);
  }
  ctx.fillText("Scheduled time point", (left + right) / 2, 207);
  for (const { elapsed_days, value } of points) {
    ctx.fillStyle = colors.accent;
    ctx.beginPath();
    ctx.arc(x(elapsed_days), y(value), 4.5, 0, Math.PI * 2);
    ctx.fill();
  }
}

class MeasurementTrend extends HTMLElement {
  #data = { items: [], loading: true };
  #study;
  #condition = node("select");
  #series = node("select");
  #notice = node("p");
  #result = node("div");
  #more = button("Show more reviewed results", () => this.#emit("iki-more"));

  constructor() {
    super();
    this.#condition.addEventListener("change", () => {
      const value = this.#condition.value;
      this.#emit("iki-refine", value ? { where: { field: "condition", op: "eq", value } } : null);
      this.#draw();
    });
    this.#series.addEventListener("change", () => this.#draw());
    this.#notice.className = "muted";
    this.#notice.setAttribute("role", "status");
    this.#more.className = "more";
    const filters = node(
      "div",
      node("label", "Condition", this.#condition),
      node("label", "Test series", this.#series),
    );
    filters.className = "filter";
    this.attachShadow({ mode: "open" }).append(
      node("style", STYLE),
      node("h2", "Reviewed results over time"),
      filters,
      this.#notice,
      this.#result,
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

  #emit(name, detail) {
    this.dispatchEvent(new CustomEvent(name, { detail }));
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

  #measurements(rows, label) {
    const [{ values }] = rows;
    const table = node("table");
    table.setAttribute("aria-label", label);
    const header = table.createTHead().insertRow();
    const conforms = values.kind === "conforms";
    const heading = conforms ? "Conforms" : `Value (${values.unit})`;
    for (const name of ["Time point", "Pulled", heading, "Result"]) {
      const th = node("th", name);
      th.scope = "col";
      if (name.startsWith("Value")) th.className = "number";
      header.append(th);
    }
    const body = table.createTBody();
    for (const item of rows) {
      const open = button(item.hrid, () => this.#emit("iki-open", { entity: item.hrid }));
      open.setAttribute("aria-label", `Open reviewed result ${item.hrid}`);
      const row = body.insertRow();
      const { time_point, replicate } = item.values;
      const month = node("th", replicate > 1 ? `${time_point} (${replicate})` : time_point);
      month.scope = "row";
      const value = node(
        "td",
        conforms
          ? item.values.result === "conforms"
            ? "Conforms"
            : "Does not conform"
          : measurementNumber.format(item.values.value),
      );
      value.className = "number";
      row.append(month, node("td", date(item.values.pulled_on)), value, node("td", open));
    }
    return table;
  }

  #draw() {
    const { items, loading, more, limited, error } = this.#data;
    const condition = this.#condition.value;
    const series = groups(
      items.filter(({ values }) => !condition || values.condition === condition),
    );
    const choices = [...series].map(([key, { label }]) => [key, label]);
    options(this.#series, choices.length ? choices : [["", "No reviewed results"]]);
    this.#series.disabled = !choices.length;
    const choice = series.get(this.#series.value);
    this.#result.replaceChildren();
    if (choice) {
      const { label, rows } = choice;
      const [{ values }] = rows;
      this.#result.append(node("h3", label));
      // Only numbers are plotted. A conforming test's results are its table.
      if (values.kind !== "conforms") {
        const canvas = node("canvas");
        canvas.setAttribute("role", "img");
        canvas.setAttribute(
          "aria-label",
          `${values.test} in ${values.unit} by scheduled time point`,
        );
        this.#result.append(node("figure", canvas));
        plot(
          canvas,
          rows.map(({ values }) => values),
        );
      }
      this.#result.append(this.#measurements(rows, label));
    }
    this.#more.hidden = !more || Boolean(error);
    this.#more.disabled = loading;
    this.#notice.textContent =
      error ||
      (loading
        ? "Loading reviewed results…"
        : limited
          ? `Showing series from the first ${items.length} results. Filter by condition to narrow the results.`
          : more
            ? `More results are available. Load them to see any additional test series and time points.`
            : choice
              ? ""
              : "No signed or QA-released results are visible for this condition.");
  }
}

customElements.define("stability-measurement-trend", MeasurementTrend);
