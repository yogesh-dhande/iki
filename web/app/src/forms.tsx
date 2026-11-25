/**
 * Form state the way the server-rendered forms behave: one alert at the top,
 * errors next to each field, and focus on the first invalid field after a
 * failed submit. Required fields are checked here first, so people get plain
 * words without a round trip; the API still checks everything.
 *
 * What it says changes when it's sent, and only then: while it's being sent (`pending`),
 * it says what it said before, as a server form's page stays till the next one loads, so
 * the alert and the fields' errors above its buttons don't go and come back while the
 * answer comes.
 */

import { useEffect, useId, useState, type ReactNode } from "react";

import { fieldErrors, problemOf } from "./api";
import { CHECK_FIELDS, ErrorSummary, problemMessage } from "./ui";
import { useKept } from "./steady";

type Checks = Record<string, string | false | undefined>;

/** `error`: the sending's error, if it failed. `pending`: whether it's being sent. */
export function useForm(error: unknown, pending = false) {
  const id = useId();
  const [own, setChecked] = useState<Record<string, string>>({});
  const [attempt, setAttempt] = useState(0);
  // Held while it's sent, not while a press is on: what a press sends is checked by its
  // click, and its errors (and `aria-invalid`, for focus) come with it. The alert and the
  // fields' errors hold themselves through a press.
  const checked = useKept(own, pending);
  const failed = useKept(error, pending);
  const problem = failed ? problemOf(failed) : undefined;
  const local = Object.keys(checked).length > 0;
  const errors = local ? checked : fieldErrors(problem);

  useEffect(() => {
    if (pending || (attempt === 0 && !error)) return;
    document.getElementById(id)?.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus();
  }, [id, attempt, error, pending]);

  const alert: ReactNode = (
    <ErrorSummary message={local ? CHECK_FIELDS : problemMessage(problem)} />
  );

  return {
    /** Spread on the <form>. */
    props: { id, noValidate: true },
    errors,
    alert,
    /** Runs the checks; true when the form can be sent. */
    validate(checks: Checks): boolean {
      const found: Record<string, string> = {};
      for (const [field, message] of Object.entries(checks)) {
        if (message) found[field] = message;
      }
      setChecked(found);
      setAttempt((n) => n + 1);
      return Object.keys(found).length === 0;
    },
  };
}

/** The message when the value is blank. */
export function required(value: string, message: string): string | false {
  return value.trim() === "" && message;
}
