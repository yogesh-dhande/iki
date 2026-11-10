import { useInfiniteQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useRouter, useSearch } from "@tanstack/react-router";
import type { MouseEvent } from "react";

import {
  inboxQuery,
  markAllNotifications,
  markNotification,
  NOTIFICATIONS,
  problemOf,
  useApiMutation,
  type InboxFilter,
  type Notification,
} from "../api";
import { usePageTitle } from "../layout";
import { Button, Loading, PageHeader, ProblemAlert, RelativeTime, useMessages } from "../ui";

export function Notifications() {
  usePageTitle("Notifications");
  const filter: InboxFilter = useSearch({ from: "/notifications" }).show ?? "all";
  const inbox = useInfiniteQuery(inboxQuery(filter));
  const items = inbox.data?.pages.flatMap((p) => p.items) ?? [];
  const { say } = useMessages();
  const markAll = useApiMutation(
    (upTo: string | undefined) => markAllNotifications(upTo),
    [NOTIFICATIONS],
  );
  const anyUnread = items.some((n) => !n.read_at);

  return (
    <>
      <PageHeader
        title="Notifications"
        actions={
          <div className="actions">
            {anyUnread ? (
              <Button
                busy={markAll.isPending}
                busyText="Marking…"
                onClick={() => {
                  // Only what's on screen: anything arriving meanwhile stays unread.
                  markAll.mutate(items[0]?.created_at, {
                    onSuccess: ({ marked }) => {
                      say(
                        `Marked ${String(marked)} notification${marked === 1 ? "" : "s"} as read.`,
                      );
                    },
                  });
                }}
              >
                Mark all as read
              </Button>
            ) : null}
            <a className="button button-quiet" href="/account/notifications/">
              Email settings
            </a>
          </div>
        }
      />
      <nav className="tabs" aria-label="Show">
        <Link
          to="/notifications/"
          search={{}}
          activeOptions={{ includeSearch: true }}
          activeProps={{ "aria-current": "page" }}
        >
          All
        </Link>
        <Link
          to="/notifications/"
          search={{ show: "unread" }}
          activeOptions={{ includeSearch: true }}
          activeProps={{ "aria-current": "page" }}
        >
          Unread
        </Link>
      </nav>
      <ProblemAlert problem={markAll.error ? problemOf(markAll.error) : undefined} />
      {inbox.isPending ? (
        <Loading label="Loading your notifications…" />
      ) : inbox.isError ? (
        <ProblemAlert problem={problemOf(inbox.error)} />
      ) : items.length === 0 ? (
        <section className="card empty">
          {filter === "unread" ? (
            <p>You’re all caught up.</p>
          ) : (
            <>
              <h2>No notifications yet</h2>
              <p>
                You’ll hear here when someone adds you to a project or team, changes your role, or
                needs your approval.
              </p>
            </>
          )}
        </section>
      ) : (
        <>
          <ul className="rows notifications">
            {items.map((notification) => (
              <NotificationRow key={notification.id} notification={notification} />
            ))}
          </ul>
          {inbox.hasNextPage ? (
            <p>
              <Button
                busy={inbox.isFetchingNextPage}
                busyText="Loading…"
                onClick={() => void inbox.fetchNextPage()}
              >
                Show older
              </Button>
            </p>
          ) : null}
        </>
      )}
    </>
  );
}

/** Whether the app shows this path itself, or the server renders it. */
function useIsAppPath(): (path: string) => boolean {
  const router = useRouter();
  return (path) => {
    const matches = router.matchRoutes(path);
    const last = matches[matches.length - 1];
    return Boolean(last && last.routeId !== "__root__");
  };
}

function NotificationRow({ notification }: { notification: Notification }) {
  const unread = !notification.read_at;
  const client = useQueryClient();
  const navigate = useNavigate();
  const isAppPath = useIsAppPath();
  const toggle = useApiMutation(
    (read: boolean) => markNotification(notification.id, read),
    [NOTIFICATIONS],
  );

  const go = () => {
    if (isAppPath(notification.path)) {
      void navigate({ to: notification.path });
    } else {
      window.location.assign(notification.path);
    }
  };

  const open = async (event: MouseEvent<HTMLAnchorElement>) => {
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) {
      // Opening it elsewhere (a new tab, say): this page stays, and it's read.
      if (unread) toggle.mutate(true);
      return;
    }
    event.preventDefault();
    if (unread) {
      // Marked read before leaving, so it's read when the person comes back.
      try {
        await markNotification(notification.id, true);
        await client.invalidateQueries({ queryKey: NOTIFICATIONS });
      } catch {
        // Opening it matters more; it stays unread.
      }
    }
    go();
  };

  const meta = [
    notification.project?.name,
    notification.actor ? `by ${notification.actor.name}` : undefined,
  ].filter(Boolean);

  return (
    <li className={unread ? "row notification notification-unread" : "row notification"}>
      <div className="row-main">
        <span className="row-title">
          {unread ? <span className="unread-dot" aria-hidden="true" /> : null}
          {unread ? <span className="visually-hidden">Unread: </span> : null}
          {notification.path ? (
            <a href={notification.path} onClick={(event) => void open(event)}>
              {notification.title}
            </a>
          ) : (
            notification.title
          )}
        </span>
        {notification.body ? <span className="notification-body">{notification.body}</span> : null}
        <span className="row-meta">
          {meta.length ? `${meta.join(" · ")} · ` : ""}
          <RelativeTime value={notification.created_at} />
        </span>
      </div>
      {toggle.error ? (
        <div className="row-problem">
          <ProblemAlert problem={problemOf(toggle.error)} />
        </div>
      ) : null}
      <div className="row-actions">
        <Button
          variant="quiet"
          busy={toggle.isPending}
          onClick={() => {
            toggle.mutate(unread);
          }}
        >
          {unread ? "Mark read" : "Mark unread"}
          <span className="visually-hidden">: {notification.title}</span>
        </Button>
      </div>
    </li>
  );
}
