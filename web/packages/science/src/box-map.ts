import { slotsOf, rowLabel, type Shape, type Slot, type BoxOccupant } from "./box-positions.js";

export * from "./box-positions.js";

export type MapMode = "open" | "select" | "pick";

export interface BoxFinding {
  label: string;
  style?:
    "matched" | "missing" | "unexpected" | "wrong_slot" | "checked_out" | "empty" | "suggested";
}

export interface BoxMapData {
  label: string;
  shape: Shape;
  contents: readonly BoxOccupant[];
  mode?: MapMode;
  /** Coordinates in row:column format; selections are controlled by the caller. */
  selected?: readonly string[];
  marked?: { row: number; column: number };
  showFree?: boolean;
  findings?: Readonly<Record<string, BoxFinding>>;
  compact?: boolean;
}

export function slotKey(row: number, column: number): string {
  return `${String(row)}:${String(column)}`;
}

export function slotName(slot: Slot): string {
  const occupant = slot.occupant;
  if (!occupant) return `${slot.label}, empty`;
  if (!occupant.identifier) return `${slot.label}, taken by a record you can’t read`;
  const name = occupant.name && occupant.name !== occupant.identifier ? ` ${occupant.name}` : "";
  const count = occupant.count === undefined ? "" : `, holding ${String(occupant.count)}`;
  const out = occupant.checkedOutBy ? `, checked out by ${occupant.checkedOutBy}` : "";
  return `${slot.label}, ${occupant.identifier}${name}${count}${out}`;
}

function shortText(occupant: BoxOccupant): string {
  if (!occupant.identifier) return "";
  if (occupant.count !== undefined) return String(occupant.count);
  if (occupant.name && occupant.name !== occupant.identifier) return occupant.name;
  const digits = /(\d+)$/.exec(occupant.identifier);
  return digits ? String(Number(digits[1])) : occupant.identifier;
}

function boxNode<T extends "span" | "div">(
  tag: T,
  className: string,
  role?: string,
  text?: string,
) {
  const element = document.createElement(tag);
  element.className = className;
  if (role) element.setAttribute("role", role);
  if (text) element.textContent = text;
  return element;
}

/** The grid owns its tab stop. The host owns navigation, selection and storage mutations. */
export class BoxMapElement extends HTMLElement {
  private model: BoxMapData | undefined;
  private readonly grid = boxNode("div", "box-map", "grid");
  private readonly cells = new Map<string, HTMLElement>();
  private readonly slots = new Map<string, Slot>();
  private geometry = "";
  private active = "";

  constructor() {
    super();
    this.grid.addEventListener("focusin", (event) => {
      const key = this.cellKey(event);
      if (key) this.activate(key);
    });
    this.grid.addEventListener("click", (event) => {
      const key = this.cellKey(event);
      if (key) {
        this.activate(key);
        this.report(key);
      }
    });
    this.grid.addEventListener("keydown", (event) => {
      this.onKeyDown(event);
    });
  }

  get data(): BoxMapData | undefined {
    return this.model;
  }

  set data(model: BoxMapData | undefined) {
    this.model = model;
    if (this.isConnected) this.render();
  }

  connectedCallback(): void {
    this.render();
  }

  /** Move keyboard focus to an existing position, e.g. after scanning a label. */
  focusPosition(row: number, column: number): void {
    this.cells.get(slotKey(row, column))?.focus();
  }

  private cellKey(event: Event): string | undefined {
    return event.target instanceof Element
      ? event.target.closest<HTMLElement>('[role="gridcell"]')?.dataset["slot"]
      : undefined;
  }

  private activate(key: string): void {
    const last = this.cells.get(this.active);
    if (last) last.tabIndex = -1;
    const cell = this.cells.get(key);
    if (cell) cell.tabIndex = 0;
    this.active = key;
  }

  private report(key: string): void {
    const detail = this.slots.get(key);
    if (detail)
      this.dispatchEvent(
        new CustomEvent("slot-activate", { detail, bubbles: true, composed: true }),
      );
  }

  private onKeyDown(event: KeyboardEvent): void {
    const key = this.cellKey(event);
    const slot = key ? this.slots.get(key) : undefined;
    if (!slot || !this.model) return;
    const { shape } = this.model;
    let { row, column } = slot;
    switch (event.key) {
      case "ArrowRight":
        column++;
        break;
      case "ArrowLeft":
        column--;
        break;
      case "ArrowDown":
        row++;
        break;
      case "ArrowUp":
        row--;
        break;
      case "Home":
        column = 1;
        if (event.ctrlKey) row = 1;
        break;
      case "End":
        column = shape.columns;
        if (event.ctrlKey) row = shape.rows;
        break;
      case "PageUp":
        row = 1;
        break;
      case "PageDown":
        row = shape.rows;
        break;
      case "Enter":
      case " ":
        event.preventDefault();
        this.report(slotKey(row, column));
        return;
      default:
        return;
    }
    event.preventDefault();
    this.focusPosition(
      Math.min(Math.max(row, 1), shape.rows),
      Math.min(Math.max(column, 1), shape.columns),
    );
  }

  private buildGrid(rows: Slot[][], model: BoxMapData): void {
    const fragment = document.createDocumentFragment();
    const headed = model.shape.labels !== "number";
    this.cells.clear();
    if (headed) {
      const head = boxNode("div", "box-map-row box-map-head", "row");
      const corner = boxNode("span", "box-map-corner", "columnheader");
      corner.setAttribute("aria-label", "Row");
      head.append(corner);
      for (const slot of rows[0] ?? []) {
        head.append(boxNode("span", "box-map-colhead", "columnheader", String(slot.column)));
      }
      fragment.append(head);
    }
    for (const [index, slots] of rows.entries()) {
      const row = boxNode("div", "box-map-row", "row");
      if (headed)
        row.append(
          boxNode("span", "box-map-rowhead", "rowheader", rowLabel(model.shape, index + 1)),
        );
      for (const slot of slots) {
        const key = slotKey(slot.row, slot.column);
        const cell = boxNode("div", "box-map-slot", "gridcell");
        cell.dataset["slot"] = key;
        cell.tabIndex = -1;
        const position = boxNode("span", "box-map-pos");
        const what = boxNode("span", "box-map-what");
        position.setAttribute("aria-hidden", "true");
        what.setAttribute("aria-hidden", "true");
        cell.append(boxNode("span", "box-map-name"), position, what);
        this.cells.set(key, cell);
        row.append(cell);
      }
      fragment.append(row);
    }
    this.grid.replaceChildren(fragment);
    this.replaceChildren(this.grid);
    const start = model.marked ? slotKey(model.marked.row, model.marked.column) : "1:1";
    this.activate(
      this.cells.has(this.active) ? this.active : this.cells.has(start) ? start : "1:1",
    );
  }

  private render(): void {
    const model = this.model;
    if (!model) {
      this.replaceChildren();
      this.geometry = "";
      this.active = "";
      this.cells.clear();
      this.slots.clear();
      return;
    }
    const mode = model.mode ?? "open";
    this.classList.add("box-map-scroll");
    this.classList.toggle("box-map-compact", !!model.compact);
    const { shape } = model;
    const rows = slotsOf(shape, model.contents);
    const geometry = [shape.rows, shape.columns, shape.labels].join("/");
    if (geometry !== this.geometry) {
      this.buildGrid(rows, model);
      this.geometry = geometry;
    }
    this.grid.className = `box-map box-map-${mode}`;
    this.grid.setAttribute("aria-label", model.label);
    if (mode === "select") this.grid.setAttribute("aria-multiselectable", "true");
    else this.grid.removeAttribute("aria-multiselectable");
    this.slots.clear();
    const selected = new Set(model.selected);
    for (const slot of rows.flat()) {
      const key = slotKey(slot.row, slot.column);
      this.slots.set(key, slot);
      const cell = this.cells.get(key);
      if (!cell) continue;
      const occupant = slot.occupant;
      const marked = model.marked?.row === slot.row && model.marked.column === slot.column;
      const finding = model.findings?.[key];
      cell.className = "box-map-slot";
      cell.classList.add(
        occupant ? (occupant.identifier ? "box-map-taken" : "box-map-hidden") : "box-map-free",
      );
      cell.classList.toggle("box-map-selected", selected.has(key));
      cell.classList.toggle("box-map-out", !!occupant?.checkedOutBy);
      cell.classList.toggle("box-map-container", occupant?.count !== undefined);
      cell.classList.toggle("box-map-show-free", !!model.showFree && !occupant);
      cell.classList.toggle("box-map-marked", marked);
      if (finding?.style) cell.classList.add("box-map-finding", `box-map-${finding.style}`);
      if (mode === "select") cell.setAttribute("aria-selected", String(selected.has(key)));
      else cell.removeAttribute("aria-selected");
      if (marked) cell.setAttribute("aria-current", "location");
      else cell.removeAttribute("aria-current");
      cell.title = slotName(slot);
      const [name, position, what] = cell.children;
      if (name) name.textContent = finding ? `${cell.title}: ${finding.label}` : cell.title;
      if (position) position.textContent = slot.label;
      if (what) what.textContent = occupant ? shortText(occupant) : "";
    }
  }
}

if (!customElements.get("iki-box-map")) customElements.define("iki-box-map", BoxMapElement);

declare global {
  interface HTMLElementTagNameMap {
    "iki-box-map": BoxMapElement;
  }
}
