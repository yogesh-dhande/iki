/**
 * One input for each kind of entity field, wired as the design system's
 * fields are: a label, errors and help linked through `aria-describedby`,
 * `aria-invalid` on invalid inputs. Numbers are text inputs with a decimal
 * keyboard (the browser's number input changes on scroll and reads "1,5"
 * differently by locale). References are picked with a combo box that
 * searches as people type.
 */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  useDeferredValue,
  useEffect,
  useId,
  useLayoutEffect,
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
  OTHER_UNIT,
  propertySchema,
  type Draft,
  type Picked,
  type QuantityDraft,
} from "./entity-values";
import { media } from "./media";
import { upload, uploadError, type Progress } from "./uploads";
import { Button } from "./ui";

export interface InputProps {
  field: FieldDef;
  type: EntityType;
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
    case "file":
      return <FileInput {...props} />;
    default:
      return <TextInput {...props} />;
  }
}

function TextInput({ field, draft, onChange, error, note, disabled, type, itemLabel }: InputProps) {
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
            {v}
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

/**
 * A reference: a combo box that searches as people type, from React Aria's hooks, which
 * handle its keyboard, focus and screen reader announcements.
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
  const search = useDeferredValue(typed);
  const found = useQuery(pickerQuery(field.to ?? undefined, search));
  const target =
    field.to === "core.Entity" ? "any entity" : inSentenceLabel(typeLabel(types, field.to ?? ""));
  const items = (found.data ?? []).map((e) => ({ id: e.id, hrid: e.hrid, title: e.title }));
  if (picked && !items.some((i) => i.id === picked.id)) items.unshift(picked);
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
    children: (item: Picked) => (
      <Item key={item.id} textValue={pickedLabel(item)}>
        <span>{item.title !== item.hrid ? item.title : item.hrid}</span>
        {item.title !== item.hrid ? <span className="muted"> {item.hrid}</span> : null}
      </Item>
    ),
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
          {state.collection.size === 0 ? (
            <p className="listbox-empty">
              {found.isFetching
                ? "Searching…"
                : typed
                  ? `Nothing matches “${typed}”.`
                  : "None yet."}
            </p>
          ) : null}
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

function inSentenceLabel(label: string): string {
  const article = /^[aeiou]/i.test(label) ? "an" : "a";
  const second = label.charAt(1);
  const lowered =
    second !== second.toLowerCase() ? label : label.charAt(0).toLowerCase() + label.slice(1);
  return `${article} ${lowered}`;
}

/** A file: one uploaded from this computer, or one already in the project. */
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
  const schema = propertySchema(type, field.name);
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
  const rules = [
    allowed.length ? `${media.either(allowed)} files` : "",
    maxSize != null ? `up to ${formatSize(maxSize)}` : "",
  ]
    .filter(Boolean)
    .join(", ");
  const help = helpText(field, [note, rules ? `It takes ${rules}.` : ""].filter(Boolean).join(" "));
  const shownError = problem ?? error;
  const label = itemLabel ?? field.label;

  async function send(file: File) {
    setProblem(null);
    if (allowed.length && !media.allowed(media.of(file), allowed)) {
      setProblem(`${file.name} is ${media.of(file)}: choose ${media.either(allowed)}.`);
      return;
    }
    if (maxSize != null && file.size > maxSize) {
      setProblem(
        `${file.name} is ${formatSize(file.size)}: choose one up to ${formatSize(maxSize)}.`,
      );
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

  return (
    <fieldset
      className={shownError ? "field field-invalid" : "field"}
      aria-describedby={describedBy(shownError && `${id}-error`, help && `${id}-help`)}
      disabled={disabled}
    >
      <legend className={itemLabel !== undefined ? "visually-hidden" : undefined}>
        {label}
        {field.required || itemLabel !== undefined ? null : (
          <span className="optional"> (optional)</span>
        )}
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

function UploadStatus({ name, progress }: { name: string; progress: Progress }) {
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
  const { field, draft, onChange, error, note, disabled } = props;
  const id = useId();
  const items = draft as Draft[];
  const item: FieldDef = { ...field, type: field.items ?? "string", required: false };
  const help = helpText(field, note);
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
      className={error ? "field field-invalid" : "field"}
      aria-describedby={describedBy(error && `${id}-error`, help && `${id}-help`)}
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
              error={undefined}
              note={undefined}
              itemLabel={`${field.label}, item ${String(index + 1)}`}
              onChange={(next) => {
                onChange(items.map((old, i) => (i === index ? next : old)));
              }}
            />
            <Button
              variant="quiet"
              disabled={disabled ?? false}
              label={`Remove ${field.label}, item ${String(index + 1)}`}
              onClick={() => {
                onChange(items.filter((_, i) => i !== index));
                focusItem(Math.max(0, index - 1));
              }}
            >
              Remove
            </Button>
          </div>
        ))}
      </div>
      <Button
        disabled={disabled ?? false}
        label={`Add ${field.label}`}
        onClick={() => {
          onChange([...items, emptyDraft(item)]);
          focusItem(items.length);
        }}
      >
        Add
      </Button>
    </fieldset>
  );
}
