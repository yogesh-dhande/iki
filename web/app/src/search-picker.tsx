/**
 * One combo box that finds what's chosen as people type: a record a reference names,
 * someone a record can name, one of a long lab list's options, or a record chosen outside
 * a form. It's React Aria's hooks, which handle its keyboard, focus and screen reader
 * announcements, with Iki's own Escape, list placement and status line. The inputs that
 * use it say only where it finds and how each one reads, so a fix here reaches every
 * picker.
 *
 * It finds in one of two ways: with a server search for what's typed (`query`), or by
 * narrowing options it has already (`options`), of which it shows the first matches
 * (SHOWN_MATCHES) and says how many there are.
 */

import { useQuery, type QueryKey, type UseQueryOptions } from "@tanstack/react-query";
import {
  useCallback,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type Dispatch,
  type KeyboardEvent,
  type ReactNode,
  type RefObject,
  type SetStateAction,
} from "react";
import {
  DismissButton,
  useComboBox,
  useListBox,
  useOption,
  type AriaListBoxOptions,
} from "react-aria";
import { Item, useComboBoxState, type ComboBoxState, type Key, type Node } from "react-stately";

import { narrowText, SHOWN_MATCHES } from "./lab-lists";

/** Anything a picker offers: it's told apart from the others by its ID. */
export interface Pickable {
  id: string;
}

/** A server search: its answer for what's typed. */
export interface QueryFinder<T extends Pickable, Q, K extends QueryKey> {
  query: (typed: string) => UseQueryOptions<Q, Error, T[], K>;
}

/** Options the picker has already, in order, and which of them `typed` matches. */
export interface OptionsFinder<T extends Pickable> {
  options: readonly T[];
  match: (options: readonly T[], typed: string) => T[];
}

export interface SearchPickerProps<T extends Pickable, Q, K extends QueryKey> {
  label: string;
  help: string;
  error?: string | undefined;
  required?: boolean | undefined;
  disabled?: boolean | undefined;
  /** In a list: the label screen readers hear, which the page doesn't show. */
  hiddenLabel?: boolean | undefined;
  value: T | null;
  onChange: (value: T | null) => void;
  find: QueryFinder<T, Q, K> | OptionsFinder<T>;
  /** How one reads in the box once it's chosen, and to screen readers in the list. */
  textOf: (item: T) => string;
  /** How one shows in the list. A function of the module, not one made while rendering,
   * so the list isn't built again for the same answer. */
  render: (item: T) => ReactNode;
  /** What the list says when it has none: for what's typed, or with nothing typed (""). */
  emptyText: (typed: string) => string;
}

/** The text in a picker's box, and the value it was typed over. */
interface Typing {
  text: string;
  over: string | undefined;
}

/** What the list holds, and the line under it. */
interface Found<T> {
  items: T[];
  message: string;
  /** The message says there are more than it shows. */
  more?: boolean;
}

/**
 * A picker's options: the chosen one first, where it stays as answers come, then those
 * found. Each answer's rows come after it, so none moves under someone choosing.
 */
function withChosen<T extends Pickable>(found: readonly T[], chosen: T | null): T[] {
  return chosen ? [chosen, ...found.filter((f) => f.id !== chosen.id)] : [...found];
}

/**
 * A combo box that finds as people type. It lists only what's found for the text in the
 * box: a server search says it's searching until that answer comes. Searches can't all be
 * worked out in the browser as people's can (see narrowNameable), and showing another
 * text's answer meanwhile would let the answer land between pressing and releasing, so a
 * click meant for one record chose another, or nothing.
 */
export function SearchPicker<T extends Pickable, Q = T[], K extends QueryKey = QueryKey>(
  props: SearchPickerProps<T, Q, K>,
) {
  const { value, textOf, find } = props;
  // Typed text belongs to the value it was typed over: when the value changes from outside
  // (a row removed above, someone else's change merged in), the new value's text shows.
  const [typing, setTyping] = useState<Typing>({
    text: value ? textOf(value) : "",
    over: value?.id,
  });
  const text = typing.over === value?.id ? typing.text : value ? textOf(value) : "";
  // What was typed, not the chosen one's text.
  const typed = value && text === textOf(value) ? "" : text;
  const box = { ...props, text, typed, setTyping };
  return "query" in find ? <Searched {...box} find={find} /> : <Narrowed {...box} find={find} />;
}

interface BoxProps<T extends Pickable, Q, K extends QueryKey> extends SearchPickerProps<T, Q, K> {
  text: string;
  typed: string;
  setTyping: Dispatch<SetStateAction<Typing>>;
}

/** Found by the server: asked for as typed, not deferred, so no render lists the answer
 * for older text. */
function Searched<T extends Pickable, Q, K extends QueryKey>(
  props: BoxProps<T, Q, K> & { find: QueryFinder<T, Q, K> },
) {
  const { find, typed, value, emptyText } = props;
  const answer = useQuery(find.query(typed));
  const items = useMemo(() => withChosen(answer.data ?? [], value), [answer.data, value]);
  const message =
    answer.data === undefined
      ? answer.isError
        ? "Couldn’t search. Try again in a moment."
        : "Searching…"
      : items.length === 0
        ? emptyText(typed)
        : "";
  return <PickerBox {...props} found={{ items, message }} />;
}

/** Narrowed in the browser: the first matches, and the chosen one. */
function Narrowed<T extends Pickable, Q, K extends QueryKey>(
  props: BoxProps<T, Q, K> & { find: OptionsFinder<T> },
) {
  const { find, typed, value, emptyText } = props;
  const { options, match } = find;
  const matched = useMemo(() => match(options, typed), [match, options, typed]);
  const items = useMemo(() => withChosen(matched.slice(0, SHOWN_MATCHES), value), [matched, value]);
  const more = matched.length > SHOWN_MATCHES;
  const message = more
    ? narrowText(SHOWN_MATCHES, matched.length)
    : items.length === 0
      ? emptyText(typed)
      : "";
  return <PickerBox {...props} found={{ items, message, more }} />;
}

function PickerBox<T extends Pickable, Q, K extends QueryKey>({
  label,
  help,
  error,
  required = false,
  disabled = false,
  hiddenLabel = false,
  value,
  onChange,
  textOf,
  render,
  text,
  setTyping,
  found,
}: BoxProps<T, Q, K> & { found: Found<T> }) {
  const { items } = found;
  const inputRef = useRef<HTMLInputElement>(null);
  const listBoxRef = useRef<HTMLUListElement>(null);
  const popoverRef = useRef<HTMLDivElement>(null);
  const children = useCallback(
    (item: T) => (
      <Item key={item.id} textValue={textOf(item)}>
        {render(item)}
      </Item>
    ),
    [textOf, render],
  );
  const props = {
    label,
    description: help,
    errorMessage: error,
    isInvalid: Boolean(error),
    isRequired: required,
    isDisabled: disabled,
    validationBehavior: "aria" as const,
    items,
    children,
    inputValue: text,
    onInputChange: (next: string) => {
      setTyping({ text: next, over: next === "" ? undefined : value?.id });
      if (next === "") onChange(null);
    },
    value: value?.id ?? null,
    // On choosing, and on committing (Escape with the list closed, or leaving the box):
    // the box shows what's chosen, or nothing, never text that chose nothing.
    onChange: (key: Key | null) => {
      const chosen = items.find((i) => i.id === key) ?? null;
      onChange(chosen);
      setTyping(chosen ? { text: textOf(chosen), over: chosen.id } : { text: "", over: undefined });
    },
    menuTrigger: "focus" as const,
    allowsEmptyCollection: true,
  };
  const state = useComboBoxState<T>(props);
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
        {/* Named by its <label for>, not aria-labelledby too: in Firefox, working out a name
            through aria-labelledby (as axe does) and then changing the form crashed the page. */}
        <input
          {...inputProps}
          onKeyDown={withEscape(state, value ? textOf(value) : "", inputProps.onKeyDown)}
          aria-labelledby={undefined}
          ref={inputRef}
          spellCheck={false}
          autoComplete="off"
        />
        {state.isOpen ? (
          <Popover state={state} triggerRef={inputRef} popoverRef={popoverRef}>
            <ListBox {...listBoxProps} listBoxRef={listBoxRef} state={state} />
            {/* A status, kept in place while the list is open, so screen readers hear it
                change politely and focus stays in the box. */}
            <p
              className={
                found.more
                  ? "listbox-empty listbox-more"
                  : found.message
                    ? "listbox-empty"
                    : "visually-hidden"
              }
              role="status"
            >
              {found.message}
            </p>
          </Popover>
        ) : null}
      </div>
      <p {...descriptionProps} className="field-help">
        {help}
      </p>
    </div>
  );
}

/**
 * Escape in a picker's box, as in WAI-ARIA's combo box pattern and GitHub's and Slack's
 * pickers: with the list open, it closes the list and keeps what's typed; with it closed,
 * it puts back what's chosen (its text), or clears the box if nothing is, as leaving the
 * box does. Either way the key goes no further, so a dialog the picker is in stays open;
 * once there's nothing to undo, it does, and closes the dialog. React Aria's own Escape
 * would put the chosen value back at once, and with nothing chosen, close a list that
 * opened again straight away for the text still in the box.
 */
function withEscape<T>(
  state: ComboBoxState<T>,
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
function Popover<T>({
  state,
  triggerRef,
  popoverRef,
  children,
}: {
  state: ComboBoxState<T>;
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

function ListBox<T>({
  state,
  listBoxRef,
  ...props
}: AriaListBoxOptions<T> & {
  state: ComboBoxState<T>;
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

function Option<T>({ item, state }: { item: Node<T>; state: ComboBoxState<T> }) {
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
