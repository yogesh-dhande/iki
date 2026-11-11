/**
 * The entity grid's search box, filters and sortable headings.
 *
 * Filters apply as people type, once each one is complete and makes sense:
 * a row still being filled in is left out, and one that can't be searched
 * for says why. What applies is kept in the page's address.
 */

import { useEffect, useId, useRef, useState } from "react";

import type { EntityType, FieldDef } from "../entities";
import { FieldInput } from "../entity-inputs";
import { emptyDraft } from "../entity-values";
import {
  MAX_FILTERS,
  NO_RULES,
  newDraft,
  operators,
  takesValue,
  toDrafts,
  toFilter,
  valueField,
  type Filter,
  type FilterDraft,
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
    <div className="field">
      <label htmlFor={id}>Search</label>
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

export function FilterPanel({
  id,
  type,
  types,
  fields,
  applied,
  projectId,
  onApply,
}: {
  id: string;
  type: EntityType | undefined;
  types: EntityType[];
  fields: FieldDef[];
  applied: Filter[];
  projectId: string;
  onApply: (filters: Filter[]) => void;
}) {
  const [rows, setRows] = useState<FilterDraft[]>(() => toDrafts(applied, fields));
  const sent = useRef<Filter[]>(applied);
  // The address changed by other means (back, a link): show what it holds.
  useEffect(() => {
    if (!same(applied, sent.current)) {
      sent.current = applied;
      setRows(toDrafts(applied, fields));
    }
  }, [applied, fields]);

  const results = rows.map((row) => toFilter(row, fields));
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
  const fieldOptions = fields.map((f) => ({ value: f.name, label: f.label }));
  const first = fields[0];

  return (
    // Named directly: Firefox can crash computing a name through aria-labelledby while the
    // content changes (see the entity form's pickers).
    <section className="filters" id={id} aria-label="Filters">
      {type ? null : <p className="field-help">Choose a type to filter by its fields.</p>}
      {rows.length ? (
        <ol className="filter-rows">
          {rows.map((row, index) => {
            const field = fields.find((f) => f.name === row.field) ?? first;
            if (!field) return null;
            const n = String(index + 1);
            const result = results[index];
            const error = result && "error" in result ? result.error : undefined;
            const ops = operators(field);
            return (
              <li key={row.key} className="filter-row">
                <SelectField
                  compact
                  label={`Filter ${n}: field`}
                  value={field.name}
                  options={fieldOptions}
                  onChange={(name) => {
                    const chosen = fields.find((f) => f.name === name);
                    if (chosen) change(row.key, { ...newDraft(chosen), key: row.key });
                  }}
                />
                {ops.length ? (
                  <SelectField<Op>
                    compact
                    label={`Filter ${n}: condition`}
                    value={row.op}
                    options={ops}
                    onChange={(op) => {
                      change(row.key, {
                        op,
                        draft: takesValue(op) ? (row.draft ?? emptyDraft(valueField(field))) : null,
                      });
                    }}
                  />
                ) : null}
                {takesValue(row.op) ? (
                  <div className="filter-value">
                    <FieldInput
                      field={valueField(field)}
                      type={NO_RULES}
                      types={types}
                      draft={row.draft}
                      onChange={(draft) => {
                        change(row.key, { draft });
                      }}
                      error={error}
                      projectId={projectId}
                      itemLabel={`Filter ${n}: value`}
                    />
                  </div>
                ) : null}
                <Button
                  variant="quiet"
                  label={`Remove filter ${n}`}
                  onClick={() => {
                    setRows((current) => current.filter((r) => r.key !== row.key));
                  }}
                >
                  Remove
                </Button>
              </li>
            );
          })}
        </ol>
      ) : null}
      {first && rows.length < MAX_FILTERS ? (
        <button
          type="button"
          className={buttonClass("quiet")}
          onClick={() => {
            setRows((current) => [...current, newDraft(fields[1] ?? first)]);
          }}
        >
          Add a filter
        </button>
      ) : null}
    </section>
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
