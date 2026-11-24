/**
 * What holds still under a press.
 *
 * A press that begins on one control and is let go on another clicks neither: the browser
 * sends the click to what both are in, a card, say. So a page that moves while a press is
 * on loses the press, or gives it to a neighbour. What a page draws late (a confirmation, a
 * form's problems, a count refetched) is drawn as it was when the press began, and
 * afresh in the task after it's let go, so the press's click lands first, on what it was
 * pressed on. The tabs (TabStrip) began this; every late change above a control goes
 * through it.
 *
 * Holding is for a press, not a pause between presses: a confirmation goes where it moves
 * nothing (MessagesProvider), and a form's problems change only when it's sent (useForm).
 */

import { useState, useSyncExternalStore } from "react";

const listeners = new Set<() => void>();
const pointers = new Set<number>();
let pressing = false;
let release: ReturnType<typeof setTimeout> | undefined;
let installed = false;

function set(next: boolean) {
  if (pressing === next) return;
  pressing = next;
  for (const listener of listeners) listener();
}

function down(event: PointerEvent) {
  pointers.add(event.pointerId);
  clearTimeout(release);
  set(true);
}

function letGo() {
  clearTimeout(release);
  // In the task after: the click the release makes is sent first, to what was pressed.
  release = setTimeout(() => {
    set(false);
  });
}

function up(event: PointerEvent) {
  pointers.delete(event.pointerId);
  if (!pointers.size) letGo();
}

/** The window lost the pointer (another window, a dialog of the browser's): let go. */
function lost() {
  pointers.clear();
  letGo();
}

function subscribe(listener: () => void): () => void {
  if (!installed) {
    installed = true;
    // Captured at the window, so a press is seen before anything it's on handles it.
    window.addEventListener("pointerdown", down, true);
    window.addEventListener("pointerup", up, true);
    window.addEventListener("pointercancel", up, true);
    window.addEventListener("blur", lost);
  }
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Whether a press is on, anywhere on the page, till the task after it's let go. */
export function usePressing(): boolean {
  return useSyncExternalStore(
    subscribe,
    () => pressing,
    () => false,
  );
}

/**
 * `value` as it was before a press began, while it's on; and while `waiting` (a form being
 * sent, say), as it was before that began. Otherwise `value`.
 *
 * `value` is a prop, a state, a query's data or a memo: one the component holding it doesn't
 * make afresh each time it draws (a filter's result, an object literal), which would never be
 * the one before.
 */
export function useSteady<T>(value: T, waiting = false): T {
  return useKept(value, usePressing() || waiting);
}

/** `value` as it was before `holding` began, while it's on; otherwise `value`. (`value` as
 * for `useSteady`.) */
export function useKept<T>(value: T, holding: boolean): T {
  const [shown, setShown] = useState({ value });
  if (!holding && shown.value !== value) setShown({ value });
  return holding ? shown.value : value;
}
