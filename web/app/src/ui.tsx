/**
 * The app's design system: the same markup and classes as the server-rendered
 * pages (static/iki/iki.css), as React components. Form controls are native
 * elements wired up the way the server's `_field.html` does it: errors and
 * help linked through `aria-describedby`, `aria-invalid` on invalid fields,
 * radio groups in a fieldset with a legend. Dialogs use React Aria, which
 * traps focus, closes on Escape and returns focus when they close.
 */

import { useRouter, useRouterState } from "@tanstack/react-router";
import {
  createContext,
  createElement,
  useCallback,
  useContext,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
  type RefObject,
} from "react";
import { Dialog, Heading, Modal, ModalOverlay } from "react-aria-components";

import { createPortal } from "react-dom";
import type { Problem } from "@ikisci/sdk";
import "@ikisci/ui/page-header";
import type { IkiPageHeader } from "@ikisci/ui/page-header";
import { buttonState, type ButtonOptions } from "@ikisci/ui/button";

export { buttonClass } from "@ikisci/ui/button";

import { TopBar, usePhone } from "./page-top";
import { useSteady } from "./steady";

// --- Buttons ---------------------------------------------------------------------

export function Button({
  children,
  onClick,
  ...options
}: ButtonOptions & { children: ReactNode; onClick?: () => void }) {
  const { attributes, content, press } = buttonState(options, children);
  return (
    <button
      {...attributes}
      onClick={(event) => {
        press(event, onClick);
      }}
    >
      {content}
    </button>
  );
}

// --- Fields ----------------------------------------------------------------------

interface FieldProps {
  label: string;
  value: string;
  onChange: (value: string) => void;
  error?: string | undefined;
  help?: string;
  optional?: boolean;
  maxLength?: number;
  autoFocus?: boolean;
}

function describedBy(...ids: (string | false | undefined)[]): string | undefined {
  const joined = ids.filter(Boolean).join(" ");
  return joined || undefined;
}

/** Whether focus is in the field `anchor` is in. */
function useInField(anchor: RefObject<HTMLElement | null>): boolean {
  const subscribe = useCallback((changed: () => void) => {
    document.addEventListener("focusin", changed);
    document.addEventListener("focusout", changed);
    return () => {
      document.removeEventListener("focusin", changed);
      document.removeEventListener("focusout", changed);
    };
  }, []);
  return useSyncExternalStore(
    subscribe,
    () => {
      const field = anchor.current?.parentElement;
      return Boolean(field && document.activeElement && field.contains(document.activeElement));
    },
    () => false,
  );
}

/**
 * A field's error, under its label and over its control, as the server's forms put it.
 *
 * Its control, and the fields and buttons after it, mustn't move under the person working
 * in it. So it changes between presses, not during one (`useSteady`); and once it's put
 * right, it keeps its room while focus is still in the field, and gives it up once they
 * leave, by a key or, after its click, by a press.
 */
export function FieldError({
  id,
  error,
  alert = false,
}: {
  /** Its own ID, for the control's `aria-describedby`. */
  id: string;
  error: string | undefined;
  /** Announced as it comes: for an error that comes without focus moving to its field. */
  alert?: boolean;
}) {
  const anchor = useRef<HTMLDivElement>(null);
  const shown = useSteady(error);
  const inField = useSteady(useInField(anchor));
  // How much room it took: kept while it's put right in the field.
  const [room, setRoom] = useState<number>();
  if (!shown && !inField && room !== undefined) setRoom(undefined);
  const measure = useCallback((node: HTMLParagraphElement | null) => {
    if (!node) return;
    const margin = parseFloat(getComputedStyle(node).marginBottom) || 0;
    setRoom(node.getBoundingClientRect().height + margin);
  }, []);
  return (
    <div className="field-message" ref={anchor}>
      {shown ? (
        // Its own element for each error, so each is measured as it's drawn.
        <p
          className="field-error"
          id={id}
          key={shown}
          ref={measure}
          role={alert ? "alert" : undefined}
        >
          {shown}
        </p>
      ) : inField && room ? (
        <div
          className="field-error-room"
          aria-hidden="true"
          style={{ height: `${String(room)}px` }}
        />
      ) : null}
    </div>
  );
}

export function TextField({
  label,
  value,
  onChange,
  error,
  help,
  optional = false,
  maxLength,
  autoFocus = false,
  type = "text",
  multiline = false,
}: FieldProps & { type?: "text" | "email"; multiline?: boolean }) {
  const id = useId();
  const errorId = `${id}-error`;
  const helpId = `${id}-help`;
  const shared = {
    id,
    value,
    onChange: (event: { target: { value: string } }) => {
      onChange(event.target.value);
    },
    "aria-invalid": error ? ("true" as const) : undefined,
    "aria-describedby": describedBy(error && errorId, help && helpId),
    required: !optional,
    maxLength,
    // A field the person just asked for (a dialog, a new form) takes focus.
    autoFocus,
  };
  return (
    <div className={error ? "field field-invalid" : "field"}>
      <label htmlFor={id}>
        {label}
        {optional ? <span className="optional"> (optional)</span> : null}
      </label>
      <FieldError id={errorId} error={error} />
      {multiline ? (
        <textarea rows={3} {...shared} />
      ) : (
        <input
          type={type}
          autoComplete="off"
          spellCheck={type === "email" ? false : undefined}
          {...shared}
        />
      )}
      {help ? (
        <p className="field-help" id={helpId}>
          {help}
        </p>
      ) : null}
    </div>
  );
}

export function RadioField<T extends string>({
  label,
  value,
  onChange,
  options,
  help,
  error,
}: {
  label: string;
  value: T;
  onChange: (value: T) => void;
  options: readonly { value: T; label: string; description?: string; disabled?: boolean }[];
  help?: string;
  error?: string | undefined;
}) {
  const id = useId();
  const name = `${id}-choice`;
  return (
    <fieldset
      className={error ? "field field-choices field-invalid" : "field field-choices"}
      aria-describedby={describedBy(error && `${id}-error`, help && `${id}-help`)}
    >
      <legend>{label}</legend>
      <FieldError id={`${id}-error`} error={error} />
      {help ? (
        <p className="field-help" id={`${id}-help`}>
          {help}
        </p>
      ) : null}
      <div className="choices">
        {options.map((option) => (
          <label className="choice" key={option.value} htmlFor={`${name}-${option.value}`}>
            <input
              type="radio"
              id={`${name}-${option.value}`}
              name={name}
              value={option.value}
              checked={option.value === value}
              disabled={option.disabled}
              onChange={() => {
                onChange(option.value);
              }}
            />
            <span>
              {option.label}
              {option.description ? `: ${option.description}` : ""}
            </span>
          </label>
        ))}
      </div>
    </fieldset>
  );
}

/** Checkboxes for choosing several, in a fieldset like `RadioField`'s. */
export function CheckboxesField<T extends string>({
  label,
  value,
  onChange,
  options,
  help,
  error,
  literal = true,
}: {
  label: string;
  value: readonly T[];
  onChange: (value: T[]) => void;
  options: readonly { value: T; label: string; description?: string }[];
  help?: string;
  error?: string | undefined;
  /** Whether the options are names to type, set as code; false for teams, say. */
  literal?: boolean;
}) {
  const id = useId();
  return (
    <fieldset
      className={error ? "field field-choices field-invalid" : "field field-choices"}
      aria-describedby={describedBy(error && `${id}-error`, help && `${id}-help`)}
    >
      <legend>{label}</legend>
      <FieldError id={`${id}-error`} error={error} />
      {help ? (
        <p className="field-help" id={`${id}-help`}>
          {help}
        </p>
      ) : null}
      <div className="choices">
        {options.map((option, index) => (
          // The label is the choice's name alone, and its description describes it, so
          // two choices that mention the same thing (a project and its team) aren't
          // confused. The label stretches over the card, so all of it toggles the box.
          <div className="choice choice-described" key={option.value}>
            <input
              type="checkbox"
              id={`${id}-${option.value}`}
              value={option.value}
              // The form moves focus to the first invalid control.
              aria-invalid={error && index === 0 ? "true" : undefined}
              aria-describedby={option.description ? `${id}-${option.value}-help` : undefined}
              checked={value.includes(option.value)}
              onChange={(event) => {
                const checked = event.target.checked;
                onChange(
                  options
                    .map((o) => o.value)
                    .filter((v) => (v === option.value ? checked : value.includes(v))),
                );
              }}
            />
            <span className="choice-text">
              <label htmlFor={`${id}-${option.value}`}>
                {literal ? <code>{option.label}</code> : option.label}
              </label>
              {option.description ? (
                <span className="choice-help" id={`${id}-${option.value}-help`}>
                  {option.description}
                </span>
              ) : null}
            </span>
          </div>
        ))}
      </div>
    </fieldset>
  );
}

/** One checkbox for a yes-or-no setting, its help under it (a description, not its name). */
export function CheckboxField({
  label,
  checked,
  onChange,
  help,
  error,
  announce = false,
  disabled = false,
}: {
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
  help?: string;
  error?: string | undefined;
  /** Its error is announced as it comes: where focus doesn't move to it, as in a dialog. */
  announce?: boolean;
  disabled?: boolean;
}) {
  const id = useId();
  return (
    <div className={error ? "field field-choices field-invalid" : "field field-choices"}>
      <FieldError id={`${id}-error`} error={error} alert={announce} />
      <label className="choice" htmlFor={id}>
        <input
          type="checkbox"
          id={id}
          checked={checked}
          disabled={disabled}
          aria-invalid={error ? "true" : undefined}
          aria-describedby={describedBy(error && `${id}-error`, help && `${id}-help`)}
          onChange={(event) => {
            onChange(event.target.checked);
          }}
        />
        <span className="choice-text">{label}</span>
      </label>
      {help ? (
        <p className="field-help" id={`${id}-help`}>
          {help}
        </p>
      ) : null}
    </div>
  );
}

/**
 * A value to copy, such as a secret shown once, with a Copy button. `shown` stands in for a
 * long value, such as an image reference with its digest cut short: the whole value is
 * still what's copied, and what a pointer resting on it shows.
 */
export function CopyValue({
  value,
  label,
  shown,
}: {
  value: string;
  label: string;
  shown?: string;
}) {
  return (
    <div className="copy-row">
      <code className="copy-value" title={shown === undefined ? undefined : value}>
        {shown ?? value}
      </code>
      <CopyButton value={value} label={label} />
    </div>
  );
}

/** Copies `value`, and says whether it could. */
export function CopyButton({ value, label }: { value: string; label: string }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  useEffect(() => {
    if (state === "idle") return;
    const timer = setTimeout(() => {
      setState("idle");
    }, 2000);
    return () => {
      clearTimeout(timer);
    };
  }, [state]);
  return (
    <>
      <button
        className="button button-quiet"
        type="button"
        aria-label={state === "idle" ? `Copy ${label}` : undefined}
        onClick={() => {
          // No clipboard (an insecure context, or refused): say so, and the value stays shown.
          void (async () => {
            try {
              await navigator.clipboard.writeText(value);
              setState("copied");
            } catch {
              setState("failed");
            }
          })();
        }}
      >
        {state === "copied" ? "Copied" : state === "failed" ? "Copy failed" : "Copy"}
      </button>
      <span className="visually-hidden" role="status">
        {state === "copied" ? "Copied." : state === "failed" ? "Couldn’t copy." : ""}
      </span>
    </>
  );
}

export function SelectField<T extends string>({
  label,
  value,
  onChange,
  options,
  help,
  error,
  compact = false,
}: {
  label: string;
  value: T;
  onChange: (value: T) => void;
  options: readonly { value: T; label: string }[];
  help?: string;
  error?: string | undefined;
  /** In a row of actions: the label is for screen readers only. */
  compact?: boolean;
}) {
  const id = useId();
  const select = (
    <select
      id={id}
      value={value}
      aria-invalid={error ? "true" : undefined}
      aria-describedby={describedBy(error && `${id}-error`, help && `${id}-help`)}
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
  );
  if (compact) {
    return (
      <>
        <label className="visually-hidden" htmlFor={id}>
          {label}
        </label>
        {select}
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
      </>
    );
  }
  return (
    <div className={error ? "field field-invalid" : "field"}>
      <label htmlFor={id}>{label}</label>
      <FieldError id={`${id}-error`} error={error} />
      {select}
      {help ? (
        <p className="field-help" id={`${id}-help`}>
          {help}
        </p>
      ) : null}
    </div>
  );
}

// --- Feedback --------------------------------------------------------------------

/** What a problem says at the top of a form: errors about particular fields show next to
 * them, and it points there. */
export function problemMessage(problem: Problem | undefined): string | undefined {
  if (!problem) return undefined;
  return problem.errors?.length ? CHECK_FIELDS : problem.detail;
}

export const CHECK_FIELDS = "Check the highlighted fields.";

/**
 * A form's problem: what went wrong, said once at the top of the form, and announced.
 *
 * It's above the form's fields and buttons, so it doesn't change during a press
 * (`useSteady`); and while the form is sent again (`pending`) it says what it said till the
 * answer comes, as a server form's page stays till the next one loads, rather than going
 * and coming back.
 */
export function ProblemAlert({
  problem,
  pending = false,
}: {
  problem: Problem | undefined;
  pending?: boolean;
}) {
  return <ErrorSummary message={problemMessage(problem)} pending={pending} />;
}

/** The alert at the top of a form (`ProblemAlert`), for a message of its own. */
export function ErrorSummary({
  message,
  pending = false,
}: {
  message: string | undefined;
  pending?: boolean;
}) {
  const shown = useSteady(message, pending);
  if (shown === undefined) return null;
  return (
    <div className="alert alert-error" role="alert">
      <p>{shown}</p>
    </div>
  );
}

interface Messages {
  /** Shows a short confirmation, like the server pages' "Saved." banners. */
  say: (message: string) => void;
}

const MessagesContext = createContext<Messages>({ say: () => undefined });

type Location = ReturnType<typeof useRouter>["state"]["location"];

/** Each visit to a page (each history entry) has its own key. */
function visitOf(location: Location): string {
  return location.state.__TSR_key ?? location.href;
}

interface Message {
  id: number;
  visit: string;
  text: string;
}

/**
 * Confirmations show on the page they're said on, like the server's messages
 * after a redirect: say them once a navigation has finished, and they go when
 * the person moves on, or dismisses them.
 *
 * They come after something's done, while the page can be pressed, so they take no room
 * from it: they're at the foot of the page, held at the foot of the window over what's
 * there. Nothing on the page moves for one.
 */
export function MessagesProvider({ children }: { children: ReactNode }) {
  const router = useRouter();
  const visit = useRouterState({ select: (state) => visitOf(state.location) });
  const [messages, setMessages] = useState<Message[]>([]);
  const nextId = useRef(0);
  const say = useCallback(
    (text: string) => {
      nextId.current += 1;
      const message = { id: nextId.current, visit: visitOf(router.state.location), text };
      setMessages((current) => [
        ...current.filter((m) => m.visit === message.visit).slice(-2),
        message,
      ]);
    },
    [router],
  );
  const dismiss = useCallback((id: number) => {
    setMessages((current) => current.filter((m) => m.id !== id));
  }, []);
  const here = useMemo(() => messages.filter((m) => m.visit === visit), [messages, visit]);
  const value = useMemo(() => ({ say }), [say]);
  return (
    <MessagesContext.Provider value={value}>
      {children}
      <Notices messages={here} visit={visit} dismiss={dismiss} />
    </MessagesContext.Provider>
  );
}

/** The confirmations at the foot of the page (`MessagesProvider`). */
function Notices({
  messages,
  visit,
  dismiss,
}: {
  messages: readonly Message[];
  visit: string;
  dismiss: (id: number) => void;
}) {
  // One said during a press comes once it's let go: it could come over what's pressed.
  const shown = useSteady(messages);
  // The most room they've taken on this visit, kept as one is dismissed: at the end of a
  // page scrolled to its foot, the page would otherwise move down as one goes.
  const [room, setRoom] = useState({ visit, height: 0 });
  const list = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const node = list.current;
    if (!node || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      const height = node.offsetHeight;
      setRoom((now) =>
        now.visit !== visit ? { visit, height } : height > now.height ? { visit, height } : now,
      );
    });
    observer.observe(node);
    return () => {
      observer.disconnect();
    };
  }, [visit]);
  const height = room.visit === visit ? room.height : 0;
  return (
    <div className="notices" style={height ? { minHeight: `${String(height)}px` } : undefined}>
      {/* Always rendered, so screen readers announce what is added to it. */}
      <div className="notices-list" role="status" ref={list}>
        {shown.map((message) => (
          <div className="alert alert-success notice" key={message.id}>
            <p>{message.text}</p>
            <Button
              variant="quiet"
              onClick={() => {
                dismiss(message.id);
              }}
            >
              Dismiss
            </Button>
          </div>
        ))}
      </div>
    </div>
  );
}

export function useMessages(): Messages {
  return useContext(MessagesContext);
}

// --- Dialogs ---------------------------------------------------------------------

/** Asks before something that's hard to undo. */
export function ConfirmDialog({
  isOpen,
  onCancel,
  onConfirm,
  title,
  children,
  confirmLabel,
  cancelLabel = "Cancel",
  confirmVariant = "danger",
  busy,
  problem,
  confirmDisabled = false,
}: {
  isOpen: boolean;
  onCancel: () => void;
  onConfirm: () => void;
  title: string;
  children: ReactNode;
  confirmLabel: string;
  /** Not yet: what it needs to say before it's confirmed is still coming, say. */
  confirmDisabled?: boolean;
  /** For when "Cancel" would be ambiguous, such as when cancelling a run. */
  cancelLabel?: string;
  /** Danger for what's hard to undo; primary for what isn't, such as restoring. */
  confirmVariant?: ButtonOptions["variant"];
  busy: boolean;
  problem?: Problem | undefined;
}) {
  return (
    <ModalOverlay
      className="dialog-overlay"
      isOpen={isOpen}
      isDismissable={!busy}
      onOpenChange={(open) => {
        if (!open) onCancel();
      }}
    >
      <Modal className="dialog">
        <Dialog role="alertdialog" className="dialog-body">
          <Heading slot="title" className="section-title">
            {title}
          </Heading>
          {/* Busy, it's being sent again: what it said stays till the answer comes. */}
          <ProblemAlert problem={problem} pending={busy} />
          {children}
          <div className="actions">
            <Button
              variant={confirmVariant}
              busy={busy}
              busyText="Working…"
              disabled={confirmDisabled}
              onClick={onConfirm}
            >
              {confirmLabel}
            </Button>
            <Button variant="quiet" onClick={onCancel} disabled={busy}>
              {cancelLabel}
            </Button>
          </div>
        </Dialog>
      </Modal>
    </ModalOverlay>
  );
}

// --- Page pieces -----------------------------------------------------------------

/** Set once the person moves within the app; on the first load the browser places focus. */
let navigated = false;

export function markNavigated(): void {
  navigated = true;
}

/**
 * The page's h1. After a navigation within the app it takes focus, as a page
 * load would, so screen readers announce the new page and Tab starts there.
 * It focuses itself when it mounts (pages that load their data show it late)
 * and when the address changes under it (a project's tabs).
 */
export function PageTitle({ children }: { children: ReactNode }) {
  const path = useRouterState({ select: (state) => state.location.pathname });
  const ref = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    if (navigated) ref.current?.focus();
  }, [path]);
  return (
    <h1 ref={ref} tabIndex={-1} data-page-title>
      {children}
    </h1>
  );
}

/**
 * A page's header (ADR 0176): its breadcrumb and actions in the bar across the top of the
 * page, then its title, with what it is in a line beside it.
 *
 * - `crumbs`: links to the pages above it, then its own name, last; its title if left out.
 * - `actions`: its buttons (New, Import, Edit…), primary last.
 * - `description`: one line beside the title. Cut short where it doesn't fit, it's whole
 *   in the toggletip (i) beside it.
 * - `about`: more about it, such as a link to its type, in that toggletip, always offered.
 * - `marker`: a swatch before the title, as a type's dot or a project's square.
 */
export function PageHeader({
  title,
  eyebrow,
  badge,
  actions,
  crumbs,
  description,
  about,
  marker,
}: {
  title: string;
  eyebrow?: ReactNode;
  badge?: ReactNode;
  actions?: ReactNode;
  crumbs?: ReactNode[];
  description?: ReactNode;
  about?: ReactNode;
  marker?: ReactNode;
}) {
  const phone = usePhone();
  const path = useRouterState({ select: (state) => state.location.pathname });
  const [header, setHeader] = useState<IkiPageHeader | null>(null);
  useEffect(() => {
    if (navigated) header?.heading.focus();
  }, [path, header]);
  return (
    <>
      <TopBar crumbs={crumbs ?? [title]} weak={!crumbs} actions={phone ? undefined : actions} />
      {createElement("iki-page-header", { data: { title }, ref: setHeader })}
      {header && (
        <>
          {createPortal(eyebrow, header.areas.eyebrow)}
          {createPortal(marker, header.areas.marker)}
          {createPortal(badge ? <> {badge}</> : null, header.areas.badge)}
          {createPortal(description, header.areas.description)}
          {createPortal(description, header.areas.fullDescription)}
          {createPortal(about, header.areas.about)}
          {phone && createPortal(actions, header.areas.actions)}
        </>
      )}
    </>
  );
}

export function Badge({ children, muted = false }: { children: ReactNode; muted?: boolean }) {
  return <span className={muted ? "badge badge-muted" : "badge"}>{children}</span>;
}

/** A type's name as `typeDisplayName` gives it: its label, then its plugin in muted text
 * when another type shares the label. */
export function TypeName({ shown }: { shown: { label: string; plugin?: string | undefined } }) {
  return (
    <>
      {shown.label}
      {shown.plugin ? (
        <>
          {" "}
          <span className="type-plugin">{shown.plugin}</span>
        </>
      ) : null}
    </>
  );
}

export function Loading({ label }: { label: string }) {
  return (
    <p className="muted" role="status" aria-live="polite">
      {label}
    </p>
  );
}

export function Time({ value }: { value: string }) {
  const date = new Date(value);
  return (
    <time dateTime={value}>
      {date.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" })}
    </time>
  );
}

const relative = new Intl.RelativeTimeFormat("en-GB", { numeric: "auto" });

const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ["minute", 60],
  ["hour", 60 * 60],
  ["day", 24 * 60 * 60],
];

/** The time now, updated each minute, so "5 minutes ago" stays true. */
export function useNow(): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => {
      setNow(Date.now());
    }, 60_000);
    return () => {
      clearInterval(timer);
    };
  }, []);
  return now;
}

/** "5 minutes ago", "yesterday"; the date after a week. The exact time shows on hover. */
export function RelativeTime({ value }: { value: string }) {
  const now = useNow();
  const date = new Date(value);
  const seconds = Math.round((date.getTime() - now) / 1000);
  let text: string;
  if (Math.abs(seconds) < 60) {
    text = "just now";
  } else if (Math.abs(seconds) >= 7 * 24 * 60 * 60) {
    text = date.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
  } else {
    const [unit, size] =
      UNITS.findLast(([, size]) => Math.abs(seconds) >= size) ?? (["minute", 60] as const);
    text = relative.format(Math.round(seconds / size), unit);
  }
  const exact = date.toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" });
  return (
    <time dateTime={value} title={exact}>
      {text}
    </time>
  );
}
