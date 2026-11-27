/**
 * A sheet's cell that names a lab list's options (ADR 0270), as it's typed in: the list's
 * active options under it, in its order, as a spreadsheet's dropdown cell lists them, and
 * narrowed as people type, by label or code. Choosing one writes its label, which the
 * sheet reads back as it reads its code; typing or pasting either does the same, and the
 * sheet's check says if what's typed names none.
 *
 * It's a WAI-ARIA combo box: the cell's input keeps focus, the arrow keys move through the
 * options (`aria-activedescendant`), Enter and Tab choose the highlighted one and leave
 * the cell as they otherwise would, and Escape closes the list, then puts the cell back.
 * A list of options' cell (`multiple`) takes several, apart by semicolons: choosing one
 * replaces what's typed after the last.
 */

import { useQuery } from "@tanstack/react-query";
import { useId, useLayoutEffect, useRef, useState, type KeyboardEvent } from "react";
import { createPortal } from "react-dom";

import type { EntityType } from "../entities";
import { matching, optionsQuery, type ListOption } from "../lab-lists";
import { SEPARATOR } from "../sheet";

/** Where the cell's input leaves the sheet's active cell, once it's done. */
export type Leave = "down" | "up" | "right" | "left" | null;

/** What's typed after a list's last semicolon: the option being typed. */
function lastPart(text: string): string {
  const at = text.lastIndexOf(";");
  return at < 0 ? text : text.slice(at + 1);
}

/** The text with what's typed after its last semicolon replaced by `label`. */
function withLast(text: string, label: string): string {
  const at = text.lastIndexOf(";");
  const before = at < 0 ? "" : text.slice(0, at).trimEnd();
  return before ? `${before}${SEPARATOR}${label}` : label;
}

export function ListCell({
  label,
  list,
  projectId,
  multiple,
  text,
  typedFirst,
  onText,
  onCommit,
  onCancel,
}: {
  /** The cell's name: its column and row. */
  label: string;
  list: EntityType;
  projectId: string;
  multiple: boolean;
  text: string;
  /** Editing began with a key typed: it narrows the options from the start. */
  typedFirst: boolean;
  onText: (text: string) => void;
  /** Done, with the cell's text (as chosen, or as typed), leaving as Enter or Tab do. */
  onCommit: (text: string, leave: Leave) => void;
  onCancel: () => void;
}) {
  const id = useId();
  const options = useQuery(optionsQuery(list.name, { project: projectId }));
  const input = useRef<HTMLInputElement | null>(null);
  const [open, setOpen] = useState(true);
  // Narrowed by what's typed once something is: opened on a value, every option shows.
  const [narrowing, setNarrowing] = useState(typedFirst);
  const every = options.data ?? [];
  const shown = narrowing ? matching(every, lastPart(text)) : every;
  const current = lastPart(text).trim().toLocaleLowerCase();
  const [active, setActive] = useState(() =>
    Math.max(
      -1,
      every.findIndex(
        (o) => o.label.toLocaleLowerCase() === current || o.code.toLocaleLowerCase() === current,
      ),
    ),
  );
  const highlighted = open && active >= 0 ? shown[active] : undefined;
  const [place, setPlace] = useState<{
    top: number;
    bottom: number;
    left: number;
    width: number;
    up: boolean;
  }>();

  // The list goes under the cell, or over it when there's more room there; it follows the
  // cell as the sheet or the page scrolls.
  useLayoutEffect(() => {
    if (!open) return;
    const measure = () => {
      const box = input.current?.getBoundingClientRect();
      if (!box) return;
      const room = window.innerHeight - box.bottom;
      setPlace({
        top: box.top,
        bottom: box.bottom,
        left: box.left,
        width: Math.max(box.width, 220),
        up: room < 200 && box.top > room,
      });
    };
    measure();
    window.addEventListener("scroll", measure, true);
    window.addEventListener("resize", measure);
    return () => {
      window.removeEventListener("scroll", measure, true);
      window.removeEventListener("resize", measure);
    };
  }, [open]);

  // Once the options come, the cell's own one is highlighted.
  const [seen, setSeen] = useState(options.data);
  if (options.data !== seen) {
    setSeen(options.data);
    if (!narrowing && active < 0) {
      setActive(
        every.findIndex(
          (o) => o.label.toLocaleLowerCase() === current || o.code.toLocaleLowerCase() === current,
        ),
      );
    }
  }

  const choose = (option: ListOption, leave: Leave) => {
    if (multiple) {
      const next = withLast(text, option.label);
      if (leave === null) {
        onText(`${next}${SEPARATOR}`);
        setNarrowing(true);
        setActive(-1);
        setOpen(false);
        input.current?.focus();
        return;
      }
      onCommit(next, leave);
      return;
    }
    onCommit(option.label, leave);
  };

  const keyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    const key = event.key;
    if (key === "ArrowDown" || key === "ArrowUp") {
      event.preventDefault();
      if (!open) {
        setOpen(true);
        return;
      }
      if (!shown.length) return;
      setActive((at) =>
        key === "ArrowDown" ? Math.min(shown.length - 1, at + 1) : Math.max(0, at - 1),
      );
      return;
    }
    if (key === "Enter") {
      event.preventDefault();
      const leave = event.shiftKey ? "up" : "down";
      if (highlighted) choose(highlighted, leave);
      else onCommit(text, leave);
      return;
    }
    if (key === "Tab") {
      event.preventDefault();
      const leave = event.shiftKey ? "left" : "right";
      // Tab takes the highlighted option once something's typed, as a picker's does.
      if (highlighted && narrowing) choose(highlighted, leave);
      else onCommit(text, leave);
      return;
    }
    if (key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      if (open) setOpen(false);
      else onCancel();
    }
  };

  const listId = `${id}-options`;
  const optionId = (index: number) => `${id}-option-${String(index)}`;
  return (
    <>
      <input
        className="record-sheet-input"
        role="combobox"
        aria-label={label}
        aria-autocomplete="list"
        aria-expanded={open}
        aria-controls={listId}
        aria-activedescendant={highlighted ? optionId(active) : undefined}
        autoComplete="off"
        spellCheck={false}
        ref={(node) => {
          input.current = node;
          // The cell being typed in has focus, its text's end the caret.
          if (node && document.activeElement !== node) {
            node.focus({ preventScroll: true });
            node.setSelectionRange(node.value.length, node.value.length);
          }
        }}
        value={text}
        onChange={(event) => {
          onText(event.target.value);
          setNarrowing(true);
          setOpen(true);
          setActive(event.target.value.trim() ? 0 : -1);
        }}
        onBlur={() => {
          onCommit(text, null);
        }}
        onKeyDown={keyDown}
      />
      {createPortal(
        // Not a control itself: a press in it keeps focus in the cell's input.
        // eslint-disable-next-line jsx-a11y/no-static-element-interactions
        <div
          className="popover sheet-options"
          hidden={!open || !place}
          data-placement={place?.up ? "top" : "bottom"}
          style={
            place
              ? {
                  left: place.left,
                  width: place.width,
                  ...(place.up
                    ? { bottom: window.innerHeight - place.top + 4 }
                    : { top: place.bottom + 4 }),
                }
              : undefined
          }
          onMouseDown={(event) => {
            // The input keeps focus: pressing in the list mustn't end the edit.
            event.preventDefault();
          }}
        >
          <div className="listbox" id={listId} role="listbox" aria-label={list.label_plural}>
            {shown.map((o, index) => (
              // Chosen by keyboard in the input, which keeps focus (aria-activedescendant),
              // as WAI-ARIA's combo box pattern has it; pressed, here.
              // eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/interactive-supports-focus
              <div
                key={o.id}
                id={optionId(index)}
                role="option"
                aria-selected={index === active}
                className="listbox-item"
                data-focused={index === active || undefined}
                onClick={() => {
                  choose(o, null);
                }}
              >
                <span>{o.label}</span> <span className="muted">{o.code}</span>
              </div>
            ))}
          </div>
          <p className={shown.length ? "visually-hidden" : "listbox-empty"} role="status">
            {options.isPending
              ? "Loading its options…"
              : options.isError
                ? "Couldn’t load its options. Type its code or label."
                : shown.length
                  ? ""
                  : `No option matches “${lastPart(text).trim()}”.`}
          </p>
        </div>,
        document.body,
      )}
    </>
  );
}
