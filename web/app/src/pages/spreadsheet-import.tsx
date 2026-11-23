/**
 * Importing a spreadsheet into a project's records (ADR 0178), a step at a time:
 *
 * 1. The workbook: an XLSX workbook or a CSV file, uploaded to the project, and the type
 *    its rows become.
 * 2. Its sheet and rows: the header row, the first and last rows, rows to leave out.
 * 3. Its columns: each mapped to a field (Iki suggests them), and how the sheet writes
 *    numbers and dates, shown on its own cells. Nothing is guessed: a cell that reads two
 *    ways is flagged until the person says how the sheet is written.
 * 4. The preview: every row checked as it would be written, what each would do, and
 *    what's wrong with each by its column.
 * 5. The import: one run writes them all, or none, and links to its records.
 *
 * Each step is a component of its own, which a plugin's page can use too.
 */

import {
  useQueryClient,
  useSuspenseQuery,
  useInfiniteQuery,
  useQuery,
} from "@tanstack/react-query";
import { Link, useNavigate, useParams, useSearch } from "@tanstack/react-router";
import { useId, useRef, useState, type ReactNode, type SubmitEvent } from "react";

import { fieldErrors, problemOf, useApiMutation, type Project } from "../api";
import { formatSize, inSentence, typesQuery, type EntityType } from "../entities";
import { FilePicker } from "../file-picker";
import { UploadStatus } from "../file-upload";
import { usePageTitle } from "../layout";
import { pageReadSignal } from "../page-reads";
import {
  changeImport,
  columnName,
  columnsWith,
  commitImport,
  counted,
  DATE_ORDERS,
  DECIMALS,
  discardImport,
  importable,
  importQuery,
  OUTCOMES,
  parseRanges,
  plural,
  previewImport,
  rangesText,
  rowsQuery,
  saveMapping,
  savedMappings,
  SPREADSHEETS,
  startImport,
  STATES,
  WORKBOOK_ACCEPT,
  WORKBOOK_TYPES,
  type Outcome,
  type SaveMapping,
  type SheetChange,
  type SheetColumn,
  type SheetField,
  type SheetImport,
  type SheetLocale,
  type SheetLocaleChange,
  type SheetProblem,
} from "../spreadsheets";
import {
  Badge,
  Button,
  buttonClass,
  CheckboxField,
  ConfirmDialog,
  Loading,
  ProblemAlert,
  RadioField,
  SelectField,
  TextField,
  useMessages,
} from "../ui";
import { upload, uploadError, type Progress } from "../uploads";
import { useProject } from "./project-overview";

/** The types a sheet's rows can become: those whose records can be made. */
export function importableTypes(types: readonly EntityType[]): EntityType[] {
  return types
    .filter((t) => !t.abstract && !t.detached && t.name !== "iki/core.Entity")
    .sort((a, b) => a.label.localeCompare(b.label));
}

// --- The steps, as a list ------------------------------------------------------------------

export type Step = "workbook" | "rows" | "columns" | "preview" | "import";

const STEPS: { step: Step; label: string }[] = [
  { step: "workbook", label: "Workbook" },
  { step: "rows", label: "Sheet and rows" },
  { step: "columns", label: "Columns" },
  { step: "preview", label: "Preview" },
  { step: "import", label: "Import" },
];

/** Where the person is: the steps before it done, it current. */
export function ImportSteps({ current }: { current: Step }) {
  const at = STEPS.findIndex((s) => s.step === current);
  return (
    <ol className="steps sheet-steps" aria-label="Importing a spreadsheet">
      {STEPS.map((s, index) => (
        <li
          key={s.step}
          className={index === at ? "step-current" : index < at ? "step-done" : undefined}
          aria-current={index === at ? "step" : undefined}
        >
          <span className="step-number" aria-hidden="true">
            {index < at ? "✓" : index + 1}
          </span>
          {s.label}
          {index < at ? <span className="visually-hidden"> (done)</span> : null}
        </li>
      ))}
    </ol>
  );
}

/** A table wider than a phone, scrolled across: by keyboard too, so it can be focused,
 * named as its table is. */
function Scroll({
  label,
  className,
  children,
}: {
  label: string;
  className?: string;
  children: ReactNode;
}) {
  return (
    <div
      className={className ? `table-scroll ${className}` : "table-scroll"}
      // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- a scrollable region
      tabIndex={0}
      role="group"
      aria-label={label}
    >
      {children}
    </div>
  );
}

// --- 1. The workbook -----------------------------------------------------------------------

/** The largest workbook Iki reads (ADR 0178). */
export const WORKBOOK_MAX_SIZE = 100 * 1024 * 1024;

/**
 * The workbook, uploaded to the project or chosen from its Files (ADR 0177), and the type
 * its rows become; then starts importing it. Either way the workbook is a file of the
 * project, which each record it imports names. Its `onStarted` gets the import, once Iki
 * has it.
 */
export function ChooseWorkbook({
  project,
  types,
  type: given,
  onStarted,
}: {
  project: Project;
  types: readonly EntityType[];
  type?: string | undefined;
  onStarted: (made: SheetImport) => void;
}) {
  const id = useId();
  const choices = importableTypes(types);
  const [type, setType] = useState(given && choices.some((t) => t.name === given) ? given : "");
  const [file, setFile] = useState<File>();
  const [progress, setProgress] = useState<Progress>();
  const [error, setError] = useState<string>();
  const [typeError, setTypeError] = useState<string>();
  const [picking, setPicking] = useState(false);
  const [starting, setStarting] = useState<string>();
  const transfer = useRef<AbortController>(undefined);
  const input = useRef<HTMLInputElement>(null);

  /** Whether the rows' type is chosen: it's asked first, as the import needs it. */
  function typed(): boolean {
    if (type) return true;
    setTypeError("Choose what its rows are first.");
    return false;
  }

  async function send(chosen: File, acquiredAt = "") {
    if (!typed()) return;
    setFile(chosen);
    setError(undefined);
    const controller = new AbortController();
    transfer.current = controller;
    try {
      const kind = /\.(csv|tsv|txt)$/i.test(chosen.name) ? "text/csv" : WORKBOOK_TYPES[0];
      const uploaded = await upload(
        chosen,
        project.id,
        setProgress,
        controller.signal,
        kind,
        acquiredAt,
      );
      onStarted(await startImport(project.id, uploaded.id, type));
    } catch (caught) {
      if (!controller.signal.aborted) setError(uploadError(caught));
    } finally {
      setProgress(undefined);
      setFile(undefined);
    }
  }

  /** A workbook the project has already: imported as it is, not uploaded again. */
  async function pick(fileId: string, name: string) {
    if (!typed()) return;
    setError(undefined);
    setStarting(name);
    try {
      onStarted(await startImport(project.id, fileId, type));
    } catch (caught) {
      setError(problemOf(caught).detail || `Iki couldn’t start importing ${name}.`);
    } finally {
      setStarting(undefined);
    }
  }

  return (
    <section className="card sheet-card" aria-labelledby={`${id}-title`}>
      <h3 className="section-title" id={`${id}-title`}>
        The workbook
      </h3>
      <p>
        An Excel workbook (.xlsx) or a CSV file. Iki reads its cells’ values, never its formulas or
        macros, and keeps it in the project: each record it imports names it, and its sheet and row.
      </p>
      <SelectField
        label="Its rows are"
        value={type}
        onChange={(value) => {
          setType(value);
          setTypeError(undefined);
        }}
        error={typeError}
        options={[
          { value: "", label: "Choose a type…" },
          ...choices.map((t) => ({ value: t.name, label: t.label_plural })),
        ]}
      />
      {file && progress ? (
        <div className="upload">
          <p className="visually-hidden" role="status">
            {progress.phase === "checking" ? `Checking ${file.name}` : `Uploading ${file.name}`}
          </p>
          <p aria-hidden="true">
            <UploadStatus name={file.name} progress={progress} />
          </p>
          {progress.phase === "checking" ? (
            <progress aria-label={`Checking ${file.name}`} />
          ) : (
            <>
              <progress
                aria-label={`Uploading ${file.name}`}
                max={progress.total || 1}
                value={progress.done}
              />
              <Button
                variant="quiet"
                label={`Cancel uploading ${file.name}`}
                onClick={() => {
                  transfer.current?.abort();
                }}
              >
                Cancel
              </Button>
            </>
          )}
        </div>
      ) : (
        <>
          <div className={error ? "field field-invalid" : "field"}>
            <label htmlFor={`${id}-file`}>Upload a workbook</label>
            <input
              ref={input}
              id={`${id}-file`}
              type="file"
              accept={WORKBOOK_ACCEPT}
              disabled={starting !== undefined}
              aria-invalid={error ? "true" : undefined}
              aria-describedby={error ? `${id}-error` : `${id}-help`}
              onChange={(event) => {
                const chosen = event.target.files?.[0];
                event.target.value = "";
                if (chosen) void send(chosen);
              }}
            />
            {error ? (
              <p className="field-error" id={`${id}-error`}>
                {error}
              </p>
            ) : (
              <p className="field-help" id={`${id}-help`}>
                Up to 100 MiB, with up to a million rows a sheet. A workbook with a password, or an
                Excel 97–2003 one (.xls), needs saving as .xlsx first.
              </p>
            )}
          </div>
          <div className="field">
            <Button
              busy={starting !== undefined}
              busyText={`Starting ${starting ?? ""}…`}
              onClick={() => {
                if (typed()) setPicking(true);
              }}
            >
              Or choose one already in {project.name}
            </Button>
          </div>
        </>
      )}
      {/* The project's Files, held to workbooks: one already there is imported as it is. */}
      <FilePicker
        isOpen={picking}
        onClose={() => {
          setPicking(false);
        }}
        projectId={project.id}
        label="the import"
        takes={{ accepts: WORKBOOK_TYPES, maxSize: WORKBOOK_MAX_SIZE }}
        onChoose={([chosen]) => {
          if (chosen) void pick(chosen.id, chosen.name);
        }}
        onUpload={([chosen], acquiredAt) => {
          if (chosen) void send(chosen, acquiredAt);
        }}
      />
    </section>
  );
}

// --- 2. Its sheet and rows ----------------------------------------------------------------

/** A whole number, changed when the person leaves the box or presses Enter. */
function RowNumber({
  label,
  value,
  onCommit,
  help,
  error,
  optional = false,
  min = 1,
}: {
  label: string;
  value: number | null;
  onCommit: (value: number | null) => void;
  help?: string;
  error?: string | undefined;
  optional?: boolean;
  min?: number;
}) {
  const id = useId();
  const [text, setText] = useState(value == null ? "" : String(value));
  const [shown, setShown] = useState(value);
  // The import's value, when it changes: another choice may have moved it.
  if (shown !== value) {
    setShown(value);
    setText(value == null ? "" : String(value));
  }
  const commit = () => {
    const trimmed = text.trim();
    const next = trimmed === "" ? null : Number(trimmed);
    if (next === value || (next !== null && !Number.isInteger(next))) return;
    if (next === null && !optional) return;
    onCommit(next);
  };
  return (
    <div className={error ? "field field-invalid sheet-number" : "field sheet-number"}>
      <label htmlFor={id}>{label}</label>
      {error ? (
        <p className="field-error" id={`${id}-error`}>
          {error}
        </p>
      ) : null}
      <input
        id={id}
        type="number"
        inputMode="numeric"
        min={min}
        value={text}
        aria-invalid={error ? "true" : undefined}
        aria-describedby={[error ? `${id}-error` : "", help ? `${id}-help` : ""]
          .filter(Boolean)
          .join(" ")}
        onChange={(event) => {
          setText(event.target.value);
        }}
        onBlur={commit}
        onKeyDown={(event) => {
          if (event.key === "Enter") commit();
        }}
      />
      {help ? (
        <p className="field-help" id={`${id}-help`}>
          {help}
        </p>
      ) : null}
    </div>
  );
}

/** Rows to leave out, as people write them ("5-7, 12"), said when the person leaves the
 * box or presses Enter: not while they're typing "5-". */
function RowsToSkip({
  value,
  error,
  onCommit,
}: {
  value: readonly (readonly number[])[];
  error?: string | undefined;
  onCommit: (ranges: number[][]) => void;
}) {
  const id = useId();
  const said = rangesText(value);
  const [text, setText] = useState(said);
  const [shown, setShown] = useState(said);
  const [wrong, setWrong] = useState<string>();
  if (shown !== said) {
    setShown(said);
    setText(said);
  }
  const commit = () => {
    try {
      const ranges = parseRanges(text);
      setWrong(undefined);
      if (rangesText(ranges) !== said) onCommit(ranges);
    } catch (problem) {
      setWrong((problem as Error).message);
    }
  };
  const shownError = wrong ?? error;
  return (
    <div className={shownError ? "field field-invalid" : "field"}>
      <label htmlFor={id}>
        Rows to leave out<span className="optional"> (optional)</span>
      </label>
      {shownError ? (
        <p className="field-error" id={`${id}-error`}>
          {shownError}
        </p>
      ) : null}
      <input
        id={id}
        type="text"
        value={text}
        aria-invalid={shownError ? "true" : undefined}
        aria-describedby={`${shownError ? `${id}-error ` : ""}${id}-help`}
        onChange={(event) => {
          setText(event.target.value);
        }}
        onBlur={commit}
        onKeyDown={(event) => {
          if (event.key === "Enter") commit();
        }}
      />
      <p className="field-help" id={`${id}-help`}>
        Such as 5-7, 12: totals, notes, or rows someone struck through.
      </p>
    </div>
  );
}

/** The sheet to import, its header row, and the rows to import. */
export function ChooseRows({
  made,
  change,
  problem,
}: {
  made: SheetImport;
  change: (choice: SheetChange) => void;
  problem?: ReturnType<typeof problemOf> | undefined;
}) {
  const errors = fieldErrors(problem);
  const sheet = made.sheets.find((s) => s.name === made.sheet);
  const id = useId();
  return (
    <section className="card sheet-card" aria-labelledby={`${id}-title`}>
      <h3 className="section-title" id={`${id}-title`}>
        Sheet and rows
      </h3>
      {made.sheets.length > 1 ? (
        <SelectField
          label="Sheet"
          value={made.sheet}
          onChange={(value) => {
            change({ sheet: value });
          }}
          error={errors["sheet"]}
          options={made.sheets.map((s) => ({
            value: s.name,
            label: `${s.name}: ${plural(s.rows, "row", "rows")}${s.hidden ? " (hidden)" : ""}`,
          }))}
        />
      ) : (
        <p>
          {made.kind === "csv" ? "The file" : `Its one sheet, ${made.sheet},`} has{" "}
          {plural(sheet?.rows ?? 0, "row", "rows")} and{" "}
          {plural(sheet?.columns ?? 0, "column", "columns")}.
        </p>
      )}
      <div className="sheet-numbers">
        <RowNumber
          label="Header row"
          value={made.header_row}
          min={0}
          help="Its columns’ names. 0 when it has none."
          error={errors["header_row"]}
          onCommit={(value) => {
            change({ header_row: value ?? 0 });
          }}
        />
        <RowNumber
          label="First row"
          value={made.first_row}
          error={errors["first_row"]}
          onCommit={(value) => {
            if (value != null) change({ first_row: value });
          }}
        />
        <RowNumber
          label="Last row"
          value={made.last_row}
          optional
          help="Empty for the sheet’s last."
          error={errors["last_row"]}
          onCommit={(value) => {
            change({ last_row: value ?? 0 });
          }}
        />
      </div>
      <RowsToSkip
        value={made.skip}
        error={errors["skip"]}
        onCommit={(ranges) => {
          change({ skip: ranges });
        }}
      />
      <SheetSample made={made} />
    </section>
  );
}

/** The sheet's first rows, numbered as the sheet numbers them, the header's marked. */
export function SheetSample({ made }: { made: SheetImport }) {
  const id = useId();
  const width = Math.min(made.columns.length || 1, 12);
  const rows = made.sample.slice(0, 12);
  if (!rows.length) return <p className="empty">The sheet is empty.</p>;
  const skipped = (n: number) =>
    n < made.first_row ||
    (made.last_row != null && n > made.last_row) ||
    made.skip.some(([a = 0, b = 0]) => n >= a && n <= b);
  return (
    <Scroll className="sheet-sample" label="The sheet’s first rows">
      <table className="table">
        <caption id={id}>
          Its first rows
          {made.columns.length > width
            ? `, and the first ${String(width)} of its ${String(made.columns.length)} columns`
            : ""}
        </caption>
        <thead>
          <tr>
            <th scope="col">
              <span className="visually-hidden">Row</span>
            </th>
            {made.columns.slice(0, width).map((c) => (
              <th scope="col" key={c.index}>
                {c.letter}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => {
            const n = i + 1;
            const header = n === made.header_row;
            return (
              <tr
                key={n}
                className={header ? "sheet-header-row" : skipped(n) ? "sheet-skipped" : undefined}
              >
                <th scope="row">
                  {n}
                  {header ? <span className="visually-hidden"> (header)</span> : null}
                  {!header && skipped(n) ? (
                    <span className="visually-hidden"> (not imported)</span>
                  ) : null}
                </th>
                {Array.from({ length: width }, (_, c) => (
                  <td key={c}>{row[c] ?? ""}</td>
                ))}
              </tr>
            );
          })}
        </tbody>
      </table>
    </Scroll>
  );
}

// --- 3. Its columns -----------------------------------------------------------------------

function fieldLabel(f: SheetField): string {
  return `${f.label}${f.required ? " (required)" : ""}`;
}

/** Each column's field, and how its cells read. */
export function MapColumns({
  made,
  change,
  problem,
  settingsPending = false,
}: {
  settingsPending?: boolean;
  made: SheetImport;
  change: (choice: SheetChange) => void;
  problem?: ReturnType<typeof problemOf> | undefined;
}) {
  const id = useId();
  const errors = fieldErrors(problem);
  const fields = new Map(made.fields.map((f) => [f.name, f]));
  const mappable = made.fields.filter((f) => !f.why_not);
  const unmappable = made.fields.filter((f) => f.why_not);
  const taken = new Set(made.columns.flatMap((c) => (c.field ? [c.field] : [])));
  const set = (index: number, update: Parameters<typeof columnsWith>[2]) => {
    change({ columns: columnsWith(made.columns, index, update) });
  };
  return (
    <section className="card sheet-card" aria-labelledby={`${id}-title`}>
      <h3 className="section-title" id={`${id}-title`}>
        Columns
      </h3>
      <p>
        {made.mapping
          ? `Mapped by the project’s saved mapping “${made.mapping.name}”. `
          : "Iki matched columns to fields by their headers. "}
        Check each, and choose Not imported for those to leave out.
      </p>
      <SavedMappings made={made} change={change} settingsPending={settingsPending} />
      {made.warnings.length ? (
        <div className="alert alert-warning" role="status">
          <ul className="plain-list">
            {made.warnings.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
        </div>
      ) : null}
      <Scroll label="Each column, and the field it sets">
        <table className="table sheet-columns">
          <caption className="visually-hidden">Each column, and the field it sets</caption>
          <thead>
            <tr>
              <th scope="col">Column</th>
              <th scope="col">Its first cells</th>
              <th scope="col">Field</th>
              <th scope="col">How it reads</th>
            </tr>
          </thead>
          <tbody>
            {made.columns.map((column, at) => {
              const f = column.field ? fields.get(column.field) : undefined;
              const wrong = Object.entries(errors).find(([k]) =>
                k.startsWith(`columns/${String(at)}/`),
              );
              return (
                <tr key={column.index}>
                  <th scope="row">
                    <span className="sheet-letter">{column.letter}</span> {column.header}
                  </th>
                  <td className="sheet-examples">
                    {column.examples.length ? (
                      column.examples.join(", ")
                    ) : (
                      <span className="muted">Empty</span>
                    )}
                  </td>
                  <td>
                    <label className="visually-hidden" htmlFor={`${id}-field-${String(at)}`}>
                      Field for {columnName(column)}
                    </label>
                    <select
                      id={`${id}-field-${String(at)}`}
                      value={column.field ?? ""}
                      aria-invalid={wrong ? "true" : undefined}
                      aria-describedby={wrong ? `${id}-error-${String(at)}` : undefined}
                      onChange={(event) => {
                        set(column.index, { field: event.target.value || null });
                      }}
                    >
                      <option value="">Not imported</option>
                      {mappable.map((m) => (
                        <option
                          key={m.name}
                          value={m.name}
                          disabled={taken.has(m.name) && m.name !== column.field}
                        >
                          {fieldLabel(m)}
                          {taken.has(m.name) && m.name !== column.field ? " (mapped)" : ""}
                        </option>
                      ))}
                      {unmappable.length ? (
                        <optgroup label="Can’t be imported">
                          {unmappable.map((m) => (
                            <option key={m.name} value={m.name} disabled>
                              {m.label}: {m.why_not}
                            </option>
                          ))}
                        </optgroup>
                      ) : null}
                    </select>
                    {wrong ? (
                      <p className="field-error" id={`${id}-error-${String(at)}`}>
                        {wrong[1]}
                      </p>
                    ) : null}
                  </td>
                  <td>
                    {f ? <ColumnOptions column={column} field={f} made={made} set={set} /> : null}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </Scroll>
      {unmappable.length ? (
        <p className="field-help">
          Not from a sheet:{" "}
          {unmappable.map((f, i) => (
            <span key={f.name}>
              {i ? "; " : ""}
              {f.label} ({f.why_not?.replace(/\.$/, "").toLowerCase()})
            </span>
          ))}
          .
        </p>
      ) : null}
    </section>
  );
}

const SEPARATORS = [
  { value: ";", label: "Separated by ;" },
  { value: ",", label: "Separated by ," },
  { value: "|", label: "Separated by |" },
  { value: "/", label: "Separated by /" },
] as const;

/** What a column's cells need besides their field: a quantity's unit, a list's separator,
 * a date and time's zone, and an enumeration's spellings. */
function ColumnOptions({
  made,
  column,
  field: f,
  set,
}: {
  made: SheetImport;
  column: SheetColumn;
  field: SheetField;
  set: (index: number, update: Parameters<typeof columnsWith>[2]) => void;
}) {
  const id = useId();
  const type = f.item_type ?? f.type;
  const parts: ReactNode[] = [];
  const unit = f.unit;
  if (type === "quantity" && unit) {
    const units = [...new Set([unit, ...f.units, ...(column.unit ? [column.unit] : [])])];
    parts.push(
      <SelectField
        key="unit"
        compact
        label={`Unit of ${columnName(column)}’s cells`}
        value={column.unit ?? unit}
        onChange={(value) => {
          set(column.index, { unit: value === unit ? null : value });
        }}
        options={units.map((u) => ({
          value: u,
          label: u === unit ? `In ${u}` : `In ${u}, converted to ${unit}`,
        }))}
      />,
    );
  }
  if (f.type === "list") {
    parts.push(
      <SelectField
        key="separator"
        compact
        label={`How ${columnName(column)}’s items are separated`}
        value={(column.separator || ";") as ";"}
        onChange={(value) => {
          set(column.index, { separator: value === ";" ? null : value });
        }}
        options={SEPARATORS}
      />,
    );
  }
  if (type === "datetime") {
    parts.push(
      <span key="zone" className="sheet-zone">
        <label className="visually-hidden" htmlFor={`${id}-zone`}>
          Time zone of {columnName(column)}
        </label>
        <input
          id={`${id}-zone`}
          type="text"
          placeholder="Time zone, such as Europe/London"
          defaultValue={column.time_zone ?? ""}
          onBlur={(event) => {
            const zone = event.target.value.trim() || null;
            if (zone !== column.time_zone) set(column.index, { time_zone: zone });
          }}
        />
      </span>,
    );
  }
  if (type === "enum" || type === "bool") {
    parts.push(<Spellings key="values" made={made} column={column} field={f} set={set} />);
  }
  if (!parts.length) {
    return (
      <span className="muted">
        {type === "ref"
          ? `By HRID or name, of ${f.to.length ? "its" : "any"} type`
          : type === "date" || type === "datetime" || type === "int" || type === "float"
            ? "As the sheet’s numbers and dates are written"
            : "As written"}
      </span>
    );
  }
  return <div className="sheet-options">{parts}</div>;
}

/** An enumeration's or a truth value's cells that aren't one of its choices as written,
 * each given the choice it means. */
function Spellings({
  made,
  column,
  field: f,
  set,
}: {
  made: SheetImport;
  column: SheetColumn;
  field: SheetField;
  set: (index: number, update: Parameters<typeof columnsWith>[2]) => void;
}) {
  const choices = f.type === "bool" || f.item_type === "bool" ? ["true", "false"] : f.choices;
  const plain = (s: string) => s.trim().toLowerCase();
  const known = new Set([...choices.map(plain), "yes", "no", "y", "n", "1", "0", "x", "✓"]);
  const cells = new Set<string>();
  for (const row of made.sample.slice(made.first_row - 1)) {
    const text = (row[column.index] ?? "").trim();
    for (const part of f.type === "list" ? text.split(column.separator || ";") : [text]) {
      if (part.trim() && !known.has(plain(part))) cells.add(part.trim());
    }
  }
  for (const spelled of Object.keys(column.values)) cells.add(spelled);
  if (!cells.size) return <span className="muted">Its choices, in any case</span>;
  return (
    <ul className="plain-list sheet-spellings">
      {[...cells].slice(0, 12).map((cell) => (
        <li key={cell}>
          <SelectField
            compact
            label={`What “${cell}” in ${columnName(column)} means`}
            value={column.values[cell] ?? ""}
            onChange={(value) => {
              const next = Object.fromEntries(
                Object.entries(column.values).filter(([spelled]) => spelled !== cell),
              );
              if (value) next[cell] = value;
              set(column.index, { values: Object.keys(next).length ? next : null });
            }}
            options={[
              { value: "", label: `“${cell}” is…` },
              ...choices.map((c) => ({ value: c, label: `“${cell}” is ${c}` })),
            ]}
          />
        </li>
      ))}
    </ul>
  );
}

/** The project's saved mappings for the type, to map by, and saving this one. */
function SavedMappings({
  made,
  change,
  settingsPending,
}: {
  made: SheetImport;
  change: (choice: SheetChange) => void;
  settingsPending: boolean;
}) {
  const [review, setReview] = useState<SheetImport | null>(null);
  const others = made.mappings.filter((m) => m.id !== made.mapping?.id);
  return (
    <div className="sheet-mappings">
      {others.length ? (
        <SelectField
          label="Map by a saved mapping"
          value=""
          onChange={(value) => {
            if (value) change({ mapping: value });
          }}
          options={[
            { value: "", label: "Choose one…" },
            ...others.map((m) => ({
              value: m.id,
              label: `${m.name}: ${String(Math.round(m.match * 100))}% of its headers are here`,
            })),
          ]}
        />
      ) : null}
      {made.can["change"] ? (
        <Button
          variant="quiet"
          disabled={settingsPending}
          onClick={() => {
            setReview(made);
          }}
        >
          Save this mapping…
        </Button>
      ) : null}
      {review ? (
        <SaveMappingDialog
          initial={review}
          close={() => {
            setReview(null);
          }}
        />
      ) : null}
    </div>
  );
}

/** The assignments and reading choices that a template keeps, by header. */
function MappingDetails({
  title,
  description,
  header,
  columns,
  fields,
  locale,
}: {
  title: string;
  description: string;
  header: number;
  columns: readonly Record<string, unknown>[];
  fields: SheetField[];
  locale: SheetLocale;
}) {
  const id = useId();
  return (
    <section aria-labelledby={id}>
      <h4 id={id}>{title}</h4>
      <p>
        {description}. {header ? `Header row ${String(header)}.` : "No header row."}
      </p>
      <table className="table">
        <thead>
          <tr>
            <th scope="col">Header</th>
            <th scope="col">Field</th>
          </tr>
        </thead>
        <tbody>
          {columns.map((column, index) => {
            const {
              header,
              field,
              unit,
              time_zone: zone,
              separator,
              dates,
              decimal,
              serial_dates: serial,
              values,
            } = column;
            if (typeof header !== "string" || typeof field !== "string") return null;
            const reading: string[] = [];
            if (typeof unit === "string") reading.push(`Unit ${unit}`);
            if (typeof separator === "string" && separator !== ";")
              reading.push(`Items separated by “${separator}”`);
            if (typeof zone === "string") reading.push(`Time zone ${zone}`);
            if (typeof dates === "string") reading.push(dates.replaceAll("_", " "));
            if (typeof decimal === "string") reading.push(`Decimal ${decimal}`);
            if (typeof serial === "boolean")
              reading.push(serial ? "Excel serial dates" : "No Excel serial dates");
            if (values && typeof values === "object") {
              for (const [text, value] of Object.entries(values)) {
                if (typeof value === "string") reading.push(`“${text}” means “${value}”`);
              }
            }
            return (
              <tr key={index}>
                <th scope="row">{header}</th>
                <td>
                  {fields.find((f) => f.name === field)?.label ?? field}
                  {reading.length > 0 && <div className="muted">{reading.join(" · ")}</div>}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p>
        {locale.decimal ? `Decimal ${locale.decimal}` : "Decimal mark unspecified"}.{" "}
        {locale.dates
          ? `Date order ${locale.dates.replaceAll("_", " ")}`
          : "Date order unspecified"}
        .{" "}
        {locale.serial_dates
          ? "Plain numbers in date columns are Excel serial dates."
          : "Plain numbers in date columns are not Excel serial dates."}
      </p>
    </section>
  );
}

function SaveMappingDialog({ initial, close }: { initial: SheetImport; close: () => void }) {
  const client = useQueryClient();
  const { say } = useMessages();
  const id = useId();
  const [round, setRound] = useState(0);
  const [entered, setEntered] = useState<{ name: string; replace: boolean } | null>(null);
  // A review is a snapshot. Page refetches cannot replace its import or retarget a save.
  const review = useQuery({
    queryKey: ["spreadsheet-mapping-review", id, round],
    staleTime: "static",
    gcTime: 0,
    retry: false,
    queryFn: async ({ signal }) => {
      const read = pageReadSignal(signal);
      const source =
        round === 0 ? initial : await client.query({ ...importQuery(initial.id), staleTime: 0 });
      const target = initial.mapping
        ? (await savedMappings(initial.project, initial.type, read)).find(
            (m) => m.id === initial.mapping?.id,
          )
        : undefined;
      return { source, target };
    },
  });
  const save = useApiMutation(
    (body: SaveMapping) => saveMapping(initial.id, body),
    [[SPREADSHEETS, initial.id]],
  );
  const target = review.data?.target;
  const source = review.data?.source;
  const targetName = target?.name ?? initial.mapping?.name;
  const name = entered?.name ?? targetName ?? "";
  const replacing = entered?.replace ?? !!initial.mapping;
  const missing = !!review.data && !!initial.mapping && !target;
  const stale = save.error?.status === 409 || save.error?.status === 404;
  const blocked =
    !source || !source.can["change"] || !name.trim() || missing || stale || review.isError;
  return (
    <ConfirmDialog
      isOpen
      title="Save this mapping"
      confirmLabel={replacing ? "Save changes" : "Save"}
      confirmVariant="primary"
      busy={save.isPending}
      confirmDisabled={blocked}
      problem={
        save.error ? problemOf(save.error) : review.error ? problemOf(review.error) : undefined
      }
      onCancel={() => {
        if (missing || stale)
          void client.invalidateQueries({ queryKey: importQuery(initial.id).queryKey });
        close();
      }}
      onConfirm={() => {
        if (blocked) return;
        const body: SaveMapping = { name: name.trim(), source_revision: source.mapping_revision };
        if (replacing && target) {
          body.replace = target.id;
          body.target_revision = target.revision;
        }
        save.mutate(body, {
          onSuccess: () => {
            close();
            say(`Saved the mapping “${body.name}”: sheets with these headers map by it.`);
          },
        });
      }}
    >
      {review.isPending ? <Loading label="Loading the mapping for review…" /> : null}
      {source ? (
        <MappingDetails
          title="Mapping to save"
          description={`Sheet “${source.sheet}” in ${source.file.name}, revision ${String(source.mapping_revision)}`}
          header={source.header_row}
          columns={source.columns}
          fields={source.fields}
          locale={source.locale}
        />
      ) : null}
      {replacing && target && source ? (
        <MappingDetails
          title="Current saved mapping"
          description={`“${target.name}”, revision ${String(target.revision)}`}
          header={target.header_row}
          columns={target.columns}
          fields={source.fields}
          locale={target.locale}
        />
      ) : null}
      {missing ? (
        <p role="alert">
          The saved mapping you selected is no longer available. Close this review to choose another
          mapping.
        </p>
      ) : null}
      {source && !source.can["change"] ? (
        <p role="alert">This import is no longer editable.</p>
      ) : null}
      <TextField
        label="Name"
        value={name}
        onChange={(value) => {
          setEntered({ name: value, replace: !!target && value.trim() === target.name });
        }}
        maxLength={100}
      />
      {stale || review.isError || missing ? (
        <Button
          onClick={() => {
            if (replacing) setEntered(null);
            save.reset();
            setRound((r) => r + 1);
          }}
        >
          Review latest mapping
        </Button>
      ) : null}
    </ConfirmDialog>
  );
}

/** How the sheet writes numbers and dates, and its own cells as they read so: those that
 * read two ways are flagged, never guessed. */
export function NumbersAndDates({
  made,
  change,
}: {
  made: SheetImport;
  change: (choice: SheetChange) => void;
}) {
  const id = useId();
  const columns = new Map(made.columns.map((c) => [c.index, c]));
  const numbers = made.readings.some((r) => r.kind === "numbers");
  const dates = made.readings.some((r) => r.kind === "dates");
  const ambiguous = made.readings.flatMap((r) => r.cells).filter((c) => c.ambiguous).length;
  const locale = made.locale;
  // The locale is said whole: what's changed, and the rest as it was.
  const say = (next: Partial<SheetLocaleChange>) => {
    change({ locale: { ...locale, ...next } as SheetLocaleChange });
  };
  return (
    <section className="card sheet-card" aria-labelledby={`${id}-title`}>
      <h3 className="section-title" id={`${id}-title`}>
        Numbers and dates
      </h3>
      <p>
        Say how the sheet writes them: Iki doesn’t guess. Until you do, a cell that reads two ways,
        such as 1,234 or 03/04/2026, is flagged. Cells the workbook stores as numbers or dates read
        one way only. Below, Iki writes numbers with a decimal point and a space between thousands,
        and dates year first.
      </p>
      {ambiguous ? (
        <div className="alert alert-warning" role="status">
          <p>
            {plural(ambiguous, "cell below reads", "cells below read")} two ways: say how the sheet
            is written.
          </p>
        </div>
      ) : null}
      <div className="sheet-locale">
        <RadioField
          label="Decimal mark"
          value={(locale.decimal ?? "") as (typeof DECIMALS)[number]["value"]}
          onChange={(value) => {
            say({ decimal: value || null });
          }}
          options={DECIMALS}
        />
        <RadioField
          label="Dates written as numbers"
          value={(locale.dates ?? "") as (typeof DATE_ORDERS)[number]["value"]}
          onChange={(value) => {
            say({ dates: value || null });
          }}
          options={DATE_ORDERS}
        />
        <CheckboxField
          label="Plain numbers in date columns are Excel serial dates"
          help="As when a date column lost its format: 46023 is 1 January 2026."
          checked={locale.serial_dates}
          onChange={(checked) => {
            say({ serial_dates: checked });
          }}
        />
      </div>
      {made.readings.length ? (
        <Scroll label="How its numbers and dates read">
          <table className="table sheet-readings">
            <caption>
              How {numbers && dates ? "numbers and dates" : numbers ? "numbers" : "dates"} in the
              mapped columns read
            </caption>
            <thead>
              <tr>
                <th scope="col">Row</th>
                <th scope="col">In the sheet</th>
                <th scope="col">Reads as</th>
              </tr>
            </thead>
            {/* A group a column: its name once, then its cells. */}
            {made.readings.map((reading) => {
              const column = columns.get(reading.column);
              return (
                <tbody key={reading.column}>
                  <tr className="sheet-reading-column">
                    <th scope="colgroup" colSpan={3}>
                      {column ? (
                        <>
                          <span className="sheet-letter">{column.letter}</span> {column.header}
                        </>
                      ) : (
                        reading.field
                      )}
                    </th>
                  </tr>
                  {reading.cells.map((cell) => (
                    <tr key={cell.row}>
                      <td className="numeric">{cell.row}</td>
                      <td>
                        <code>{cell.cell}</code>
                        {cell.stored ? <span className="muted"> stored as such</span> : null}
                      </td>
                      <td>
                        {cell.reads_as ??
                          (cell.ambiguous ? (
                            <>
                              <Badge>Reads two ways</Badge> {cell.problem}
                            </>
                          ) : (
                            <span className="sheet-unreadable">{cell.problem}</span>
                          ))}
                      </td>
                    </tr>
                  ))}
                </tbody>
              );
            })}
          </table>
        </Scroll>
      ) : (
        <p className="muted">No mapped column holds numbers or dates.</p>
      )}
    </section>
  );
}

// --- 4. The preview -----------------------------------------------------------------------

/** How far a preview or an import has got. */
export function ImportProgress({
  label,
  value,
  message,
}: {
  label: string;
  value: number | null;
  message: string;
}) {
  return (
    <div className="run-progress" role="status">
      <span>{message || label}</span>
      {value == null ? (
        <progress aria-label={label} />
      ) : (
        <progress aria-label={label} max={1} value={value} />
      )}
      {value == null ? null : <span>{Math.round(value * 100)}%</span>}
    </div>
  );
}

/** What importing would do, row by row: each outcome counted, and the rows themselves. */
export function PreviewSummary({ made }: { made: SheetImport }) {
  const counts = made.preview?.counts ?? {};
  const found = counted(made);
  const problems = counts["problem"] ?? 0;
  const ambiguous = counts["ambiguous"] ?? 0;
  return (
    <>
      {problems ? (
        <div className="alert alert-warning" role="status">
          <p>
            {plural(problems, "row has", "rows have")} problems
            {ambiguous
              ? `, ${plural(ambiguous, "of them", "of them")} with cells that read two ways`
              : ""}
            . Fix them in the workbook and import it again, or leave them out.
          </p>
        </div>
      ) : (
        <div className="alert alert-success" role="status">
          <p>Every row can be imported as it is.</p>
        </div>
      )}
      <ul className="sheet-counts">
        {found.map(({ outcome, count }) => (
          <li key={outcome} className={`sheet-count sheet-count-${outcome}`}>
            <strong>{count.toLocaleString("en")}</strong>{" "}
            {count === 1 ? OUTCOMES[outcome].one : OUTCOMES[outcome].many}
          </li>
        ))}
        {counts["blank"] ? (
          <li className="sheet-count">
            <strong>{counts["blank"].toLocaleString("en")}</strong> blank, left out
          </li>
        ) : null}
        {counts["held"] ? (
          <li className="sheet-count">
            <strong>{counts["held"].toLocaleString("en")}</strong> held for review by a rule once
            imported
          </li>
        ) : null}
      </ul>
    </>
  );
}

function ProblemText({ made, problem }: { made: SheetImport; problem: SheetProblem }) {
  const column = problem.column == null ? undefined : made.columns[problem.column];
  return (
    <>
      {column ? <span className="sheet-letter">{column.letter}</span> : null} {problem.detail}
    </>
  );
}

/** The rows, as importing would write them: those with problems first, by default. */
export function PreviewRows({ made, project }: { made: SheetImport; project: Project }) {
  const problems = made.preview?.counts["problem"] ?? 0;
  const [outcome, setOutcome] = useState<Outcome | "">(problems ? "problem" : "");
  const list = useInfiniteQuery(rowsQuery(made, outcome || undefined));
  const rows = list.data?.pages.flatMap((p) => p.items) ?? [];
  const id = useId();
  return (
    <section className="card sheet-card" aria-labelledby={`${id}-title`}>
      <h3 className="section-title" id={`${id}-title`}>
        {made.kind === "csv" ? "Rows" : `Rows of ${made.sheet}`}
      </h3>
      <SelectField
        label="Show"
        value={outcome}
        onChange={setOutcome}
        options={[
          { value: "", label: "Every row" },
          ...counted(made).map(({ outcome: o, count }) => ({
            value: o,
            label: `${OUTCOMES[o].label} (${count.toLocaleString("en")})`,
          })),
        ]}
      />
      {list.isError ? <ProblemAlert problem={problemOf(list.error)} /> : null}
      {list.isPending ? <Loading label="Loading its rows…" /> : null}
      {rows.length ? (
        <Scroll label="Its rows">
          <table className="table sheet-rows">
            <caption className="visually-hidden">
              {outcome ? OUTCOMES[outcome].label : "Every row"}, in the sheet’s order
            </caption>
            <thead>
              <tr>
                <th scope="col">Row</th>
                <th scope="col">Importing it</th>
                <th scope="col">Why</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr
                  key={row.row}
                  className={row.outcome === "problem" ? "sheet-row-problem" : undefined}
                >
                  <th scope="row" className="numeric">
                    {row.row}
                  </th>
                  <td>{OUTCOMES[row.outcome].label}</td>
                  <td>
                    {row.problems.length ? (
                      <ul className="plain-list">
                        {row.problems.map((p, i) => (
                          <li key={i}>
                            <ProblemText made={made} problem={p} />
                          </li>
                        ))}
                      </ul>
                    ) : row.outcome === "same" ? (
                      `As row ${String(row.same_as)}`
                    ) : row.match && row.match_id ? (
                      <Link to="/entities/$ref/" params={{ ref: row.match }}>
                        {row.match}
                      </Link>
                    ) : row.match ? (
                      row.match
                    ) : null}
                    {row.held.length ? (
                      <p className="muted">
                        Held for review: {row.held.map((h) => h["rule"]).join(", ")}
                      </p>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Scroll>
      ) : list.isSuccess ? (
        <p className="empty">No rows {outcome ? "like that" : ""}.</p>
      ) : null}
      {list.hasNextPage ? (
        <Button
          variant="quiet"
          busy={list.isFetchingNextPage}
          busyText="Loading…"
          onClick={() => {
            void list.fetchNextPage();
          }}
        >
          Show more rows
        </Button>
      ) : null}
      <p className="field-help">
        Importing checks every row again, as {project.name} is then: if anything changed since, it
        imports nothing and says which rows.
      </p>
    </section>
  );
}

// --- 5. The import ------------------------------------------------------------------------

/** Imports what the preview checked, as one run: all of it, or none. */
export function CommitImport({
  made,
  onCommitted,
}: {
  made: SheetImport;
  onCommitted?: (made: SheetImport) => void;
}) {
  const id = useId();
  const problems = made.preview?.counts["problem"] ?? 0;
  const rows = importable(made);
  const type = useQuery(typesQuery).data?.find((t) => t.name === made.type);
  const typeLabel = type ? inSentence(type.label_plural) : made.type;
  const [leaveOut, setLeaveOut] = useState(false);
  const [reason, setReason] = useState("");
  const commit = useApiMutation(
    () => commitImport(made.id, { skip_problems: leaveOut, reason: reason.trim() }),
    [[SPREADSHEETS, made.id]],
  );
  const blocked = (problems > 0 && !leaveOut) || rows === 0;
  return (
    <form
      className="card sheet-card"
      aria-labelledby={`${id}-title`}
      onSubmit={(event: SubmitEvent<HTMLFormElement>) => {
        event.preventDefault();
        if (blocked) return;
        commit.mutate(undefined, {
          onSuccess: (next) => {
            onCommitted?.(next);
          },
        });
      }}
    >
      <h3 className="section-title" id={`${id}-title`}>
        Import
      </h3>
      <ProblemAlert problem={commit.error ? problemOf(commit.error) : undefined} />
      {problems ? (
        <CheckboxField
          label={`Leave out the ${plural(problems, "row", "rows")} with problems`}
          help="They aren’t imported. The others are, all of them or none."
          checked={leaveOut}
          onChange={setLeaveOut}
        />
      ) : null}
      <TextField
        label="Why"
        value={reason}
        onChange={setReason}
        optional
        maxLength={2000}
        help="For the run and the audit trail, such as the batch or the request it’s for."
      />
      <p id={`${id}-what`} className="field-help">
        {rows
          ? `One run writes ${plural(rows, "row", "rows")} as ${typeLabel}, in row order: all of them, or none if anything is wrong.`
          : "There’s nothing to import."}
      </p>
      <Button
        type="submit"
        variant="primary"
        busy={commit.isPending}
        busyText="Starting…"
        disabled={blocked}
        describedBy={`${id}-what`}
      >
        Import {plural(problems && leaveOut ? rows : rows, "row", "rows")}
      </Button>
    </form>
  );
}

/** Where the import went: its run's progress, then its records, or why it didn't. */
export function ImportResult({ made, project }: { made: SheetImport; project: Project }) {
  const navigate = useNavigate();
  const again = useApiMutation(() => startImport(project.id, made.file.id, made.type), []);
  const run = made.run;
  if (made.state === "committing" || made.state === "importing") {
    return (
      <section className="card sheet-card">
        <h3 className="section-title">Importing</h3>
        <ImportProgress
          label="Importing its rows"
          value={run?.progress ?? null}
          message={run?.message ?? ""}
        />
        {run ? (
          <p>
            Run{" "}
            <Link to="/runs/$ref/" params={{ ref: run.hrid }}>
              {run.hrid}
            </Link>{" "}
            imports them.
          </p>
        ) : null}
      </section>
    );
  }
  if (made.state === "imported" && run) {
    const rows = importable(made);
    return (
      <section className="card sheet-card">
        <div className="alert alert-success" role="status">
          <p>
            Imported {plural(rows, "row", "rows")} of {made.file.name} into {project.name}.
          </p>
        </div>
        <p className="sheet-done-links">
          <Link
            className={buttonClass("primary")}
            to="/projects/$slug/entities/"
            params={{ slug: project.slug }}
            search={{ type: made.type, run: run.hrid }}
          >
            Show the imported records
          </Link>
          <Link to="/runs/$ref/" params={{ ref: run.hrid }}>
            Run {run.hrid}
          </Link>
        </p>
      </section>
    );
  }
  return (
    <section className="card sheet-card">
      <div className="alert alert-error" role="alert">
        <p>
          {made.error ||
            (made.state === "cancelled"
              ? "Its run was cancelled: nothing was imported."
              : "Nothing was imported: its rows no longer fit, as the project is now.")}
        </p>
      </div>
      {made.run_problems.length ? (
        <Scroll label="Why, by row">
          <table className="table">
            <caption>Why</caption>
            <thead>
              <tr>
                <th scope="col">Row</th>
                <th scope="col">Problem</th>
              </tr>
            </thead>
            <tbody>
              {made.run_problems.map((p, i) => (
                <tr key={i}>
                  <th scope="row" className="numeric">
                    {p.row ?? "—"}
                  </th>
                  <td>
                    <ProblemText made={made} problem={p} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Scroll>
      ) : null}
      {project.can.edit && !project.archived ? (
        <Button
          busy={again.isPending}
          busyText="Starting…"
          onClick={() => {
            again.mutate(undefined, {
              onSuccess: (next) => {
                void navigate({
                  to: "/projects/$slug/entities/import/$importId/",
                  params: { slug: project.slug, importId: next.id },
                });
              },
            });
          }}
        >
          Import {made.file.name} again
        </Button>
      ) : null}
      {run ? (
        <p>
          <Link to="/runs/$ref/" params={{ ref: run.hrid }}>
            Run {run.hrid}
          </Link>
        </p>
      ) : null}
    </section>
  );
}

// --- The pages ----------------------------------------------------------------------------

/**
 * The import's own heading. It sits under the project's (its name, its tabs, the bar
 * across the top), as a new record's form does: the project's is the page's one h1.
 */
export function ImportHeading({
  title,
  badge,
  description,
  actions,
}: {
  title: string;
  badge?: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="section-header sheet-heading">
      <div>
        <h2 className="sheet-title">
          {title}
          {badge ? <> {badge}</> : null}
        </h2>
        {description ? <p className="muted sheet-description">{description}</p> : null}
      </div>
      {actions ? <div className="sheet-heading-actions">{actions}</div> : null}
    </div>
  );
}

/** Starting an import: the type, and the workbook to upload. */
export function SpreadsheetImportStart() {
  const project = useProject();
  const search = useSearch({ from: "/projects/$slug/entities/import" });
  const navigate = useNavigate();
  const types = useSuspenseQuery(typesQuery).data;
  usePageTitle(`Import a spreadsheet · ${project.name}`);
  return (
    <>
      <ImportHeading title="Import a spreadsheet" />
      <ImportSteps current="workbook" />
      {project.can.edit && !project.archived ? (
        <ChooseWorkbook
          project={project}
          types={types}
          type={search.type}
          onStarted={(made) => {
            void navigate({
              to: "/projects/$slug/entities/import/$importId/",
              params: { slug: project.slug, importId: made.id },
            });
          }}
        />
      ) : (
        <p className="empty">
          {project.archived
            ? `${project.name} is archived: restore it to import into it.`
            : `Only the project’s Editors can import into ${project.name}.`}
        </p>
      )}
    </>
  );
}

/** An import, at whichever step it's at. */
export function SpreadsheetImportPage() {
  const project = useProject();
  const { importId } = useParams({ from: "/projects/$slug/entities/import/$importId" });
  const found = useQuery(importQuery(importId));
  const types = useSuspenseQuery(typesQuery).data;
  const client = useQueryClient();
  const navigate = useNavigate();
  const { say } = useMessages();
  const [step, setStep] = useState<"rows" | "columns">("rows");
  const [discarding, setDiscarding] = useState(false);
  const [pendingSettings, setPendingSettings] = useState(0);
  const change = useApiMutation((choice: SheetChange) => changeImport(importId, choice), []);
  const preview = useApiMutation(() => previewImport(importId), []);
  const discard = useApiMutation(() => discardImport(importId), [[SPREADSHEETS, importId]]);
  const made = found.data;
  usePageTitle(made ? `Import of ${made.file.name} · ${project.name}` : project.name);
  if (found.isPending) return <Loading label="Loading the import…" />;
  if (found.isError || !made) {
    return (
      <>
        <ImportHeading title="Import a spreadsheet" />
        <ProblemAlert problem={problemOf(found.error)} />
      </>
    );
  }
  const type = types.find((t) => t.name === made.type);
  const apply = (choice: SheetChange) => {
    setPendingSettings((count) => count + 1);
    void change
      .mutateAsync(choice)
      .then((next) => client.setQueryData(importQuery(importId).queryKey, next))
      .catch(() => {
        /* The editing step displays the mutation error. */
      })
      .finally(() => {
        setPendingSettings((count) => count - 1);
      });
  };
  const current =
    made.state === "mapping" || made.state === "reading"
      ? step
      : made.state === "previewing" || made.state === "previewed"
        ? "preview"
        : "import";
  const back = (to: "rows" | "columns") => {
    // Back from the preview: it's started afresh once anything changes.
    setStep(to);
    if (made.state === "previewed" || made.state === "previewing") apply({});
  };
  return (
    <>
      <ImportHeading
        title={`Import ${made.file.name}`}
        badge={<Badge muted={made.state !== "imported"}>{STATES[made.state]}</Badge>}
        description={`As ${type ? inSentence(type.label_plural) : made.type}, into ${project.name}. ${formatSize(made.file.size)}.`}
        actions={
          made.can["discard"] ? (
            <Button
              variant="quiet"
              onClick={() => {
                setDiscarding(true);
              }}
            >
              Discard
            </Button>
          ) : null
        }
      />
      <ImportSteps current={current} />
      {made.state === "reading" ? <Loading label={`Reading ${made.file.name}…`} /> : null}
      {made.state === "failed" && !made.run ? (
        <div className="alert alert-error" role="alert">
          <p>{made.error}</p>
        </div>
      ) : null}
      {made.state === "discarded" ? (
        <p className="empty">It was discarded: nothing was imported.</p>
      ) : null}
      {made.state === "mapping" && made.error ? (
        <div className="alert alert-error" role="alert">
          <p>{made.error}</p>
        </div>
      ) : null}
      {made.state === "mapping" ? (
        <>
          {step === "rows" ? (
            <ChooseRows
              made={made}
              change={apply}
              problem={change.error ? problemOf(change.error) : undefined}
            />
          ) : (
            <>
              <MapColumns
                settingsPending={pendingSettings > 0}
                made={made}
                change={apply}
                problem={change.error ? problemOf(change.error) : undefined}
              />
              <NumbersAndDates made={made} change={apply} />
            </>
          )}
          <div className="actions sheet-actions">
            {step === "columns" ? (
              <Button
                onClick={() => {
                  back("rows");
                }}
              >
                Back
              </Button>
            ) : null}
            {step === "rows" ? (
              <Button
                variant="primary"
                onClick={() => {
                  setStep("columns");
                }}
              >
                Next: columns
              </Button>
            ) : (
              <Button
                variant="primary"
                busy={preview.isPending || change.isPending}
                busyText="Checking…"
                disabled={!made.can["preview"]}
                onClick={() => {
                  preview.mutate(undefined, {
                    onSuccess: (next) => {
                      client.setQueryData(importQuery(importId).queryKey, next);
                    },
                  });
                }}
              >
                Check every row
              </Button>
            )}
          </div>
          {preview.error ? <ProblemAlert problem={problemOf(preview.error)} /> : null}
        </>
      ) : null}
      {made.state === "previewing" && made.preview ? (
        <section className="card sheet-card">
          <h3 className="section-title">Checking every row</h3>
          <ImportProgress
            label="Checking its rows"
            value={made.preview.progress}
            message={made.preview.message}
          />
        </section>
      ) : null}
      {made.state === "previewed" ? (
        <>
          <PreviewSummary made={made} />
          <PreviewRows made={made} project={project} />
          <div className="actions sheet-actions">
            <Button
              onClick={() => {
                back("columns");
              }}
            >
              Back to the columns
            </Button>
          </div>
          {made.can["commit"] ? (
            <CommitImport
              made={made}
              onCommitted={(next) => {
                client.setQueryData(importQuery(importId).queryKey, next);
              }}
            />
          ) : null}
        </>
      ) : null}
      {made.run ? <ImportResult made={made} project={project} /> : null}
      <ConfirmDialog
        isOpen={discarding}
        title={`Discard the import of ${made.file.name}?`}
        confirmLabel="Discard"
        busy={discard.isPending}
        problem={discard.error ? problemOf(discard.error) : undefined}
        onCancel={() => {
          setDiscarding(false);
        }}
        onConfirm={() => {
          discard.mutate(undefined, {
            onSuccess: () => {
              setDiscarding(false);
              say(`Discarded the import. ${made.file.name} stays in the project.`);
              void navigate({ to: "/projects/$slug/entities/", params: { slug: project.slug } });
            },
          });
        }}
      >
        <p>Nothing has been imported from it. The workbook stays in the project’s files.</p>
      </ConfirmDialog>
    </>
  );
}
