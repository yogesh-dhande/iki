// The result's own snapshot supplies the measurements and fit. A later edit to a
// reading must never silently change the curve that a reviewer signed.
const STYLE = `
  :host {
    display: block; min-width: 0; padding: .75rem; color-scheme: light dark;
    --text: light-dark(#1d1f21,#e8e9eb); --surface: light-dark(#fff,#1a1b1e);
    --muted: light-dark(#5d6064,#a3a6ab); --border: light-dark(#e4e4df,#2d2f33);
    --accent: light-dark(#0b6e5f,#3fb8ad); --warning: light-dark(#8a5300,#f2b75a);
    font: 14px/1.45 "Inter Variable", system-ui, sans-serif;
    color: var(--text); background: var(--surface); overflow-wrap: anywhere;
  }
  h2 { margin: 0; font-size: 1.125rem; } h3 { margin: 1rem 0 .4rem; font-size: 1rem; }
  p, figure, dl { margin: .25rem 0 .75rem; }
  .endpoint { font-size: 1.5rem; font-weight: 650; font-variant-numeric: tabular-nums; margin: 0; }
  figcaption, .muted { color: var(--muted); }
  svg { width: 100%; max-width: 560px; height: auto; display: block; overflow: visible; }
  svg text { fill: var(--text); font: 15px system-ui, sans-serif; }
  .grid { stroke: var(--border); } .axis { stroke: var(--muted); }
  .curve { stroke: var(--accent); stroke-width: 3; fill: none; }
  .included { stroke: var(--surface); fill: var(--text); }
  .excluded { stroke: var(--warning); stroke-width: 2.5; fill: none; }
  dl { display: grid; grid-template-columns: minmax(6rem,auto) 1fr; gap: .25rem 1rem; }
  dt { color: var(--muted); } dd { margin: 0; }
  details { margin-top: .75rem; } summary { width: fit-content; cursor: pointer; font-weight: 600; }
  summary:focus-visible, .scroll:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .scroll { overflow-x: auto; margin-top: .25rem; }
  table { width: 100%; border-collapse: collapse; font-size: .875rem; }
  th, td { padding: .35rem .55rem; text-align: left; vertical-align: top; border-bottom: 1px solid var(--border); }
  th:first-child, td:first-child { padding-left: 0; } th[scope=col] { white-space: nowrap; }
  .number { text-align: right; font-variant-numeric: tabular-nums; }
`;

function node(tag, text, attributes = {}) {
  const out = document.createElement(tag);
  if (text !== undefined) out.textContent = text;
  for (const [name, value] of Object.entries(attributes)) out.setAttribute(name, value);
  return out;
}

function svg(tag, attributes, text) {
  const out = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [name, value] of Object.entries(attributes)) out.setAttribute(name, value);
  if (text !== undefined) out.textContent = text;
  return out;
}

function number(value, digits = 6) {
  if (!Number.isFinite(value)) return "—";
  const magnitude = Math.abs(value);
  if (magnitude > 0 && (magnitude < 0.0001 || magnitude >= 1e7)) {
    return Number(value.toPrecision(digits)).toExponential();
  }
  return value.toLocaleString("en-US", { maximumSignificantDigits: digits });
}

// Ritz et al., PLoS ONE (2015), eqs. 2–3, https://doi.org/10.1371/journal.pone.0146021.
// The stored Hill slope uses the increasing-positive sign convention (−b in eq. 2).
function responseAt(logDose, fit) {
  const exponent = fit.hill_slope * (Math.log(fit.endpoint_value) - logDose);
  return fit.bottom + (fit.top - fit.bottom) / (1 + Math.exp(exponent));
}

// The methods whose recorded parameters are this four-parameter logistic. A lab's own
// method may name its parameters alike and mean another curve, so it isn't drawn.
const DRAWN_METHODS = new Set(["4PL-OLS/1"]);

function hasParameters(fit) {
  return (
    fit.fit_status === "fitted" &&
    fit.endpoint_value > 0 &&
    [fit.endpoint_value, fit.bottom, fit.top, fit.hill_slope].every(Number.isFinite)
  );
}

function hasCurve(fit) {
  return DRAWN_METHODS.has(fit.fit_version) && hasParameters(fit);
}

function graph(fit, points) {
  // A logarithmic dose axis has no position for a zero-dose control. Keep such
  // readings in the table, and tell the reader how many the plot can show.
  const visible = points.filter(
    (p) => Number.isFinite(p.dose) && p.dose > 0 && Number.isFinite(p.response),
  );
  if (!visible.length) return null;
  const logDoses = visible.map((p) => Math.log(p.dose));
  let low = Math.min(...logDoses);
  let high = Math.max(...logDoses);
  if (low === high) {
    low -= 0.5;
    high += 0.5;
  }
  const responses = visible.map((p) => p.response);
  const curve = hasCurve(fit);
  if (curve) responses.push(responseAt(low, fit), responseAt(high, fit));
  const min = Math.min(...responses);
  const max = Math.max(...responses);
  const padding = (max - min || Math.abs(max) || 1) * 0.07;
  const floor = min - padding;
  const ceiling = max + padding;
  // Fixed coordinates in a scalable viewBox leave room for numerical ticks on both axes.
  const left = 78,
    right = 500,
    top = 18,
    bottom = 180;
  const x = (logDose) => left + ((right - left) * (logDose - low)) / (high - low);
  const y = (response) => bottom - ((bottom - top) * (response - floor)) / (ceiling - floor);
  const drawing = svg("svg", {
    viewBox: "0 0 560 240",
    role: "img",
    "aria-label": "Concentration–response plot",
  });
  for (const fraction of [0, 0.5, 1]) {
    const logDose = low + fraction * (high - low);
    const level = min + fraction * (max - min);
    drawing.append(
      svg("line", { class: "grid", x1: x(logDose), x2: x(logDose), y1: top, y2: bottom }),
      svg(
        "text",
        { x: x(logDose), y: bottom + 21, "text-anchor": "middle" },
        number(Math.exp(logDose), 3),
      ),
    );
    if (fraction === 0 || max > min) {
      drawing.append(
        svg("line", { class: "grid", x1: left, x2: right, y1: y(level), y2: y(level) }),
        svg("text", { x: left - 8, y: y(level) + 4, "text-anchor": "end" }, number(level, 3)),
      );
    }
  }
  drawing.append(
    svg("line", { class: "axis", x1: left, x2: right, y1: bottom, y2: bottom }),
    svg(
      "text",
      { x: (left + right) / 2, y: bottom + 48, "text-anchor": "middle" },
      `Concentration (${fit.concentration_unit}, log scale)`,
    ),
  );
  if (curve) {
    const coords = Array.from({ length: 129 }, (_, i) => {
      const logDose = low + ((high - low) * i) / 128;
      return `${x(logDose)},${y(responseAt(logDose, fit))}`;
    });
    drawing.append(svg("polyline", { class: "curve", points: coords.join(" ") }));
  }
  for (const point of visible) {
    const cx = x(Math.log(point.dose)),
      cy = y(point.response);
    drawing.append(
      point.exclusion_reason
        ? svg("path", { class: "excluded", d: `M${cx - 5} ${cy - 5}l10 10m0 -10l-10 10` })
        : svg("circle", { class: "included", cx, cy, r: 4 }),
    );
  }
  const figure = node("figure");
  figure.append(node("p", `Response (${fit.response_unit}) · ${fit.channel}`), drawing);
  const key = ["Dots show included readings."];
  if (visible.some((p) => p.exclusion_reason)) key.push("Crosses show exclusions.");
  if (curve) key.push("The line shows the recorded four-parameter fit.");
  else if (hasParameters(fit))
    key.push(`No line is drawn: this view draws only ${[...DRAWN_METHODS].join(", ")} fits.`);
  if (visible.length !== points.length)
    key.push(`${visible.length} of ${points.length} readings have plottable positive doses.`);
  figure.append(node("figcaption", key.join(" ")));
  return figure;
}

function measurements(fit, points) {
  const table = node("table", undefined, { "aria-label": "Source measurements" });
  const header = table.createTHead().insertRow();
  const headings = [
    "Well",
    "Reading",
    "Replicate",
    `Dose (${fit.concentration_unit})`,
    `Response (${fit.response_unit})`,
    "Fit inclusion",
  ];
  for (const heading of headings) header.append(node("th", heading, { scope: "col" }));
  const body = table.createTBody();
  for (const p of points) {
    const row = body.insertRow();
    row.append(
      node("th", p.well, { scope: "row" }),
      node("td", `${p.reading_hrid} · revision ${p.revision}`),
      node("td", p.replicate, { class: "number" }),
      node("td", number(p.dose), { class: "number" }),
      node("td", number(p.response), { class: "number" }),
      node(
        "td",
        p.exclusion_reason ? `Excluded by ${p.excluded_by} — ${p.exclusion_reason}` : "Included",
      ),
    );
  }
  const scroll = node("div", undefined, {
    class: "scroll",
    tabindex: "0",
    role: "region",
    "aria-label": "Measurement table",
  });
  scroll.append(table);
  const details = node("details");
  details.append(node("summary", `Source measurements (${points.length})`), scroll);
  return details;
}

class DoseResponse extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
  }

  set entity(entity) {
    const fit = entity?.values ?? {};
    const out = node("section", undefined, { "aria-label": "Dose response" });
    out.append(node("h2", fit.endpoint_kind ?? "Dose response"));
    if (!fit.fit_status) {
      out.append(node("p", "No fit has been recorded.", { role: "status" }));
      this.shadowRoot.replaceChildren(node("style", STYLE), out);
      return;
    }
    if (fit.fit_status === "fitted" && fit.endpoint_value > 0) {
      const qualifier = {
        within_range: "Within tested range",
        above_range: "Above tested range",
        below_range: "Below tested range",
      }[fit.qualifier];
      out.append(
        node("p", `${number(fit.endpoint_value)} ${fit.concentration_unit}`, { class: "endpoint" }),
      );
      if (qualifier) out.append(node("p", qualifier));
      if (Number.isFinite(fit.ci_low) && Number.isFinite(fit.ci_high)) {
        out.append(
          node(
            "p",
            `95% confidence interval ${number(fit.ci_low)}–${number(fit.ci_high)} ${fit.concentration_unit}`,
          ),
        );
      }
    } else {
      out.append(
        node(
          "p",
          fit.fit_reason ? `Endpoint unavailable. ${fit.fit_reason}` : "Endpoint unavailable.",
          { role: "status" },
        ),
      );
    }
    const points = fit.points ?? [];
    const chart = graph(fit, points);
    if (chart) out.append(chart);
    const params = node("dl");
    const fields = [
      ["Fit status", fit.fit_status],
      ["Method", fit.fit_version],
      ["Normalization", fit.normalization],
    ];
    if (hasParameters(fit))
      fields.push(
        ["Bottom", number(fit.bottom)],
        ["Top", number(fit.top)],
        ["Hill slope", number(fit.hill_slope)],
        ["Confidence method", fit.ci_method],
      );
    for (const [label, value] of fields) {
      if (value) params.append(node("dt", label), node("dd", value));
    }
    out.append(node("h3", "Fit details"), params, measurements(fit, points));
    this.shadowRoot.replaceChildren(node("style", STYLE), out);
  }
}

customElements.define("assay-dose-response", DoseResponse);
