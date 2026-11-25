/**
 * One input for each kind of entity field, wired as the design system's
 * fields are: a label, errors and help linked through `aria-describedby`,
 * `aria-invalid` on invalid inputs. Numbers are text inputs with a decimal
 * keyboard (the browser's number input changes on scroll and reads "1,5"
 * differently by locale). References are picked with a combo box that
 * searches as people type; people with one that finds only those the field can name.
 */

import "@ikisci/science/quantity-input";
import "@ikisci/science/sequence-input";
import type { QuantityInputData } from "@ikisci/science/quantity-input";
import type { SequenceInputData } from "@ikisci/science/sequence-input";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createElement,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
  type RefObject,
} from "react";
import {
  DismissButton,
  useComboBox,
  useListBox,
  useOption,
  type AriaListBoxOptions,
} from "react-aria";
import { Item, useComboBoxState, type ComboBoxState, type Key, type Node } from "react-stately";

import {
  formatSize,
  pickerQuery,
  typeLabel,
  type Entity,
  type EntityType,
  type FieldDef,
} from "./entities";
import {
  emptyDraft,
  newItem,
  propertySchema,
  rowFields,
  type Draft,
  type Picked,
  type RowDraft,
  type Schemaed,
  type QuantityDraft,
} from "./entity-values";
import { FilePicker, takesText } from "./file-picker";
import { UploadStatus } from "./file-upload";
import { FILES } from "./files";
import { optionLabel } from "./filters";
import { describeHelm, isProblem, useHelmReading } from "./helm";
import { useMedia } from "./media";
import { nameableQuery } from "./people";
import { progressLabel, upload, uploadError, type Progress } from "./uploads";
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
          field={roleChoice(field, cell)}
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

/**
 * A row's role, where its roles are every role it may have (ADR 0164: a subtype's, or a
 * string's that names them): a choice of them, by their labels, rather than text to type.
 */
function roleChoice(field: FieldDef, cell: FieldDef): FieldDef {
  const row = field.row;
  if (!row?.closed || row.by !== cell.name || cell.type !== "string") return cell;
  return {
    ...cell,
    type: "enum",
    values: row.roles.map((r) => r.name),
    states: row.roles.map((r) => ({ name: r.name, label: r.label })) as FieldDef["states"],
  };
}

/** Expose the native input to the containing field's label, help and validation. */
function fieldOptions(id: string, props: InputProps, error = props.error) {
  const help = helpText(props.field, props.note);
  const description = describedBy(error && `${id}-error`, help && `${id}-help`);
  return {
    inputId: id,
    label: props.itemLabel ?? props.field.label,
    required: props.field.required,
    disabled: !!props.disabled,
    invalid: !!error,
    ...(description && { describedBy: description }),
  };
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
function HelmInput(props: InputProps) {
  const { field, draft, onChange, error, note, itemLabel } = props;
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
      {createElement("iki-sequence-input", {
        data: {
          ...fieldOptions(id, props, shown),
          value: text,
          format: "helm",
          reading: reading && !isProblem(reading) ? describeHelm(reading) : "",
        } satisfies SequenceInputData,
        "onvalue-change": (event: CustomEvent<string>) => {
          onChange(event.detail);
        },
      })}
    </Shell>
  );
}

function PlainTextInput(props: InputProps) {
  const { field, draft, onChange, error, note, disabled, type, itemLabel } = props;
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
      input = sequence ? (
        createElement("iki-sequence-input", {
          data: {
            ...fieldOptions(id, props),
            value: (draft as string | null) ?? "",
          } satisfies SequenceInputData,
          "onvalue-change": (event: CustomEvent<string>) => {
            onChange(event.detail);
          },
        })
      ) : (
        <input type="text" autoComplete="off" {...shared} />
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

function QuantityInput(props: InputProps) {
  const { field, draft, onChange, error, note, itemLabel } = props;
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
      {createElement("iki-quantity-input", {
        data: {
          ...fieldOptions(id, props),
          value: draft as QuantityDraft,
          units: field.input_units,
          referenceUnit: field.unit_symbol ?? field.unit ?? "",
        } satisfies QuantityInputData,
        "onvalue-change": (event: CustomEvent<QuantityDraft>) => {
          onChange(event.detail);
        },
      })}
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
 * Escape in a picker's box, as in WAI-ARIA's combo box pattern and GitHub's and Slack's
 * pickers: with the list open, it closes the list and keeps what's typed; with it closed,
 * it puts back what's chosen (its label), or clears the box if nothing is, as leaving the
 * box does. Either way the key goes no further, so a dialog the picker is in stays open;
 * once there's nothing to undo, it does, and closes the dialog. React Aria's own Escape
 * would put the chosen value back at once, and with nothing chosen, close a list that
 * opened again straight away for the text still in the box.
 */
function withEscape(
  state: ComboBoxState<Picked>,
  shown: string,
  onKeyDown: ((event: KeyboardEvent<HTMLInputElement>) => void) | undefined,
) {
  return (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Escape" && (state.isOpen || state.inputValue !== shown)) {
      event.preventDefault();
      event.stopPropagation();
      // Closed as it is: what's typed stays, and typing more opens it again.
      if (state.isOpen) state.toggle();
      // Committed: `onChange` gets what's chosen, or nothing, and the box shows it.
      else state.revert();
      return;
    }
    onKeyDown?.(event);
  };
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
      {item.project ? <span className="muted"> · {item.project}</span> : null}
    </Item>
  );
}

/** A picker's answer as its options: each record that shares its ID with another (one
 * registry entry's records in several projects) says whose it is. */
function pickable(found: readonly Entity[]): Picked[] {
  const shared = new Map<string, number>();
  for (const e of found) shared.set(e.hrid, (shared.get(e.hrid) ?? 0) + 1);
  return found.map((e) => ({
    id: e.id,
    hrid: e.hrid,
    title: e.title,
    ...(!e.locked && e.can.edit_reason_required ? { editReasonRequired: true } : {}),
    ...((shared.get(e.hrid) ?? 0) > 1 && e.owner_project ? { project: e.owner_project.name } : {}),
  }));
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
  const items = useMemo(() => withChosen(pickable(found.data ?? []), picked), [found.data, picked]);
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
    // On choosing, and on committing (Escape with the list closed, or leaving the box):
    // the box shows what's chosen, or nothing, never text that chose nothing.
    onChange: (key: Key | null) => {
      const chosen = items.find((i) => i.id === key) ?? null;
      onChange(chosen);
      setTyping(
        chosen ? { text: pickedLabel(chosen), over: chosen.id } : { text: "", over: undefined },
      );
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
        <input
          {...inputProps}
          onKeyDown={withEscape(state, picked ? pickedLabel(picked) : "", inputProps.onKeyDown)}
          aria-labelledby={undefined}
          ref={inputRef}
          spellCheck={false}
        />
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
      <p {...descriptionProps} className="field-help">
        {help}
      </p>
    </div>
  );
}

/** The most of a picker's list that shows before it scrolls: the stylesheet's 18rem. */
const LIST_HEIGHT_REM = 18;
/** Room a list keeps from the edge of what's visible, and the least it's given. */
const LIST_MARGIN = 12;
const LIST_MIN_HEIGHT = 120;

/** Where a field's list has room: below it unless there's more above. Measured against
 * the window and any scrolling box the field is in (a dialog's body), once, when it opens. */
function listRoom(field: HTMLElement): { above: boolean; maxHeight: number } {
  const rem = parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;
  const tallest = LIST_HEIGHT_REM * rem;
  const box = field.getBoundingClientRect();
  let top = 0;
  let bottom = window.visualViewport?.height ?? window.innerHeight;
  for (let up = field.parentElement; up; up = up.parentElement) {
    const { overflowY } = getComputedStyle(up);
    if (overflowY === "visible") continue;
    const clip = up.getBoundingClientRect();
    top = Math.max(top, clip.top);
    bottom = Math.min(bottom, clip.bottom);
  }
  const below = bottom - box.bottom - LIST_MARGIN;
  const aboveRoom = box.top - top - LIST_MARGIN;
  const above = below < tallest && aboveRoom > below;
  return {
    above,
    maxHeight: Math.max(LIST_MIN_HEIGHT, Math.min(tallest, above ? aboveRoom : below)),
  };
}

/**
 * A picker's list, in the field just after its box (as in WAI-ARIA's combo box pattern), so
 * it moves with the field as the page or a dialog scrolls, and scrolling never closes it.
 * React Aria's popover, portalled to the page, closed whenever the page scrolled; the box
 * kept the typed text, so it opened again at once with new rows, and a press that came as
 * the page scrolled (a trackpad still coasting, or the browser bringing the row into view)
 * landed on a row that was gone and chose nothing. Its side is chosen once, when it opens,
 * so rows never jump while someone points at one. A press in it that isn't on an option
 * (its scroll bar, its message) keeps focus in the box.
 */
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
  const [room, setRoom] = useState<{ above: boolean; maxHeight: number }>();
  useLayoutEffect(() => {
    if (triggerRef.current) setRoom(listRoom(triggerRef.current));
    // Not a control itself: a press anywhere in it but an option (whose own press keeps
    // focus) mustn't take focus from the box, which would close the list.
    const list = popoverRef.current;
    const keepFocus = (event: MouseEvent) => {
      event.preventDefault();
    };
    list?.addEventListener("mousedown", keepFocus);
    return () => list?.removeEventListener("mousedown", keepFocus);
  }, [triggerRef, popoverRef]);
  return (
    <div
      ref={popoverRef}
      className="popover"
      data-placement={room?.above ? "top" : "bottom"}
      style={room ? { maxHeight: room.maxHeight } : undefined}
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
    // As a reference's (see RefInput): the box shows who's chosen, or nothing.
    onChange: (key: Key | null) => {
      const chosen = items.find((i) => i.id === key) ?? null;
      onChange(chosen);
      setTyping(
        chosen ? { text: personText(chosen), over: chosen.id } : { text: "", over: undefined },
      );
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
          onKeyDown={withEscape(state, value ? personText(value) : "", inputProps.onKeyDown)}
          aria-labelledby={undefined}
          ref={inputRef}
          spellCheck={false}
          autoComplete="off"
        />
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
      <p {...descriptionProps} className="field-help">
        {shownHelp}
      </p>
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
  const client = useQueryClient();
  const media = useMedia();
  const allowed = schema["x-iki-media-types"] ?? [];
  const maxSize = schema["x-iki-max-size"];
  const [progress, setProgress] = useState<Progress | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [picking, setPicking] = useState(false);
  const transfer = useRef<AbortController | null>(null);
  const inList = itemLabel !== undefined;
  const help = inList
    ? undefined
    : helpText(
        field,
        [note, takesText({ accepts: allowed, maxSize }, media)].filter(Boolean).join(" "),
      );
  const shownError = problem ?? error;
  const label = itemLabel ?? field.label;

  async function send(file: File, acquiredAt = "") {
    setProblem(null);
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
      const made = await upload(
        file,
        projectId,
        setProgress,
        controller.signal,
        allowed,
        acquiredAt,
      );
      onChange({ id: made.id, hrid: "", title: made.name, size: made.size });
      void client.invalidateQueries({ queryKey: FILES });
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
      void send(given, picked.acquiredAt);
    }
    // send is this render's; the file is what matters.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [given]);

  const chosen = picked && !picked.uploading && picked.id ? picked : null;
  if (inList && chosen) {
    return (
      <p className="list-input-file">
        <span className="visually-hidden">{label}: </span>
        {chosen.title}
        {chosen.size != null ? <span className="muted"> {formatSize(chosen.size)}</span> : null}
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
      {/* Said when a file is chosen, or once its upload is checked. */}
      <p className="visually-hidden" role="status">
        {chosen ? `Chosen: ${chosen.title}` : ""}
      </p>
      {picked?.uploading && progress ? (
        <div className="upload">
          {/* Screen readers hear each step, not every percent. */}
          <p className="visually-hidden" role="status">
            {progressLabel(progress, picked.title)}
          </p>
          <p aria-hidden="true">
            <UploadStatus name={picked.title} progress={progress} />
          </p>
          {progress.phase === "checking" || progress.phase === "scanning" ? (
            <progress aria-label={progressLabel(progress, picked.title)} />
          ) : (
            <progress
              aria-label={`${progress.phase === "hashing" ? "Reading" : "Uploading"} ${picked.title}`}
              max={progress.total || 1}
              value={progress.done}
            />
          )}
          {progress.phase !== "checking" && progress.phase !== "scanning" ? (
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
          {chosen ? (
            <p className="file-chosen">
              <span className="visually-hidden">Chosen: </span>
              <span className="file-chosen-name">{chosen.title}</span>
              {chosen.size != null ? (
                <span className="muted"> {formatSize(chosen.size)}</span>
              ) : null}
              <Button
                variant="quiet"
                label={`Remove ${chosen.title}`}
                onClick={() => {
                  setProblem(null);
                  onChange(emptyDraft(field));
                }}
              >
                Remove
              </Button>
            </p>
          ) : null}
          <div className="field-sub">
            <label htmlFor={`${id}-upload`}>{chosen ? "Upload another" : "Upload a file"}</label>
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
            <Button
              onClick={() => {
                setPicking(true);
              }}
            >
              {chosen ? "Choose another already here" : "Or choose one already here"}
            </Button>
          </div>
        </div>
      )}
      <FilePicker
        isOpen={picking}
        onClose={() => {
          setPicking(false);
        }}
        projectId={projectId}
        label={label}
        takes={{ accepts: allowed, maxSize }}
        chosen={chosen ? [chosen.id] : []}
        onChoose={([file]) => {
          if (!file) return;
          setProblem(null);
          onChange({ id: file.id, hrid: "", title: file.name, size: file.size });
        }}
        onUpload={([file], acquiredAt) => {
          if (file) void send(file, acquiredAt);
        }}
      />
    </fieldset>
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
  const media = useMedia(files);
  const itemSchema = propertySchema(type, field.name).items ?? {};
  const takes = {
    accepts: itemSchema["x-iki-media-types"] ?? [],
    maxSize: itemSchema["x-iki-max-size"],
  };
  // What each file may be, said once for the list rather than for each file.
  const help = helpText(
    field,
    [note, files ? takesText(takes, media) : ""].filter(Boolean).join(" "),
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
  const [picking, setPicking] = useState(false);
  /** Files chosen from this computer: each item's input uploads its own. */
  const addFiles = (chosen: File[], acquiredAt = "") => {
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
        acquiredAt,
      })),
    ]);
  };
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
              accept={media.accept(takes.accepts)}
              onChange={(event) => {
                const chosen = [...(event.target.files ?? [])];
                event.target.value = "";
                if (chosen.length) addFiles(chosen);
              }}
            />
          </div>
        ) : null}
        {files ? (
          <Button
            disabled={disabled ?? false}
            onClick={() => {
              setPicking(true);
            }}
          >
            Choose files already here
          </Button>
        ) : null}
      </div>
      {files ? (
        <FilePicker
          isOpen={picking}
          onClose={() => {
            setPicking(false);
          }}
          projectId={props.projectId}
          label={field.label}
          takes={takes}
          multiple
          room={most == null ? undefined : Math.max(0, most - items.length)}
          chosen={items.flatMap((d) => ((d as Picked | null)?.id ? [(d as Picked).id] : []))}
          onChoose={(picked) => {
            setTooMany(null);
            change([
              ...latest.current,
              ...picked.map((f) => ({ id: f.id, hrid: "", title: f.name, size: f.size })),
            ]);
          }}
          onUpload={addFiles}
        />
      ) : null}
    </fieldset>
  );
}

/**
 * A record chosen outside a form, as a reference field's: what a draw went into, say. It's
 * the reference input, given a field of its own to search `to`'s types.
 */
export function RecordPicker({
  name,
  label,
  to = [],
  types,
  value,
  onChange,
  error,
  help,
}: {
  name: string;
  label: string;
  to?: string[];
  types: EntityType[];
  value: Picked | null;
  onChange: (picked: Picked | null) => void;
  error?: string | undefined;
  help?: string | undefined;
}) {
  const field = {
    name,
    label,
    type: "ref",
    to,
    required: false,
    description: null,
  } as unknown as FieldDef;
  return (
    <RefInput
      field={field}
      type={{ values_schema: {} }}
      types={types}
      draft={value}
      onChange={(d) => {
        onChange(d as Picked | null);
      }}
      error={error}
      note={help}
      projectId=""
    />
  );
}
