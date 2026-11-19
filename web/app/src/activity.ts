/**
 * Whether the person is using the page, for their session's idle timeout (ADR 0153).
 *
 * Only what the person does keeps their session open (21 CFR Part 11 §11.10(d)). The app
 * polls, follows live updates and reads again what changed by itself, and none of that may
 * keep an unattended tab signed in. So an API request made when the person hasn't touched
 * the page for a minute says so with `Iki-Background: 1`, and the server leaves the idle
 * timeout where it was. Work the person started and is waiting on, such as an upload,
 * counts as them being there while it runs.
 */

export const BACKGROUND_HEADER = "Iki-Background";

/** How recent the person's last input must be for a request to count as theirs. */
export const ACTIVE_WITHIN_MS = 60_000;

const INPUTS = ["pointerdown", "pointermove", "keydown", "wheel", "touchstart"] as const;

// Opening the page is the person's doing.
let lastInput = Date.now();
let working = 0;

/** The person did something: on this page, or in a frame of Iki's own that said so. */
export function noticeInput(): void {
  lastInput = Date.now();
}

for (const type of INPUTS) {
  window.addEventListener(type, noticeInput, { capture: true, passive: true });
}

/** Whether a request made now is the page's own, not the person's. */
export function isIdle(): boolean {
  return working === 0 && Date.now() - lastInput > ACTIVE_WITHIN_MS;
}

/** Runs work the person started and waits on: while it runs, they count as active. */
export async function whileWorking<T>(work: () => Promise<T>): Promise<T> {
  working += 1;
  try {
    return await work();
  } finally {
    working -= 1;
  }
}

/** For tests. */
export function resetActivity(): void {
  lastInput = Date.now();
  working = 0;
}
