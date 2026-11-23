import {
  GEL_AGAROSE_RANGE,
  gelLanes,
  migrationAt,
  type GelBand,
  type GelLaneLayout,
  type VirtualGelData,
} from "./gel-layout.js";

export { GEL_AGAROSE_RANGE } from "./gel-layout.js";
export type { GelFragment, GelLane, VirtualGelData } from "./gel-layout.js";
export {
  GEL_LADDERS,
  type GelLadder,
  type GelLadderBand,
  type GelLadderId,
} from "./gel-ladders.js";

const format = new Intl.NumberFormat(undefined, { maximumFractionDigits: 3 });
const massFormat = new Intl.NumberFormat(undefined, { maximumSignificantDigits: 3 });

function node<T extends keyof HTMLElementTagNameMap>(tag: T, className = "", label = "") {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (label) element.textContent = label;
  return element;
}

function sizes(band: GelBand): string {
  const first = format.format(band.sizeBp);
  return band.companionSizeBp === undefined
    ? first
    : `${first} / ${format.format(band.companionSizeBp)}`;
}

function showSize(bp: number): string {
  return bp >= 1000 ? `${format.format(bp / 1000)} kb` : `${format.format(bp)} bp`;
}

function panel(lanes: GelLaneLayout[], agarosePercent: number): HTMLElement {
  const viewport = node("div", "virtual-gel__viewport");
  // Keep a scrollable region reachable by keyboard if several lanes overflow.
  viewport.tabIndex = 0;
  viewport.setAttribute("role", "region");
  viewport.setAttribute("aria-label", "Gel lanes");
  const grid = node("div", "virtual-gel__image");
  grid.setAttribute("role", "img");
  grid.setAttribute("aria-label", "Virtual gel band pattern");
  grid.style.setProperty("--gel-lanes", String(lanes.length));
  viewport.append(grid);
  const migration = migrationAt(agarosePercent);
  const rail = node("div", "virtual-gel__rail");
  const ruler = node("div", "virtual-gel__track");
  const markers = lanes.filter((lane) => lane.sourceUrl !== undefined);
  const marks = new Set(
    (markers.length ? markers : lanes).flatMap(({ bands }) => bands.map(({ sizeBp }) => sizeBp)),
  );
  let lastTick = -1;
  for (const bp of [...marks].sort((a, b) => b - a)) {
    const position = migration(bp);
    // Show only marks with enough room for a legible size label.
    if (position - lastTick < 0.085) continue;
    const tick = node("span", "virtual-gel__tick", showSize(bp));
    tick.style.setProperty("--band-position", `${String(100 * position)}%`);
    ruler.append(tick);
    lastTick = position;
  }
  rail.append(ruler);
  grid.append(node("div", "virtual-gel__lane-label", "Size"), rail);

  // NEB reports band masses for comparing bands of similar intensity and
  // size. Give calibrated lanes a shared display scale for their masses.
  // https://www.neb.com/products/n3232-1-kb-dna-ladder
  const maxMass = lanes.reduce(
    (max, lane) => lane.bands.reduce((m, band) => Math.max(m, band.massNg ?? 0), max),
    0,
  );
  for (const lane of lanes) {
    const laneLabel = node("div", "virtual-gel__lane-label", lane.label);
    const well = node("div", "virtual-gel__lane");
    const track = node("div", "virtual-gel__track");
    const maxRelative = lane.bands.reduce((m, band) => Math.max(m, band.weight), 0);
    for (const band of lane.bands) {
      if (!band.weight) continue;
      const position = migration(band.sizeBp);
      const other = migration(band.companionSizeBp ?? band.sizeBp);
      const intensity = band.weight / (band.massNg === undefined ? maxRelative : maxMass);
      const mark = node("span", "virtual-gel__band");
      mark.title = `${lane.label} · ${sizes(band)} bp`;
      mark.style.setProperty("--band-position", `${String(50 * (position + other))}%`);
      mark.style.setProperty("--band-intensity", String(intensity));
      track.append(mark);
    }
    well.append(track);
    grid.append(laneLabel, well);
  }
  return viewport;
}

function bandTable(lanes: GelLaneLayout[]): HTMLTableElement {
  const table = node("table", "virtual-gel__table");
  const caption = table.createCaption();
  caption.textContent = "Gel band sizes and masses";
  const heading = table.createTHead().insertRow();
  for (const title of ["Lane", "Size (bp)", "Count", "Mass (ng)"]) {
    const cell = node("th", "", title);
    cell.scope = "col";
    heading.append(cell);
  }
  for (const lane of lanes) {
    const body = table.createTBody();
    const first = body.insertRow();
    const label = node("th");
    label.scope = "rowgroup";
    label.rowSpan = Math.max(lane.bands.length, 1);
    if (lane.sourceUrl) {
      const source = node("a", "", lane.label);
      source.href = lane.sourceUrl;
      label.append(source);
    } else {
      label.textContent = lane.label;
    }
    first.append(label);
    if (!lane.bands.length) {
      const empty = first.insertCell();
      empty.colSpan = 3;
      empty.textContent = "No bands";
    }
    lane.bands.forEach((band, index) => {
      const row = index === 0 ? first : body.insertRow();
      row.insertCell().textContent = sizes(band);
      row.insertCell().textContent = format.format(band.count);
      row.insertCell().textContent =
        band.massNg === undefined ? "—" : massFormat.format(band.massNg);
    });
  }
  return table;
}

export class VirtualGelElement extends HTMLElement {
  private gel: VirtualGelData | undefined;

  get data(): VirtualGelData | undefined {
    return this.gel;
  }

  set data(data: VirtualGelData | undefined) {
    this.gel = data;
    if (this.isConnected) this.render();
  }

  connectedCallback(): void {
    this.render();
  }

  private render(): void {
    if (!this.gel) {
      this.replaceChildren();
      return;
    }
    const { agarosePercent, label } = this.gel;
    const figure = node("figure", "virtual-gel__figure");
    const caption = node("figcaption", "virtual-gel__caption");
    caption.append(
      node("span", "virtual-gel__title", label ?? `${format.format(agarosePercent)}% agarose`),
      node(
        "span",
        "virtual-gel__description",
        label
          ? `${format.format(agarosePercent)}% agarose · Relative migration of linear DNA`
          : "Relative migration of linear DNA",
      ),
    );
    figure.append(caption);
    if (
      !Number.isFinite(agarosePercent) ||
      agarosePercent < GEL_AGAROSE_RANGE.min ||
      agarosePercent > GEL_AGAROSE_RANGE.max
    ) {
      figure.append(node("p", "", "Enter an agarose percentage from 0.5 to 3."));
    } else if (!this.gel.lanes.length) {
      figure.append(node("p", "", "No gel lanes."));
    } else {
      const lanes = gelLanes(this.gel.lanes);
      const details = node("details", "virtual-gel__details");
      details.open = this.querySelector("details")?.open ?? false;
      details.append(node("summary", "", "Band details"), bandTable(lanes));
      if (lanes.some(({ bands }) => bands.some(({ massNg }) => massNg === undefined))) {
        details.append(
          node(
            "p",
            "",
            "Sample bands without a mass use relative brightness based on fragment size and count.",
          ),
        );
      }
      figure.append(panel(lanes, agarosePercent), details);
    }
    this.replaceChildren(figure);
  }
}

if (!customElements.get("iki-virtual-gel")) {
  customElements.define("iki-virtual-gel", VirtualGelElement);
}

declare global {
  interface HTMLElementTagNameMap {
    "iki-virtual-gel": VirtualGelElement;
  }
}
