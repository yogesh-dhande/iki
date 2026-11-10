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
  useCallback,
  useContext,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { Dialog, Heading, Modal, ModalOverlay } from "react-aria-components";

import type { Problem } from "@ikisci/sdk";

// --- Buttons ---------------------------------------------------------------------

type Variant = "primary" | "quiet" | "danger";

export function buttonClass(variant?: Variant): string {
  if (variant === "primary") return "button button-primary";
  if (variant === "danger") return "button button-quiet button-danger";
  if (variant === "quiet") return "button button-quiet";
  return "button";
}

export function Button({
  variant,
  busy = false,
  busyText,
  children,
  type = "button",
  onClick,
  disabled = false,
}: {
  variant?: Variant;
  busy?: boolean;
  busyText?: string;
  children: ReactNode;
  type?: "button" | "submit";
  onClick?: () => void;
  disabled?: boolean;
}) {
  return (
    <button
      className={buttonClass(variant)}
      type={type}
      aria-disabled={busy || disabled ? "true" : undefined}
      onClick={(event) => {
        if (busy || disabled) {
          event.preventDefault();
          return;
        }
        onClick?.();
      }}
    >
      {busy && busyText ? busyText : children}
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
      {error ? (
        <p className="field-error" id={errorId}>
          {error}
        </p>
      ) : null}
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
  options: readonly { value: T; label: string; description?: string }[];
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
      <div className="choices">
        {options.map((option) => (
          <label className="choice" key={option.value} htmlFor={`${name}-${option.value}`}>
            <input
              type="radio"
              id={`${name}-${option.value}`}
              name={name}
              value={option.value}
              checked={option.value === value}
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
      </>
    );
  }
  return (
    <div className={error ? "field field-invalid" : "field"}>
      <label htmlFor={id}>{label}</label>
      {error ? (
        <p className="field-error" id={`${id}-error`}>
          {error}
        </p>
      ) : null}
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

/** A form's problem: what went wrong, said once at the top of the form. */
export function ProblemAlert({ problem }: { problem: Problem | undefined }) {
  if (!problem) return null;
  // Errors about particular fields show next to them; the alert points there.
  const message = problem.errors?.length ? "Check the highlighted fields." : problem.detail;
  return (
    <div className="alert alert-error" role="alert">
      <p>{message}</p>
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

/**
 * Confirmations show on the page they're said on, like the server's messages
 * after a redirect: say them once a navigation has finished, and they go when
 * the person moves on.
 */
export function MessagesProvider({ children }: { children: ReactNode }) {
  const router = useRouter();
  const visit = useRouterState({ select: (state) => visitOf(state.location) });
  const [messages, setMessages] = useState<{ id: number; visit: string; text: string }[]>([]);
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
  const shown = messages.filter((m) => m.visit === visit);
  const value = useMemo(() => ({ say }), [say]);
  return (
    <MessagesContext.Provider value={value}>
      {/* Always rendered, so screen readers announce what is added to it. */}
      <div className="messages" role="status">
        {shown.map((message) => (
          <div className="alert alert-success" key={message.id}>
            <p>{message.text}</p>
          </div>
        ))}
      </div>
      {children}
    </MessagesContext.Provider>
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
  busy,
  problem,
}: {
  isOpen: boolean;
  onCancel: () => void;
  onConfirm: () => void;
  title: string;
  children: ReactNode;
  confirmLabel: string;
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
          <ProblemAlert problem={problem} />
          {children}
          <div className="actions">
            <Button variant="danger" busy={busy} busyText="Working…" onClick={onConfirm}>
              {confirmLabel}
            </Button>
            <Button variant="quiet" onClick={onCancel} disabled={busy}>
              Cancel
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

export function PageHeader({
  title,
  eyebrow,
  badge,
  actions,
}: {
  title: string;
  eyebrow?: ReactNode;
  badge?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="page-header">
      <div>
        {eyebrow ? <p className="eyebrow">{eyebrow}</p> : null}
        <PageTitle>
          {title}
          {badge ? <> {badge}</> : null}
        </PageTitle>
      </div>
      {actions}
    </div>
  );
}

export function Badge({ children, muted = false }: { children: ReactNode; muted?: boolean }) {
  return <span className={muted ? "badge badge-muted" : "badge"}>{children}</span>;
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
function useNow(): number {
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
