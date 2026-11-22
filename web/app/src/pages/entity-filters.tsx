/**
 * The entity grid's search box, filters and sortable headings.
 *
 * Filters are chips (ADR 0176): each says what it holds back, and opens to change it.
 * They apply as people type, once each one is complete and makes sense: one still being
 * filled in is left out, and one that can't be searched for says why. What applies is
 * kept in the page's address.
 */

import { useEffect, useId, useRef, useState } from "react";
import {
  Button as AriaButton,
  Dialog,
  DialogTrigger,
  Header,
  Menu,
  MenuItem,
  MenuTrigger,
  Popover,
  MenuSection,
} from "react-aria-components";

import {
  formatDate,
  formatDateTime,
  type EntityType,
  type FieldDef,
  type Showing,
} from "../entities";
import { FieldInput } from "../entity-inputs";
import type { Picked } from "../entity-values";
import {
  emptyValue,
  MAX_FILTERS,
  NO_RULES,
  SOFTWARE,
  newDraft,
  operators,
  optionLabel,
  takesValue,
  toDrafts,
  toFilter,
  valueField,
  type Filter,
  type FilterDraft,
  type MakingDraft,
  type Op,
} from "../filters";
import { buttonClass, Button, SelectField } from "../ui";

/** How long typing pauses before the grid follows it, in milliseconds. */
const SETTLE = 350;

function useSettled<T>(value: T, onSettle: (value: T) => void, key: string): void {
  const latest = useRef(onSettle);
  useEffect(() => {
    latest.current = onSettle;
  });
  useEffect(() => {
    const timer = setTimeout(() => {
      latest.current(value);
    }, SETTLE);
    return () => {
      clearTimeout(timer);
    };
    // Settles when the value's key changes, not on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
}

/** Whether a key press is typing into something, which a page's shortcuts leave alone. */
function typingIn(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return (
    target.isContentEditable ||
    target.tagName === "INPUT" ||
    target.tagName === "TEXTAREA" ||
    target.tagName === "SELECT"
  );
}

/** A list's search: a box with a magnifier and its shortcut, "/", which focuses it from
 * anywhere on the page that isn't taking typing. */
export function SearchBox({
  value,
  onSearch,
  placeholder,
}: {
  value: string;
  onSearch: (q: string) => void;
  placeholder: string;
}) {
  const id = useId();
  const input = useRef<HTMLInputElement>(null);
  const [text, setText] = useState(value);
  useSettled(
    text,
    (q) => {
      if (q.trim() !== value.trim()) onSearch(q.trim());
    },
    text,
  );
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey) return;
      if (event.defaultPrevented || typingIn(event.target)) return;
      // Not while a dialog or menu is open over the page: its keys are its own.
      if (document.querySelector("[role=dialog], [role=alertdialog], [role=menu]")) return;
      event.preventDefault();
      input.current?.focus();
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
    };
  }, []);
  return (
    <div className="list-search">
      <label htmlFor={id} className="visually-hidden">
        Search
      </label>
      <input
        ref={input}
        id={id}
        type="search"
        autoComplete="off"
        spellCheck={false}
        maxLength={200}
        placeholder={placeholder}
        aria-keyshortcuts="/"
        value={text}
        onChange={(event) => {
          setText(event.target.value);
        }}
      />
      <kbd className="list-search-key" aria-hidden="true">
        /
      </kbd>
    </div>
  );
}

/** A choice in a list's toolbar, drawn as its buttons are: its label inside, then what's
 * chosen, then a chevron. A native select, so it works as one with every input. */
export function ToolbarSelect<T extends string>({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: T;
  onChange: (value: T) => void;
  options: readonly { value: T; label: string }[];
}) {
  const id = useId();
  return (
    <div className="toolbar-select">
      <label htmlFor={id}>{label}</label>
      <select
        id={id}
        value={value}
        onChange={(event) => {
          onChange(event.target.value as T);
        }}
      >
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </div>
  );
}

const TOOL_ICONS = {
  filter: ["M4 5h16l-6 8v5l-4 2v-7z"],
  sort: ["M7 4v16M3 16l4 4 4-4M17 20V4M13 8l4-4 4 4"],
  columns: [
    "M4.5 4h15A1.5 1.5 0 0 1 21 5.5v13a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 18.5v-13A1.5 1.5 0 0 1 4.5 4z",
    "M9 4v16M15 4v16",
  ],
  download: ["M12 4v11M7 10l5 5 5-5M5 20h14"],
} as const;

/** A toolbar button's icon, in its text's color. */
export function ToolIcon({ name }: { name: keyof typeof TOOL_ICONS }) {
  return (
    <svg className="tool-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      {TOOL_ICONS[name].map((d) => (
        <path key={d} d={d} />
      ))}
    </svg>
  );
}

/** Which records a list shows by whether they're archived, as the Filter menu offers it. */
const SHOWING: { value: Showing; label: string; chip: string }[] = [
  { value: "current", label: "Current only", chip: "Current" },
  { value: "archived", label: "Archived only", chip: "Archived only" },
  { value: "all", label: "Current and archived", chip: "Current and archived" },
];

const same = (a: Filter[], b: Filter[]) => JSON.stringify(a) === JSON.stringify(b);

/** A field's operators in the menu of what to filter by: the first few, as words. */
function opsHint(field: FieldDef, ops: { label: string }[]): string {
  if (field.type === "making") return "software, how, who says so";
  return ops
    .slice(0, 3)
    .map((o) => o.label)
    .join(", ");
}

/** A filter's value as a chip says it: a state by its label, a quantity with its unit. */
function valueWords(field: FieldDef, value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "object" && !Array.isArray(value)) {
    const given = value as Record<string, unknown>;
    if ("unit" in given && "value" in given)
      return `${String(given["value"])} ${String(given["unit"])}`;
    if (field.type === "making") {
      const kind = KINDS.find((k) => k.value === given["kind"])?.label;
      const says = SAYS.find((k) => k.value === given["by"])?.label;
      return [given["software"], kind, says].filter(Boolean).join(", ");
    }
    return JSON.stringify(value);
  }
  if (typeof value === "boolean") return value ? "yes" : "no";
  const text = typeof value === "string" ? value : JSON.stringify(value);
  if (field.type === "enum" || field.type === "rule" || field.type === "state") {
    return optionLabel(field, text);
  }
  if (field.type === "quantity" && typeof value === "number") {
    return `${text} ${field.unit_symbol ?? field.unit ?? ""}`.trim();
  }
  if (field.type === "datetime") return formatDateTime(text);
  if (field.type === "date") return formatDate(text);
  return text;
}

/** What a filter says, as its chip reads: "Direction is forward", "GC content is at
 * least 40 %". One still being filled in ends in an ellipsis. */
export function filterWords(row: FilterDraft, fields: FieldDef[]): string {
  const field = fields.find((f) => f.name === row.field);
  if (!field) return "";
  const op = operators(field).find((o) => o.value === row.op)?.label ?? "";
  const said = [field.label, field.type === "making" && row.op === "has" ? "" : op]
    .filter(Boolean)
    .join(" ");
  if (!takesValue(row.op)) return said;
  const result = toFilter(row, fields);
  if (!result || !("filter" in result)) return `${said} …`;
  const value = valueWords(field, result.filter.value);
  return field.type === "making" ? `${field.label}: ${value}` : `${said} ${value}`;
}

/**
 * A grid's filters, as chips: each says what it holds back, opens to change it, and has a
 * button to remove it. "Filter" lists what can be filtered by, each with its conditions;
 * choosing one adds it, open to fill in.
 */
export function FilterBar({
  type,
  types,
  fields,
  applied,
  projectId,
  noun,
  onApply,
  show,
}: {
  type: EntityType | undefined;
  types: EntityType[];
  fields: FieldDef[];
  applied: Filter[];
  /** Whether archived records show, as a filter: a chip when it isn't the current ones. */
  show?: { value: Showing; onChange: (show: Showing) => void } | undefined;
  /** The project its pickers find people and files in: none across projects, where a
   * person is filtered by "is me" alone. */
  projectId: string | undefined;
  /** What it calls the records, for "Filter proteins by". */
  noun: string;
  onApply: (filters: Filter[]) => void;
}) {
  const offered = fields;
  const [rows, setRows] = useState<FilterDraft[]>(() => toDrafts(applied, offered));
  const [open, setOpen] = useState<number | null>(null);
  const sent = useRef<Filter[]>(applied);
  const address = JSON.stringify(applied);
  const seen = useRef(address);
  // The address changed by other means (back, a link): show what it holds. Only a change
  // counts: until the address catches up with what was just applied, it still holds the
  // old filters, and resetting to them would drop what's being edited.
  useEffect(() => {
    if (address === seen.current) return;
    seen.current = address;
    if (!same(applied, sent.current)) {
      sent.current = applied;
      setRows(toDrafts(applied, offered));
    }
    // Keyed by type, so the fields only change with the address.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [address]);

  const results = rows.map((row) => toFilter(row, offered));
  const ready = results.flatMap((r) => (r && "filter" in r ? [r.filter] : []));
  useSettled(
    ready,
    (filters) => {
      if (!same(filters, sent.current)) {
        sent.current = filters;
        onApply(filters);
      }
    },
    JSON.stringify(ready),
  );

  const change = (key: number, change: Partial<FilterDraft>) => {
    setRows((current) => current.map((r) => (r.key === key ? { ...r, ...change } : r)));
  };
  const remove = (key: number) => {
    setRows((current) => current.filter((r) => r.key !== key));
    if (open === key) setOpen(null);
  };
  const add = (name: string) => {
    const field = offered.find((f) => f.name === name);
    if (!field) return;
    const row = newDraft(field);
    setRows((current) => [...current, row]);
    setOpen(row.key);
  };

  return (
    // Named directly: Firefox can crash computing a name through aria-labelledby while the
    // content changes (see the entity form's pickers).
    <div className="filter-bar" role="group" aria-label="Filters">
      {rows.map((row, index) => {
        const field = offered.find((f) => f.name === row.field);
        if (!field) return null;
        const n = String(index + 1);
        const result = results[index];
        const error = result && "error" in result ? result.error : undefined;
        const words = filterWords(row, offered);
        return (
          <div key={row.key} className={error ? "filter-chip filter-chip-invalid" : "filter-chip"}>
            <DialogTrigger
              isOpen={open === row.key}
              onOpenChange={(isOpen) => {
                setOpen(isOpen ? row.key : null);
              }}
            >
              <AriaButton className="filter-chip-words" aria-label={`Filter ${n}: ${words}`}>
                {words}
              </AriaButton>
              <Popover className="filter-popover" placement="bottom start" offset={4}>
                <Dialog className="filter-editor" aria-label={`Filter ${n}: ${field.label}`}>
                  <FilterEditor
                    n={n}
                    field={field}
                    row={row}
                    types={types}
                    error={error}
                    projectId={projectId}
                    onChange={(update) => {
                      change(row.key, update);
                    }}
                  />
                  <div className="filter-editor-actions">
                    <Button
                      variant="quiet"
                      label={`Remove filter ${n}`}
                      onClick={() => {
                        remove(row.key);
                      }}
                    >
                      Remove
                    </Button>
                    <Button
                      onClick={() => {
                        setOpen(null);
                      }}
                    >
                      Done
                    </Button>
                  </div>
                </Dialog>
              </Popover>
            </DialogTrigger>
            <button
              type="button"
              className="filter-chip-remove"
              aria-label={`Remove filter ${n}: ${words}`}
              onClick={() => {
                remove(row.key);
              }}
            >
              <span aria-hidden="true">×</span>
            </button>
          </div>
        );
      })}
      {show && show.value !== "current" ? (
        <div className="filter-chip">
          <MenuTrigger>
            <AriaButton
              className="filter-chip-words"
              aria-label={`Showing: ${showing(show.value)}`}
            >
              {showing(show.value)}
            </AriaButton>
            <Popover className="menu-popover" placement="bottom start" offset={4}>
              <Menu
                className="menu"
                aria-label="Show"
                selectionMode="single"
                selectedKeys={[show.value]}
                onAction={(key) => {
                  show.onChange(String(key) as Showing);
                }}
              >
                {SHOWING.map((s) => (
                  <MenuItem key={s.value} id={s.value} className="menu-item">
                    {s.label}
                  </MenuItem>
                ))}
              </Menu>
            </Popover>
          </MenuTrigger>
          <button
            type="button"
            className="filter-chip-remove"
            aria-label={`Show current ones only, not ${showing(show.value).toLowerCase()}`}
            onClick={() => {
              show.onChange("current");
            }}
          >
            <span aria-hidden="true">×</span>
          </button>
        </div>
      ) : null}
      {rows.length < MAX_FILTERS || show ? (
        <MenuTrigger>
          <AriaButton className={buttonClass()}>
            <ToolIcon name="filter" />
            Filter
          </AriaButton>
          <Popover className="menu-popover" placement="bottom start" offset={4}>
            {/* Named by its button, as a menu opened from one is. */}
            <Menu
              className="menu"
              onAction={(key) => {
                const picked = String(key);
                if (picked.startsWith(SHOW_KEY)) {
                  show?.onChange(picked.slice(SHOW_KEY.length) as Showing);
                } else {
                  add(picked);
                }
              }}
            >
              <MenuSection>
                <Header className="menu-heading">
                  {type ? `Filter ${noun} by` : `Filter ${noun} by (pick a type for its fields)`}
                </Header>
                {rows.length < MAX_FILTERS
                  ? offered
                      .filter((f) => operators(f).length > 0)
                      .map((f) => (
                        <MenuItem
                          key={f.name}
                          id={f.name}
                          className="menu-item"
                          textValue={f.label}
                        >
                          <span>{f.label}</span>
                          <small className="menu-item-hint">{opsHint(f, operators(f))}</small>
                        </MenuItem>
                      ))
                  : null}
              </MenuSection>
              {show ? (
                <MenuSection>
                  <Header className="menu-heading">Archived</Header>
                  {SHOWING.filter((s) => s.value !== show.value).map((s) => (
                    <MenuItem
                      key={s.value}
                      id={`${SHOW_KEY}${s.value}`}
                      className="menu-item"
                      textValue={`Show ${s.label.toLowerCase()}`}
                    >
                      Show {s.label.toLowerCase()}
                    </MenuItem>
                  ))}
                </MenuSection>
              ) : null}
            </Menu>
          </Popover>
        </MenuTrigger>
      ) : null}
    </div>
  );
}

/** The Filter menu's keys for whether archived records show: apart from fields' names. */
const SHOW_KEY = "show:";

function showing(value: Showing): string {
  return SHOWING.find((s) => s.value === value)?.chip ?? value;
}

/** Whether a field names people (ADR 0122), who are picked from a project's. */
function namesPeople(field: FieldDef): boolean {
  return field.type === "person" || (field.type === "list" && field.items === "person");
}

/** A filter's condition and value, in its chip's popover. */
function FilterEditor({
  n,
  field,
  row,
  types,
  error,
  projectId,
  onChange,
}: {
  n: string;
  field: FieldDef;
  row: FilterDraft;
  types: EntityType[];
  error: string | undefined;
  projectId: string | undefined;
  onChange: (update: Partial<FilterDraft>) => void;
}) {
  // Across projects there's nobody to pick from: a person is "me", or set, or empty. One
  // from the address still shows, as its email address.
  const picking = Boolean(projectId) || !namesPeople(field);
  const ops = operators(field).filter((o) => picking || !takesValue(o.value) || o.value === row.op);
  const named = row.draft as Picked | null;
  return (
    <>
      <p className="filter-editor-title">{field.label}</p>
      {ops.length ? (
        <SelectField<Op>
          compact
          label={`Filter ${n}: condition`}
          value={row.op}
          options={ops}
          onChange={(op) => {
            onChange({ op, draft: takesValue(op) ? (row.draft ?? emptyValue(field)) : null });
          }}
        />
      ) : null}
      {takesValue(row.op) && field.type === "making" ? (
        <MakingInput
          n={n}
          projectId={projectId ?? ""}
          draft={(row.draft ?? emptyValue(field)) as MakingDraft}
          onChange={(draft) => {
            onChange({ draft });
          }}
        />
      ) : takesValue(row.op) && !picking ? (
        <p className="filter-value">
          {named?.hrid ?? ""}
          <span className="field-help">Pick a project to filter by someone else.</span>
        </p>
      ) : takesValue(row.op) ? (
        <div className="filter-value">
          <FieldInput
            field={valueField(field)}
            type={NO_RULES}
            types={types}
            draft={row.draft}
            onChange={(draft) => {
              onChange({ draft });
            }}
            error={error}
            projectId={projectId ?? ""}
            itemLabel={`Filter ${n}: value`}
          />
        </div>
      ) : null}
    </>
  );
}

const KINDS = [
  { value: "", label: "made in any way" },
  { value: "computational", label: "computed" },
  { value: "experimental", label: "measured" },
] as const;

const SAYS = [
  { value: "", label: "whoever says so" },
  { value: "iki", label: "as Iki recorded" },
  { value: "source", label: "as its source says" },
] as const;

/** How it was made (ADR 0106): with what software, in what way, and who says so, all of
 * one making. */
function MakingInput({
  n,
  projectId,
  draft,
  onChange,
}: {
  n: string;
  projectId: string;
  draft: MakingDraft;
  onChange: (draft: MakingDraft) => void;
}) {
  const [software, kind, by] = draft;
  return (
    <div className="filter-value filter-making">
      <FieldInput
        field={SOFTWARE}
        type={NO_RULES}
        types={[]}
        draft={software}
        onChange={(typed) => {
          onChange([typeof typed === "string" ? typed : "", kind, by]);
        }}
        error={undefined}
        projectId={projectId}
        itemLabel={`Filter ${n}: software`}
      />
      <SelectField
        compact
        label={`Filter ${n}: how`}
        value={kind}
        options={KINDS}
        onChange={(chosen) => {
          onChange([software, chosen, by]);
        }}
      />
      <SelectField
        compact
        label={`Filter ${n}: who says so`}
        value={by}
        options={SAYS}
        onChange={(chosen) => {
          onChange([software, kind, chosen]);
        }}
      />
    </div>
  );
}

export type Direction = "ascending" | "descending" | "none";

/** A column heading that sorts the grid: ascending, then descending, then as it was. */
export function SortHeading({
  label,
  direction,
  onSort,
  next: given,
}: {
  label: string;
  direction: Direction;
  onSort: (direction: Direction) => void;
  /** Where a click goes, when it isn't the next of the three: from the list's default
   * order, which "as it was" would leave as it is. */
  next?: Direction | undefined;
}) {
  const next: Direction =
    given ??
    (direction === "none" ? "ascending" : direction === "ascending" ? "descending" : "none");
  return (
    <button
      type="button"
      className="sort-heading"
      onClick={() => {
        onSort(next);
      }}
    >
      {label}
      <span className="sort-mark" aria-hidden="true">
        {direction === "ascending" ? "▲" : direction === "descending" ? "▼" : ""}
      </span>
    </button>
  );
}
