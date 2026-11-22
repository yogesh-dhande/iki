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

import { formatDate, formatDateTime, type EntityType, type FieldDef } from "../entities";
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
  const [text, setText] = useState(value);
  useSettled(
    text,
    (q) => {
      if (q.trim() !== value.trim()) onSearch(q.trim());
    },
    text,
  );
  return (
    <div className="search-box">
      <label htmlFor={id} className="visually-hidden">
        Search
      </label>
      <input
        id={id}
        type="search"
        autoComplete="off"
        spellCheck={false}
        maxLength={200}
        placeholder={placeholder}
        value={text}
        onChange={(event) => {
          setText(event.target.value);
        }}
      />
    </div>
  );
}

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
}: {
  type: EntityType | undefined;
  types: EntityType[];
  fields: FieldDef[];
  applied: Filter[];
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
      {rows.length < MAX_FILTERS ? (
        <MenuTrigger>
          <AriaButton className={buttonClass("quiet")}>
            <span aria-hidden="true">+ </span>Filter
          </AriaButton>
          <Popover className="menu-popover" placement="bottom start" offset={4}>
            {/* Named by its button, as a menu opened from one is. */}
            <Menu
              className="menu"
              onAction={(key) => {
                add(String(key));
              }}
            >
              <MenuSection>
                <Header className="menu-heading">
                  {type ? `Filter ${noun} by` : `Filter ${noun} by (pick a type for its fields)`}
                </Header>
                {offered
                  .filter((f) => operators(f).length > 0)
                  .map((f) => (
                    <MenuItem key={f.name} id={f.name} className="menu-item" textValue={f.label}>
                      <span>{f.label}</span>
                      <small className="menu-item-hint">{opsHint(f, operators(f))}</small>
                    </MenuItem>
                  ))}
              </MenuSection>
            </Menu>
          </Popover>
        </MenuTrigger>
      ) : null}
    </div>
  );
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
}: {
  label: string;
  direction: Direction;
  onSort: (direction: Direction) => void;
}) {
  const next: Direction =
    direction === "none" ? "ascending" : direction === "ascending" ? "descending" : "none";
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
