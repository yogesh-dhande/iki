/**
 * Form state the way the server-rendered forms behave: one alert at the top,
 * errors next to each field, and focus on the first invalid field after a
 * failed submit. Required fields are checked here first, so people get plain
 * words without a round trip; the API still checks everything.
 */

import { useEffect, useId, useState, type ReactNode } from "react";

import { fieldErrors, problemOf } from "./api";
import { ProblemAlert } from "./ui";

type Checks = Record<string, string | false | undefined>;

export function useForm(error: unknown) {
  const id = useId();
  const [checked, setChecked] = useState<Record<string, string>>({});
  const [attempt, setAttempt] = useState(0);
  const problem = error ? problemOf(error) : undefined;
  const local = Object.keys(checked).length > 0;
  const errors = local ? checked : fieldErrors(problem);

  useEffect(() => {
    if (attempt === 0 && !error) return;
    document.getElementById(id)?.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus();
  }, [id, attempt, error]);

  const alert: ReactNode = local ? (
    <div className="alert alert-error" role="alert">
      <p>Check the highlighted fields.</p>
    </div>
  ) : (
    <ProblemAlert problem={problem} />
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
