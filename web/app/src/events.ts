/**
 * Live updates: one EventSource on `/api/v1/events` per tab, signed in with
 * the browser session. Events carry ids and counts, never content; they
 * update the bell's count and refresh the notification queries.
 *
 * - The browser reconnects by itself when a stream ends (server restarts end
 *   them, and each lasts at most 30 minutes). Every `ready` after the first
 *   means something may have been missed, so the inbox refreshes.
 * - A tab hidden for a while closes its stream and reopens it when shown, so
 *   background tabs don't hold a connection each.
 * - If the server refuses the stream (too many tabs, or signed out), the
 *   browser stops; this checks who's signed in and tries again later.
 * - When the session has ended, the page says so (see `markSignedOut`) but
 *   stays, so nothing half typed is lost.
 */

import { useEffect } from "react";

import { iki, isSignedOut, markSignedOut, NOTIFICATIONS, queryClient, unreadQuery } from "./api";

export const EVENTS_URL = "/api/v1/events";
/** How long a hidden tab keeps its stream. */
export const HIDDEN_GRACE_MS = 5 * 60_000;
/** Before opening the stream again after the server refused it. */
export const REFUSED_RETRY_MS = 30_000;

function parse(event: MessageEvent<string>): Record<string, unknown> {
  try {
    const data: unknown = JSON.parse(event.data);
    return data && typeof data === "object" ? (data as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

function setUnread(data: Record<string, unknown>): void {
  const unread = data["unread"];
  if (typeof unread === "number") {
    queryClient.setQueryData(unreadQuery.queryKey, unread);
  }
}

function refreshInbox(): Promise<void> {
  return queryClient.invalidateQueries({ queryKey: [...NOTIFICATIONS, "list"] });
}

/** Keeps a live-update stream open while the app is mounted. */
export function useLiveUpdates(enabled: boolean): void {
  useEffect(() => {
    if (!enabled || typeof EventSource === "undefined") return;
    let source: EventSource | undefined;
    let hiddenTimer: ReturnType<typeof setTimeout> | undefined;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    let readyBefore = false;

    const close = () => {
      source?.close();
      source = undefined;
    };

    const open = () => {
      if (source || isSignedOut()) return;
      const stream = new EventSource(EVENTS_URL, { withCredentials: true });
      source = stream;
      stream.addEventListener("ready", (event: MessageEvent<string>) => {
        setUnread(parse(event));
        if (readyBefore) void refreshInbox();
        readyBefore = true;
      });
      stream.addEventListener("notification", (event: MessageEvent<string>) => {
        setUnread(parse(event));
        void refreshInbox();
      });
      stream.addEventListener("notifications_read", (event: MessageEvent<string>) => {
        setUnread(parse(event));
        void refreshInbox();
      });
      stream.addEventListener("resync", () => {
        void queryClient.invalidateQueries({ queryKey: NOTIFICATIONS });
      });
      stream.addEventListener("signed_out", () => {
        close();
        markSignedOut();
      });
      stream.addEventListener("error", () => {
        // CONNECTING: the browser is reconnecting by itself. CLOSED: it gave up.
        if (stream.readyState !== EventSource.CLOSED || source !== stream) return;
        close();
        void iki.GET("/api/v1/me").then(
          ({ response }) => {
            if (response.status === 401) {
              markSignedOut();
            } else {
              retryTimer = setTimeout(open, REFUSED_RETRY_MS * (1 + Math.random() / 2));
            }
          },
          () => {
            retryTimer = setTimeout(open, REFUSED_RETRY_MS * (1 + Math.random() / 2));
          },
        );
      });
    };

    const onVisibility = () => {
      clearTimeout(hiddenTimer);
      if (document.visibilityState === "hidden") {
        hiddenTimer = setTimeout(close, HIDDEN_GRACE_MS);
      } else if (!source) {
        clearTimeout(retryTimer);
        open();
      }
    };

    open();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      clearTimeout(hiddenTimer);
      clearTimeout(retryTimer);
      close();
    };
  }, [enabled]);
}
