/**
 * One input for each kind of entity field, wired as the design system's
 * fields are: a label, errors and help linked through `aria-describedby`,
 * `aria-invalid` on invalid inputs. Numbers are text inputs with a decimal
 * keyboard (the browser's number input changes on scroll and reads "1,5"
 * differently by locale). References are picked with a combo box that
 * searches as people type; people with one that finds only those the field can name.
 */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
  type RefObject,
} from "react";
import {
  DismissButton,
  Overlay,
  useComboBox,
  useListBox,
  useOption,
  usePopover,
  type AriaListBoxOptions,
} from "react-aria";
import { Item, useComboBoxState, type ComboBoxState, type Key, type Node } from "react-stately";

import {
  formatSize,
  pickerQuery,
  projectFilesQuery,
  typeLabel,
  type EntityType,
  type FieldDef,
} from "./entities";
import {
  emptyDraft,
  newItem,
  OTHER_UNIT,
  propertySchema,
  rowFields,
  type Draft,
  type Picked,
  type RowDraft,
  type Schemaed,
  type QuantityDraft,
} from "./entity-values";
import { optionLabel } from "./filters";
import { describeHelm, isProblem, useHelmReading } from "./helm";
import { media } from "./media";
import { nameableQuery } from "./people";
import { upload, uploadError, type Progress } from "./uploads";
import { Button } from "./ui";

export interface InputProps {
  field: FieldDef;
  /** What the field's value is checked against. */
  type: Schemaed;
  types: EntityType[];
  draft: Draft;
  onChange: (draft: Draft) => void;
  error?: string | undefined;
  /** Shown under the input, after the field's own description. */
  note?: string | undefined;
  disabled?: boolean;
  /** Where files are chosen from. */
  projectId: string;
  /** In a list: the item's label, which screen readers hear but the page doesn't show. */
  itemLabel?: string | undefined;
  /** The API's messages by where they point (`values/components/1/part`), for a list of
   * rows' fields (ADR 0137), and where this input's value is among them. */
  errors?: Record<string, string> | undefined;
  path?: string | undefined;
}

function describedBy(...ids: (string | false | undefined)[]): string | undefined {
  return ids.filter(Boolean).join(" ") || undefined;
}

/** The text under a field: its description, then what this form adds. */
function helpText(field: FieldDef, note: string | undefined): string | undefined {
  return [field.description, note].filter(Boolean).join(" ") || undefined;
}

function Shell({
  id,
  label,
  optional,
  error,
  help,
  children,
  hiddenLabel = false,
}: {
  id: string;
  label: string;
  optional: boolean;
  error: string | undefined;
  help: string | undefined;
  children: ReactNode;
  hiddenLabel?: boolean;
}) {
  return (
    <div className={error ? "field field-invalid" : "field"}>
      <label htmlFor={id} className={hiddenLabel ? "visually-hidden" : undefined}>
        {label}
        {optional && !hiddenLabel ? <span className="optional"> (optional)</span> : null}
      </label>
      {error ? (
        <p className="field-error" id={`${id}-error`}>
          {error}
        </p>
      ) : null}
      {children}
      {help ? (
        <p className="field-help" id={`${id}-help`}>
          {help}
        </p>
      ) : null}
    </div>
  );
}

export function FieldInput(props: InputProps) {
  const { field } = props;
  if (field.type === "list") return <ListInput {...props} />;
  switch (field.type) {
    case "quantity":
      return <QuantityInput {...props} />;
    case "bool":
      return <BoolInput {...props} />;
    case "enum":
      return <EnumInput {...props} />;
    case "ref":
      return <RefInput {...props} />;
    case "person":
      return <PersonInput {...props} />;
    case "file":
      return <FileInput {...props} />;
    case "row":
      return <RowInput {...props} />;
    default:
      return <TextInput {...props} />;
  }
}

/**
 * A list's row (ADR 0137): an input for each of its fields, each with what the API said
 * of it. The row's own schema checks them as the type's checks its fields.
 */
function RowInput(props: InputProps) {
  const { field, draft, onChange, type, itemLabel, errors, path } = props;
  const row = draft as RowDraft;
  const schema = { values_schema: propertySchema(type, field.name).items ?? {} } as Schemaed;
  const latest = useRef(row);
  useLayoutEffect(() => {
    latest.current = row;
  }, [row]);
  return (
    <fieldset className="row-input">
      <legend className="visually-hidden">{itemLabel ?? field.label}</legend>
      {rowFields(field).map((cell) => (
        <FieldInput
          {...props}
          key={cell.name}
          field={cell}
          type={schema}
          draft={row.cells[cell.name] ?? null}
          error={path ? errors?.[`${path}/${cell.name}`] : undefined}
          note={undefined}
          itemLabel={undefined}
          onChange={(next) => {
            const changed = { cells: { ...latest.current.cells, [cell.name]: next } };
            latest.current = changed;
            onChange(changed);
          }}
        />
      ))}
    </fieldset>
  );
}

function TextInput(props: InputProps) {
  const schema = propertySchema(props.type, props.field.name);
  if (schema["x-iki-format"] === "helm") return <HelmInput {...props} />;
  return <PlainTextInput {...props} />;
}

/**
 * A HELM field (ADR 0144): read as it's typed, by the module the server reads it with,
 * saying what it holds or what's wrong and where. The reading is announced politely, so
 * a screen reader hears it once typing settles.
 */
function HelmInput({ field, draft, onChange, error, note, disabled, itemLabel }: InputProps) {
  const id = useId();
  const help = helpText(field, note);
  const text = (draft as string | null) ?? "";
  const reading = useHelmReading(text);
  const problem = reading && isProblem(reading) ? describeHelm(reading) : undefined;
  const shown = problem ?? error;
  return (
    <Shell
      id={id}
      label={itemLabel ?? field.label}
      optional={!field.required}
      error={shown}
      help={help}
      hiddenLabel={itemLabel !== undefined}
    >
      <textarea
        id={id}
        rows={3}
        className="input-code"
        spellCheck={false}
        autoComplete="off"
        value={text}
        disabled={disabled}
        aria-invalid={shown ? "true" : undefined}
        aria-describedby={describedBy(
          shown && `${id}-error`,
          `${id}-reading`,
          help && `${id}-help`,
        )}
        aria-required={field.required || undefined}
        onChange={(event) => {
          onChange(event.target.value);
        }}
      />
      <p className="field-help" id={`${id}-reading`} role="status">
        {reading && !isProblem(reading) ? describeHelm(reading) : ""}
      </p>
    </Shell>
  );
}

function PlainTextInput({
  field,
  draft,
  onChange,
  error,
  note,
  disabled,
  type,
  itemLabel,
}: InputProps) {
  const id = useId();
  const help = helpText(field, note);
  const schema = propertySchema(type, field.name);
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const shared = {
    id,
    value: draft as string,
    disabled,
    onChange: (event: { target: { value: string } }) => {
      onChange(event.target.value);
    },
    "aria-invalid": error ? ("true" as const) : undefined,
    "aria-describedby": describedBy(error && `${id}-error`, help && `${id}-help`),
    "aria-required": field.required || undefined,
  };
  let input: ReactNode;
  switch (field.type) {
    case "text":
      input = <textarea rows={4} {...shared} />;
      break;
    case "json":
      input = <textarea rows={6} className="input-code" spellCheck={false} {...shared} />;
      break;
    case "date":
      input = <input type="date" {...shared} />;
      break;
    case "datetime":
      input = <input type="datetime-local" step={1} {...shared} />;
      break;
    case "int":
    case "float":
      input = (
        <input
          type="text"
          inputMode={field.type === "int" ? "numeric" : "decimal"}
          autoComplete="off"
          spellCheck={false}
          className="input-number"
          {...shared}
        />
      );
      break;
    default: {
      const sequence = schema["x-iki-format"]?.endsWith("_sequence");
      input = (
        <input
          type="text"
          autoComplete="off"
          spellCheck={sequence ? false : undefined}
          className={sequence ? "input-code" : undefined}
          {...shared}
        />
      );
    }
  }
  return (
    <Shell
      id={id}
      label={itemLabel ?? field.label}
      optional={!field.required}
      error={error}
      help={
        field.type === "datetime" ? [help, `In your time zone (${zone}).`].join(" ").trim() : help
      }
      hiddenLabel={itemLabel !== undefined}
    >
      {input}
    </Shell>
  );
}

function QuantityInput({ field, draft, onChange, error, note, disabled, itemLabel }: InputProps) {
  const id = useId();
  const q = draft as QuantityDraft;
  const help = helpText(field, note);
  const units = field.input_units;
  const set = (change: Partial<QuantityDraft>) => {
    onChange({ ...q, ...change });
  };
  return (
    <Shell
      id={id}
      label={itemLabel ?? field.label}
      optional={!field.required}
      error={error}
      help={help}
      hiddenLabel={itemLabel !== undefined}
    >
      <div className="input-with-unit">
        <input
          id={id}
          type="text"
          inputMode="decimal"
          autoComplete="off"
          spellCheck={false}
          className="input-number"
          value={q.number}
          disabled={disabled}
          aria-invalid={error ? "true" : undefined}
          aria-describedby={describedBy(error && `${id}-error`, help && `${id}-help`)}
          aria-required={field.required || undefined}
          onChange={(event) => {
            set({ number: event.target.value });
          }}
        />
        <label className="visually-hidden" htmlFor={`${id}-unit`}>
          Unit for {itemLabel ?? field.label}
        </label>
        <select
          id={`${id}-unit`}
          value={q.unit}
          disabled={disabled}
          onChange={(event) => {
            set({ unit: event.target.value });
          }}
        >
          {units.map((u) => (
            <option key={u.code} value={u.code}>
              {u.symbol}
            </option>
          ))}
          <option value={OTHER_UNIT}>Other unit…</option>
        </select>
      </div>
      {q.unit === OTHER_UNIT ? (
        <div className="field-sub">
          <label htmlFor={`${id}-other`}>UCUM unit code</label>
          <input
            id={`${id}-other`}
            type="text"
            autoComplete="off"
            spellCheck={false}
            className="input-code"
            value={q.other}
            disabled={disabled}
            aria-describedby={`${id}-other-help`}
            onChange={(event) => {
              set({ other: event.target.value });
            }}
          />
          <p className="field-help" id={`${id}-other-help`}>
            Any unit that converts to {field.unit_symbol ?? field.unit}, written as{" "}
            <a href="https://ucum.org/ucum" target="_blank" rel="noreferrer">
              UCUM
            </a>{" "}
            codes are, such as <code>ug/mL</code> for μg/mL.
          </p>
        </div>
      ) : null}
    </Shell>
  );
}

function BoolInput({ field, draft, onChange, error, note, disabled }: InputProps) {
  const id = useId();
  const help = helpText(field, note);
  const options = [
    { value: "true", label: "Yes" },
    { value: "false", label: "No" },
    ...(field.required ? [] : [{ value: "", label: "Not set" }]),
  ];
  return (
    <fieldset
      className={error ? "field field-invalid" : "field"}
      aria-describedby={describedBy(error && `${id}-error`, help && `${id}-help`)}
      disabled={disabled}
    >
      <legend>
        {field.label}
        {field.required ? null : <span className="optional"> (optional)</span>}
      </legend>
      {error ? (
        <p className="field-error" id={`${id}-error`}>
          {error}
        </p>
      ) : null}
      <div className="choices choices-inline">
        {options.map((option) => (
          <label className="choice" key={option.value} htmlFor={`${id}-${option.value || "unset"}`}>
            <input
              type="radio"
              id={`${id}-${option.value || "unset"}`}
              name={id}
              value={option.value}
              checked={draft === option.value}
              onChange={() => {
                onChange(option.value);
              }}
            />
            <span>{option.label}</span>
          </label>
        ))}
      </div>
      {help ? (
        <p className="field-help" id={`${id}-help`}>
          {help}
        </p>
      ) : null}
    </fieldset>
  );
}

function EnumInput({ field, draft, onChange, error, note, disabled, itemLabel }: InputProps) {
  const id = useId();
  const help = helpText(field, note);
  return (
    <Shell
      id={id}
      label={itemLabel ?? field.label}
      optional={!field.required}
      error={error}
      help={help}
      hiddenLabel={itemLabel !== undefined}
    >
      <select
        id={id}
        value={draft as string}
        disabled={disabled}
        aria-invalid={error ? "true" : undefined}
        aria-describedby={describedBy(error && `${id}-error`, help && `${id}-help`)}
        aria-required={field.required || undefined}
        onChange={(event) => {
          onChange(event.target.value);
        }}
      >
        <option value="">{field.required ? "Choose one" : "Not set"}</option>
        {field.values.map((v) => (
          <option key={v} value={v}>
            {optionLabel(field, v)}
          </option>
        ))}
      </select>
    </Shell>
  );
}

function pickedLabel(picked: Picked): string {
  return picked.title && picked.title !== picked.hrid
    ? `${picked.title} (${picked.hrid})`
    : picked.hrid;
}

/** "a protein", "a protein or a DNA sequence", "a protein, a conjugate or a compound". */
function orList(words: string[]): string {
  return new Intl.ListFormat("en", { type: "disjunction" }).format(words);
}

/**
 * A picker's options: the chosen one first, where it stays as answers come, then those
 * found. Each answer's rows come after it, so none moves under someone choosing.
 */
function withChosen(found: Picked[], chosen: Picked | null): Picked[] {
  return chosen ? [chosen, ...found.filter((f) => f.id !== chosen.id)] : found;
}

/**
 * What a picker's list says under its options: that it's searching, until the answer for
 * the text in the box comes (it never shows another text's answer meanwhile), that the
 * search failed, or that nothing matches. A status, kept in place while the list is open,
 * so screen readers hear it change politely and focus stays in the box.
 */
function PickerMessage({
  found,
  shown,
  none,
}: {
  found: { data: unknown; isError: boolean };
  shown: number;
  none: string;
}) {
  const message =
    found.data === undefined
      ? found.isError
        ? "Couldn’t search. Try again in a moment."
        : "Searching…"
      : shown === 0
        ? none
        : "";
  return (
    <p className={message ? "listbox-empty" : "visually-hidden"} role="status">
      {message}
    </p>
  );
}

/** An entity as a reference picker lists it. One function, so options aren't rebuilt for
 * the same answer. */
function refOption(item: Picked) {
  return (
    <Item key={item.id} textValue={pickedLabel(item)}>
      <span>{item.title !== item.hrid ? item.title : item.hrid}</span>
      {item.title !== item.hrid ? <span className="muted"> {item.hrid}</span> : null}
    </Item>
  );
}

/**
 * A reference: a combo box that searches as people type, from React Aria's hooks, which
 * handle its keyboard, focus and screen reader announcements. It lists only the answer for
 * the text in the box: until that comes it says it's searching. A search can't be worked
 * out in the browser as people's can (see narrowNameable), and showing another text's
 * answer meanwhile would let the answer land between pressing and releasing, so a click
 * meant for one record chose another, or nothing.
 */
function RefInput({ field, types, draft, onChange, error, note, disabled, itemLabel }: InputProps) {
  const picked = draft as Picked | null;
  // Typed text belongs to the value it was typed over: when the value changes from outside
  // (a row removed above, someone else's change merged in), the new value's label shows.
  const [typing, setTyping] = useState({
    text: picked ? pickedLabel(picked) : "",
    over: picked?.id,
  });
  const text = typing.over === picked?.id ? typing.text : picked ? pickedLabel(picked) : "";
  // What was typed, not the chosen entity's label.
  const typed = picked && text === pickedLabel(picked) ? "" : text;
  // Asked for as typed, not deferred, so no render lists the answer for older text.
  const found = useQuery(pickerQuery(field.to, typed));
  const target =
    !field.to.length || field.to.includes("iki/core.Entity")
      ? "any entity"
      : orList(field.to.map((t) => inSentenceLabel(typeLabel(types, t))));
  const items = useMemo(
    () =>
      withChosen(
        (found.data ?? []).map((e) => ({ id: e.id, hrid: e.hrid, title: e.title })),
        picked,
      ),
    [found.data, picked],
  );
  const help = helpText(field, note) ?? `Type an ID or a name to find ${target}.`;
  const label = itemLabel ?? field.label;

  const inputRef = useRef<HTMLInputElement>(null);
  const listBoxRef = useRef<HTMLUListElement>(null);
  const popoverRef = useRef<HTMLDivElement>(null);
  const props = {
    label,
    description: help,
    errorMessage: error,
    isInvalid: Boolean(error),
    isRequired: field.required,
    isDisabled: disabled ?? false,
    validationBehavior: "aria" as const,
    items,
    children: refOption,
    inputValue: text,
    onInputChange: (value: string) => {
      setTyping({ text: value, over: value === "" ? undefined : picked?.id });
      if (value === "") onChange(null);
    },
    value: picked?.id ?? null,
    onChange: (key: Key | null) => {
      const chosen = items.find((i) => i.id === key) ?? null;
      onChange(chosen);
      if (chosen) setTyping({ text: pickedLabel(chosen), over: chosen.id });
    },
    menuTrigger: "focus" as const,
    allowsEmptyCollection: true,
  };
  const state = useComboBoxState(props);
  const { inputProps, listBoxProps, labelProps, descriptionProps, errorMessageProps } = useComboBox(
    { ...props, inputRef, listBoxRef, popoverRef },
    state,
  );
  return (
    <div className={error ? "field field-invalid" : "field"}>
      <label {...labelProps} className={itemLabel !== undefined ? "visually-hidden" : undefined}>
        {label}
        {field.required || itemLabel !== undefined ? null : (
          <span className="optional"> (optional)</span>
        )}
      </label>
      {error ? (
        <p {...errorMessageProps} className="field-error">
          {error}
        </p>
      ) : null}
      <div className="combobox">
        {/* Named by its <label for>, not aria-labelledby too: in Firefox, working out a name
            through aria-labelledby (as axe does) and then changing the form crashed the page. */}
        <input {...inputProps} aria-labelledby={undefined} ref={inputRef} spellCheck={false} />
      </div>
      <p {...descriptionProps} className="field-help">
        {help}
      </p>
      {state.isOpen ? (
        <Popover state={state} triggerRef={inputRef} popoverRef={popoverRef}>
          <ListBox {...listBoxProps} listBoxRef={listBoxRef} state={state} />
          <PickerMessage
            found={found}
            shown={state.collection.size}
            none={typed ? `Nothing matches “${typed}”.` : "None yet."}
          />
        </Popover>
      ) : null}
    </div>
  );
}

function Popover({
  state,
  triggerRef,
  popoverRef,
  children,
}: {
  state: ComboBoxState<Picked>;
  triggerRef: RefObject<HTMLInputElement | null>;
  popoverRef: RefObject<HTMLDivElement | null>;
  children: ReactNode;
}) {
  const { popoverProps } = usePopover(
    { triggerRef, popoverRef, placement: "bottom start", offset: 4, isNonModal: true },
    state,
  );
  // As wide as the field it belongs to, measured once it's laid out.
  const [width, setWidth] = useState<number>();
  useLayoutEffect(() => {
    setWidth(triggerRef.current?.offsetWidth);
  }, [triggerRef]);
  return (
    <Overlay>
      <div
        {...popoverProps}
        ref={popoverRef}
        className="popover"
        style={{ ...popoverProps.style, width }}
      >
        <DismissButton
          onDismiss={() => {
            state.close();
          }}
        />
        {children}
        <DismissButton
          onDismiss={() => {
            state.close();
          }}
        />
      </div>
    </Overlay>
  );
}

function ListBox({
  state,
  listBoxRef,
  ...props
}: AriaListBoxOptions<Picked> & {
  state: ComboBoxState<Picked>;
  listBoxRef: RefObject<HTMLUListElement | null>;
}) {
  const { listBoxProps } = useListBox(props, state, listBoxRef);
  return (
    <ul {...listBoxProps} ref={listBoxRef} className="listbox">
      {[...state.collection].map((item) => (
        <Option key={item.key} item={item} state={state} />
      ))}
    </ul>
  );
}

function Option({ item, state }: { item: Node<Picked>; state: ComboBoxState<Picked> }) {
  const ref = useRef<HTMLLIElement>(null);
  const { optionProps, isFocused, isSelected } = useOption({ key: item.key }, state, ref);
  return (
    <li
      {...optionProps}
      ref={ref}
      className="listbox-item"
      data-focused={isFocused || undefined}
      data-selected={isSelected || undefined}
    >
      {item.rendered}
    </li>
  );
}

/** A person, as a picker shows them: `hrid` holds their email address. */
function personText(picked: Picked): string {
  return picked.hrid && picked.hrid !== picked.title
    ? `${picked.title} (${picked.hrid})`
    : picked.title;
}

/** A person as a picker lists them (see refOption). */
function personOption(item: Picked) {
  return (
    <Item key={item.id} textValue={personText(item)}>
      <span>{item.title}</span>
      {item.hrid && item.hrid !== item.title ? <span className="muted"> {item.hrid}</span> : null}
    </Item>
  );
}

export interface PersonPickerProps {
  label: string;
  projectId: string;
  /** Only those holding this lab role in the project: the field's. */
  role?: string | null | undefined;
  roleLabel?: string | null | undefined;
  value: Picked | null;
  onChange: (picked: Picked | null) => void;
  error?: string | undefined;
  help?: string | undefined;
  required?: boolean;
  disabled?: boolean;
  /** In a list: the label screen readers hear, which the page doesn't show. */
  hiddenLabel?: boolean;
}

/**
 * Someone a record can name (ADR 0122): a combo box that finds, as people type, only those
 * who can be named in the project, holding the field's lab role if it has one. Nobody else
 * is listed, so nobody learns who else exists.
 */
export function PersonPicker({
  label,
  projectId,
  role,
  roleLabel,
  value,
  onChange,
  error,
  help,
  required = false,
  disabled = false,
  hiddenLabel = false,
}: PersonPickerProps) {
  const [typing, setTyping] = useState({
    text: value ? personText(value) : "",
    over: value?.id,
  });
  const text = typing.over === value?.id ? typing.text : value ? personText(value) : "";
  const typed = value && text === personText(value) ? "" : text;
  // Asked for as typed, not deferred: the suggestions are only ever those for the text in
  // the box (see nameableQuery), so they don't change as someone moves to choose one.
  const found = useQuery(nameableQuery(projectId, role, typed));
  const items = useMemo(
    () =>
      withChosen(
        (found.data ?? []).map((p) => ({ id: p.id, hrid: p.email, title: p.name })),
        value,
      ),
    [found.data, value],
  );
  const shownHelp =
    help ??
    (roleLabel
      ? `Type a name or email address: someone with the ${roleLabel} role in this project.`
      : "Type a name or email address: someone in this project.");

  const inputRef = useRef<HTMLInputElement>(null);
  const listBoxRef = useRef<HTMLUListElement>(null);
  const popoverRef = useRef<HTMLDivElement>(null);
  const props = {
    label,
    description: shownHelp,
    errorMessage: error,
    isInvalid: Boolean(error),
    isRequired: required,
    isDisabled: disabled,
    validationBehavior: "aria" as const,
    items,
    children: personOption,
    inputValue: text,
    onInputChange: (next: string) => {
      setTyping({ text: next, over: next === "" ? undefined : value?.id });
      if (next === "") onChange(null);
    },
    value: value?.id ?? null,
    onChange: (key: Key | null) => {
      const chosen = items.find((i) => i.id === key) ?? null;
      onChange(chosen);
      if (chosen) setTyping({ text: personText(chosen), over: chosen.id });
    },
    menuTrigger: "focus" as const,
    allowsEmptyCollection: true,
  };
  const state = useComboBoxState(props);
  const { inputProps, listBoxProps, labelProps, descriptionProps, errorMessageProps } = useComboBox(
    { ...props, inputRef, listBoxRef, popoverRef },
    state,
  );
  return (
    <div className={error ? "field field-invalid" : "field"}>
      <label {...labelProps} className={hiddenLabel ? "visually-hidden" : undefined}>
        {label}
        {required || hiddenLabel ? null : <span className="optional"> (optional)</span>}
      </label>
      {error ? (
        <p {...errorMessageProps} className="field-error">
          {error}
        </p>
      ) : null}
      <div className="combobox">
        {/* Named by its <label for>, as references' are (see RefInput). */}
        <input
          {...inputProps}
          aria-labelledby={undefined}
          ref={inputRef}
          spellCheck={false}
          autoComplete="off"
        />
      </div>
      <p {...descriptionProps} className="field-help">
        {shownHelp}
      </p>
      {state.isOpen ? (
        <Popover state={state} triggerRef={inputRef} popoverRef={popoverRef}>
          <ListBox {...listBoxProps} listBoxRef={listBoxRef} state={state} />
          <PickerMessage
            found={found}
            shown={state.collection.size}
            none={typed ? `Nobody here matches “${typed}”.` : "Nobody can be named here yet."}
          />
        </Popover>
      ) : null}
    </div>
  );
}

function PersonInput({
  field,
  draft,
  onChange,
  error,
  note,
  disabled,
  projectId,
  itemLabel,
}: InputProps) {
  return (
    <PersonPicker
      label={itemLabel ?? field.label}
      projectId={projectId}
      role={field.role}
      roleLabel={field.role_label}
      value={draft as Picked | null}
      onChange={onChange}
      error={error}
      help={helpText(field, note)}
      required={field.required}
      disabled={disabled ?? false}
      hiddenLabel={itemLabel !== undefined}
    />
  );
}

function inSentenceLabel(label: string): string {
  const article = /^[aeiou]/i.test(label) ? "an" : "a";
  const second = label.charAt(1);
  const lowered =
    second !== second.toLowerCase() ? label : label.charAt(0).toLowerCase() + label.slice(1);
  return `${article} ${lowered}`;
}

/** What a file field takes, as a sentence, or "" when it takes anything. */
function fileRules(schema: { "x-iki-media-types"?: string[]; "x-iki-max-size"?: number }): string {
  const allowed = schema["x-iki-media-types"] ?? [];
  const maxSize = schema["x-iki-max-size"];
  const rules = [
    allowed.length ? `${media.either(allowed)} files` : "",
    maxSize != null ? `up to ${formatSize(maxSize)}` : "",
  ]
    .filter(Boolean)
    .join(", ");
  return rules ? `It takes ${rules}.` : "";
}

/** A file: one uploaded from this computer, or one already in the project. In a list,
 * a chosen file is one line (the list says what it takes, once). */
function FileInput({
  field,
  type,
  draft,
  onChange,
  error,
  note,
  disabled,
  projectId,
  itemLabel,
}: InputProps) {
  const id = useId();
  const picked = draft as Picked | null;
  // In a list of files, the rules are its items'.
  const own = propertySchema(type, field.name);
  const schema = own.items ?? own;
  const files = useQuery(projectFilesQuery(projectId));
  const client = useQueryClient();
  const allowed = schema["x-iki-media-types"] ?? [];
  const maxSize = schema["x-iki-max-size"];
  const [progress, setProgress] = useState<Progress | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const transfer = useRef<AbortController | null>(null);
  const fits = (files.data ?? []).filter(
    (f) => media.allowed(f.media_type, allowed) && (maxSize == null || f.size <= maxSize),
  );
  const inList = itemLabel !== undefined;
  const help = inList
    ? undefined
    : helpText(field, [note, fileRules(schema)].filter(Boolean).join(" "));
  const shownError = problem ?? error;
  const label = itemLabel ?? field.label;

  async function send(file: File) {
    setProblem(null);
    if (allowed.length && !media.allowed(media.of(file), allowed)) {
      setProblem(`${file.name} is ${media.of(file)}: choose ${media.either(allowed)}.`);
      onChange(emptyDraft(field));
      return;
    }
    if (maxSize != null && file.size > maxSize) {
      setProblem(
        `${file.name} is ${formatSize(file.size)}: choose one up to ${formatSize(maxSize)}.`,
      );
      onChange(emptyDraft(field));
      return;
    }
    const controller = new AbortController();
    transfer.current = controller;
    onChange({ id: "", hrid: "", title: file.name, uploading: true });
    try {
      const made = await upload(file, projectId, setProgress, controller.signal);
      onChange({ id: made.id, hrid: "", title: made.name });
      void client.invalidateQueries({ queryKey: projectFilesQuery(projectId).queryKey });
    } catch (failure) {
      onChange(emptyDraft(field));
      if (!controller.signal.aborted) setProblem(uploadError(failure));
    } finally {
      transfer.current = null;
      setProgress(null);
    }
  }

  // Leaving the form stops what's still sending.
  useEffect(
    () => () => {
      transfer.current?.abort();
    },
    [],
  );

  // A file chosen with others, in a list: this input sends it.
  const started = useRef<File | null>(null);
  const given = picked?.file;
  useEffect(() => {
    if (given && started.current !== given) {
      started.current = given;
      void send(given);
    }
    // send is this render's; the file is what matters.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [given]);

  if (inList && picked && !picked.uploading && picked.id) {
    const size = files.data?.find((f) => f.id === picked.id)?.size;
    return (
      <p className="list-input-file">
        <span className="visually-hidden">{label}: </span>
        {picked.title}
        {size != null ? <span className="muted"> {formatSize(size)}</span> : null}
      </p>
    );
  }

  return (
    <fieldset
      className={shownError ? "field field-invalid" : "field"}
      aria-describedby={describedBy(shownError && `${id}-error`, help && `${id}-help`)}
      disabled={disabled}
    >
      <legend className={inList ? "visually-hidden" : undefined}>
        {label}
        {field.required || inList ? null : <span className="optional"> (optional)</span>}
      </legend>
      {shownError ? (
        <p className="field-error" id={`${id}-error`}>
          {shownError}
        </p>
      ) : null}
      {help ? (
        <p className="field-help" id={`${id}-help`}>
          {help}
        </p>
      ) : null}
      {picked?.uploading && progress ? (
        <div className="upload">
          {/* Screen readers hear each step, not every percent. */}
          <p className="visually-hidden" role="status">
            {progress.phase === "hashing"
              ? `Reading ${picked.title}`
              : progress.phase === "sending"
                ? `Uploading ${picked.title}`
                : `Checking ${picked.title}`}
          </p>
          <p aria-hidden="true">
            <UploadStatus name={picked.title} progress={progress} />
          </p>
          {progress.phase === "checking" ? (
            <progress aria-label={`Checking ${picked.title}`} />
          ) : (
            <progress
              aria-label={`${progress.phase === "hashing" ? "Reading" : "Uploading"} ${picked.title}`}
              max={progress.total || 1}
              value={progress.done}
            />
          )}
          {progress.phase !== "checking" ? (
            <Button
              variant="quiet"
              label={`Cancel uploading ${picked.title}`}
              onClick={() => {
                transfer.current?.abort();
              }}
            >
              Cancel
            </Button>
          ) : null}
        </div>
      ) : (
        <div className="file-choice">
          <div className="field-sub">
            <label htmlFor={`${id}-upload`}>Upload a file</label>
            <input
              id={`${id}-upload`}
              type="file"
              accept={media.accept(allowed)}
              aria-invalid={shownError ? "true" : undefined}
              aria-describedby={describedBy(shownError && `${id}-error`)}
              onChange={(event) => {
                const chosen = event.target.files?.[0];
                event.target.value = "";
                if (chosen) void send(chosen);
              }}
            />
          </div>
          <div className="field-sub">
            <label htmlFor={`${id}-existing`}>Or one already in the project</label>
            <select
              id={`${id}-existing`}
              value={picked && !picked.uploading ? picked.id : ""}
              onChange={(event) => {
                setProblem(null);
                const chosen = fits.find((f) => f.id === event.target.value);
                onChange(
                  chosen ? { id: chosen.id, hrid: "", title: chosen.name } : emptyDraft(field),
                );
              }}
            >
              <option value="">{files.isPending ? "Loading files…" : "No file"}</option>
              {picked && !picked.uploading && !fits.some((f) => f.id === picked.id) ? (
                <option value={picked.id}>{picked.title}</option>
              ) : null}
              {fits.map((f) => (
                <option key={f.id} value={f.id}>
                  {f.name} ({formatSize(f.size)})
                </option>
              ))}
            </select>
          </div>
        </div>
      )}
    </fieldset>
  );
}

export function UploadStatus({ name, progress }: { name: string; progress: Progress }) {
  if (progress.phase === "checking") return <>Iki is checking {name}’s bytes…</>;
  const percent = progress.total ? Math.floor((progress.done / progress.total) * 100) : 100;
  return (
    <>
      {progress.phase === "hashing" ? "Reading" : "Uploading"} {name}: {percent}% (
      {formatSize(progress.done)} of {formatSize(progress.total)})
    </>
  );
}

function ListInput(props: InputProps) {
  const { field, draft, onChange, error, note, disabled, type } = props;
  const id = useId();
  const items = draft as Draft[];
  const item: FieldDef = { ...field, type: field.items ?? "string", required: false };
  const rows = field.items === "row";
  const noun = rows ? "row" : "item";
  const files = field.items === "file";
  const itemSchema = propertySchema(type, field.name).items ?? {};
  // What each file may be, said once for the list rather than for each file.
  const help = helpText(
    field,
    [note, files ? fileRules(itemSchema) : ""].filter(Boolean).join(" "),
  );
  // Items change one by one as their files finish uploading, so each change starts from
  // the list as the last one left it, not as this render saw it.
  const latest = useRef(items);
  useLayoutEffect(() => {
    latest.current = items;
  }, [items]);
  const change = (next: Draft[]) => {
    latest.current = next;
    onChange(next);
  };
  const most = propertySchema(type, field.name).maxItems;
  const [tooMany, setTooMany] = useState<string | null>(null);
  const shownError = tooMany ?? error;
  const list = useRef<HTMLDivElement>(null);
  const focusItem = (index: number) => {
    // After React draws the new row.
    requestAnimationFrame(() => {
      list.current
        ?.querySelectorAll<HTMLElement>("[data-list-item]")
        [index]?.querySelector<HTMLElement>("input, select, textarea")
        ?.focus();
    });
  };
  return (
    <fieldset
      className={shownError ? "field field-invalid" : "field"}
      aria-describedby={describedBy(shownError && `${id}-error`, help && `${id}-help`)}
    >
      <legend>
        {field.label}
        {field.required ? null : <span className="optional"> (optional)</span>}
      </legend>
      {shownError ? (
        <p className="field-error" id={`${id}-error`}>
          {shownError}
        </p>
      ) : null}
      {help ? (
        <p className="field-help" id={`${id}-help`}>
          {help}
        </p>
      ) : null}
      <div className="list-input" ref={list}>
        {items.map((d, index) => (
          // Items have no identity but their place in the list.
          <div className="list-input-item" data-list-item key={index}>
            <FieldInput
              {...props}
              field={item}
              draft={d}
              error={rows ? props.errors?.[`values/${field.name}/${String(index)}`] : undefined}
              note={undefined}
              itemLabel={`${field.label}, ${noun} ${String(index + 1)}`}
              path={`values/${field.name}/${String(index)}`}
              onChange={(next) => {
                change(latest.current.map((old, i) => (i === index ? next : old)));
              }}
            />
            <Button
              variant="quiet"
              disabled={disabled ?? false}
              label={
                files && (d as Picked | null)?.id
                  ? `Remove ${(d as Picked).title}`
                  : `Remove ${field.label}, ${noun} ${String(index + 1)}`
              }
              onClick={() => {
                setTooMany(null);
                change(latest.current.filter((_, i) => i !== index));
                focusItem(Math.max(0, index - 1));
              }}
            >
              Remove
            </Button>
          </div>
        ))}
      </div>
      <div className="list-input-add">
        <Button
          disabled={disabled ?? false}
          label={rows ? `Add a row to ${field.label}` : `Add ${field.label}`}
          onClick={() => {
            change([...latest.current, newItem(field, type)]);
            focusItem(items.length);
          }}
        >
          Add
        </Button>
        {files ? (
          <div className="field-sub">
            <label htmlFor={`${id}-several`}>Or upload several at once</label>
            <input
              id={`${id}-several`}
              type="file"
              multiple
              disabled={disabled ?? false}
              accept={media.accept(itemSchema["x-iki-media-types"] ?? [])}
              onChange={(event) => {
                const chosen = [...(event.target.files ?? [])];
                event.target.value = "";
                if (!chosen.length) return;
                const room = most == null ? chosen.length : most - latest.current.length;
                if (chosen.length > room) {
                  setTooMany(
                    `${field.label} takes at most ${String(most)} files: choose ${
                      room > 0 ? `up to ${String(room)} more` : "none more, or remove some"
                    }.`,
                  );
                  return;
                }
                setTooMany(null);
                change([
                  ...latest.current,
                  ...chosen.map((f) => ({
                    id: "",
                    hrid: "",
                    title: f.name,
                    uploading: true,
                    file: f,
                  })),
                ]);
              }}
            />
          </div>
        ) : null}
      </div>
    </fieldset>
  );
}
