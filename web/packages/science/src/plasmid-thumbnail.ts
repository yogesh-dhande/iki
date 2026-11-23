import { featureName, readFeatures, spans, type Alphabet, type Feature } from "./sequence.js";
import { viewerThemeUrl } from "./viewer-theme.js";
import { featureColor } from "./feature-colors.js";
export { featureColor } from "./feature-colors.js";
import type { SequenceFeature } from "./sequence-viewer.js";

export interface ThumbnailData {
  title: string;
  length: number;
  alphabet: Alphabet;
  circular: boolean;
  features: SequenceFeature[];
  /** When supplied, make the thumbnail a keyboard-accessible button. */
  openLabel?: string;
}

const SIZE = 180;
const MIDDLE = SIZE / 2;
const RADIUS = 68;
/** The most features the map's description names; it counts the rest. */
const NAMED = 6;

function point(position: number, length: number, radius: number): [number, number] {
  // From the top, clockwise: where a plasmid map starts its origin.
  const angle = (position / length) * 2 * Math.PI - Math.PI / 2;
  return [MIDDLE + radius * Math.cos(angle), MIDDLE + radius * Math.sin(angle)];
}

/** An arc along the ring from `from` to `to` (bases, from 1, inclusive). */
function arc(from: number, to: number, length: number, radius: number): string {
  if (from === 1 && to === length) {
    const top = MIDDLE - radius;
    return `M${String(MIDDLE)} ${String(top)} a${String(radius)} ${String(radius)} 0 1 1 0 ${String(2 * radius)} a${String(radius)} ${String(radius)} 0 1 1 0 ${String(-2 * radius)}`;
  }
  const [x1, y1] = point(from - 1, length, radius);
  const [x2, y2] = point(to, length, radius);
  const large = to - from + 1 > length / 2 ? 1 : 0;
  return `M${x1.toFixed(2)} ${y1.toFixed(2)} A${String(radius)} ${String(radius)} 0 ${String(large)} 1 ${x2.toFixed(2)} ${y2.toFixed(2)}`;
}

function described(map: ThumbnailData, features: Feature[]): string {
  const names = features.slice(0, NAMED).map(featureName);
  const more = features.length - names.length;
  const shape = map.circular ? "Circular" : "Linear";
  const bases = `${map.length.toLocaleString()} ${map.alphabet === "protein" ? "residues" : "bp"}`;
  const listed = more > 0 ? `${names.join(", ")} and ${String(more)} more` : names.join(", ");
  return `${shape} map of ${map.title}, ${bases}: ${listed}`;
}

function svg<K extends keyof SVGElementTagNameMap>(
  tag: K,
  attributes: Record<string, string | number> = {},
) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, String(value));
  return node;
}

export class PlasmidThumbnail extends HTMLElement {
  #value: ThumbnailData | null = null;
  get data() {
    return this.#value;
  }
  set data(data: ThumbnailData | null) {
    this.#value = data;
    if (this.isConnected) this.draw();
  }
  connectedCallback() {
    this.draw();
  }

  private draw() {
    const map = this.#value;
    if (!map || !Number.isSafeInteger(map.length) || map.length < 1) {
      this.replaceChildren();
      return;
    }
    const features = readFeatures(map.features, map.length);
    const picture = svg("svg", {
      viewBox: `0 0 ${String(SIZE)} ${map.circular ? String(SIZE) : "64"}`,
      width: SIZE,
      role: "img",
      "aria-label": described(map, features),
    });
    picture.style.maxWidth = "100%";
    const spine = map.circular
      ? svg("circle", { cx: MIDDLE, cy: MIDDLE, r: RADIUS })
      : svg("line", { x1: 4, y1: 12, x2: SIZE - 4, y2: 12 });
    spine.setAttribute("fill", "none");
    spine.setAttribute("stroke", "var(--border, GrayText)");
    spine.setAttribute("stroke-width", map.circular ? "6" : "3");
    picture.append(spine);
    const lanes = features.reduce((n, f) => Math.max(n, f.lane + 1), 1);
    const step = Math.min(9, 36 / lanes);
    for (const f of features) {
      const color = featureColor(f.type);
      for (const [from, to] of spans(f, map.length)) {
        const shape = map.circular
          ? svg("path", {
              d: arc(from, to, map.length, RADIUS - f.lane * step),
              fill: "none",
              stroke: color,
              "stroke-width": 8,
            })
          : svg("rect", {
              x: 4 + ((from - 1) / map.length) * (SIZE - 8),
              y: 20 + f.lane * step,
              width: Math.max(2, ((to - from + 1) / map.length) * (SIZE - 8)),
              height: Math.max(3, step - 2),
              rx: 1,
              fill: color,
            });
        const title = svg("title");
        title.textContent = featureName(f);
        shape.append(title);
        picture.append(shape);
      }
    }
    if (map.circular) {
      const label = svg("text", {
        x: MIDDLE,
        y: MIDDLE - 2,
        "text-anchor": "middle",
        "font-size": 12,
        "font-weight": 600,
        fill: "var(--text, CanvasText)",
      });
      label.textContent = map.title.length > 18 ? map.title.slice(0, 17) + "…" : map.title;
      const length = svg("text", {
        x: MIDDLE,
        y: MIDDLE + 14,
        "text-anchor": "middle",
        "font-size": 11,
        fill: "var(--muted, CanvasText)",
      });
      length.textContent = `${map.length.toLocaleString()} ${map.alphabet === "protein" ? "residues" : "bp"}`;
      picture.append(label, length);
    }
    if (map.openLabel) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "button";
      const theme = document.createElement("link");
      theme.rel = "stylesheet";
      theme.href = viewerThemeUrl;
      button.textContent = map.openLabel;
      button.addEventListener("click", () =>
        this.dispatchEvent(new CustomEvent("thumbnail-open", { bubbles: true, composed: true })),
      );
      this.replaceChildren(theme, picture, button);
    } else this.replaceChildren(picture);
  }
}

if (!customElements.get("iki-plasmid-thumbnail"))
  customElements.define("iki-plasmid-thumbnail", PlasmidThumbnail);
declare global {
  interface HTMLElementTagNameMap {
    "iki-plasmid-thumbnail": PlasmidThumbnail;
  }
}
