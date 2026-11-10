/**
 * The page frame, matching the server's base.html: skip link, header with
 * the main navigation and sign-out, and the main region.
 */

import { useQuery } from "@tanstack/react-query";
import { Link, Outlet, useMatches, useRouter } from "@tanstack/react-router";
import { useEffect } from "react";

import { meQuery, unreadQuery } from "./api";
import { useLiveUpdates } from "./events";
import { markNavigated, MessagesProvider } from "./ui";

export function organizationName(): string {
  return (
    document.querySelector<HTMLMetaElement>('meta[name="iki-organization"]')?.content.trim() ?? ""
  );
}

/** Sets the document title the way server pages do: "Page · Organization". */
export function usePageTitle(title: string | undefined): void {
  useEffect(() => {
    if (title === undefined) return;
    const org = organizationName();
    document.title = org ? `${title} · ${org}` : title;
  }, [title]);
}

function csrfToken(): string {
  return document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content ?? "";
}

function Bell() {
  const unread = useQuery(unreadQuery).data ?? 0;
  const shown = unread > 99 ? "99+" : String(unread);
  return (
    <Link
      className="header-notifications"
      to="/notifications/"
      activeProps={{ "aria-current": "page" }}
      aria-label={unread ? `Notifications, ${String(unread)} unread` : "Notifications"}
    >
      <svg
        viewBox="0 0 24 24"
        aria-hidden="true"
        focusable="false"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <path d="M6 8a6 6 0 1 1 12 0c0 7 3 9 3 9H3s3-2 3-9" />
        <path d="M10.3 21a1.94 1.94 0 0 0 3.4 0" />
      </svg>
      {unread ? (
        <span className="badge" aria-hidden="true">
          {shown}
        </span>
      ) : null}
    </Link>
  );
}

export function Layout() {
  const me = useQuery(meQuery).data;
  const org = organizationName();
  const narrow = useMatches({ select: (matches) => matches.some((m) => m.staticData.narrow) });
  const router = useRouter();
  useEffect(() => router.subscribe("onBeforeNavigate", markNavigated), [router]);
  useLiveUpdates(me?.kind === "person");
  return (
    <>
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <header className="site-header">
        <Link className="brand" to="/">
          <svg className="brand-mark" viewBox="0 0 32 32" aria-hidden="true" focusable="false">
            <circle cx="16" cy="16" r="14" fill="none" stroke="currentColor" strokeWidth="2.5" />
            <circle cx="16" cy="16" r="5" fill="currentColor" />
          </svg>
          <span className="brand-name">iki</span>
          {org ? <span className="brand-org">{org}</span> : null}
        </Link>
        {me ? (
          <>
            <nav className="site-nav" aria-label="Main">
              <Link to="/projects/" activeProps={{ "aria-current": "page" }}>
                Projects
              </Link>
              <a href="/teams/">Teams</a>
              {me.can.manage_org ? <a href="/people/">People</a> : null}
              {me.can.manage_org ? <a href="/settings/sign-in/">Settings</a> : null}
              {me.can.read_audit ? <a href="/audit/">Audit</a> : null}
            </nav>
            <form className="header-actions" method="post" action="/logout/">
              <input type="hidden" name="csrfmiddlewaretoken" value={csrfToken()} />
              <Bell />
              <a className="header-user" href="/account/" title={`${me.email}: your account`}>
                {me.name || me.email}
              </a>
              <button className="button button-quiet" type="submit">
                Sign out
              </button>
            </form>
          </>
        ) : null}
      </header>
      <main id="main" className={narrow ? "page-narrow" : "page-wide"} tabIndex={-1}>
        <MessagesProvider>
          <Outlet />
        </MessagesProvider>
      </main>
      <footer className="site-footer">
        <span>Iki</span>
      </footer>
    </>
  );
}

declare module "@tanstack/react-router" {
  interface StaticDataRouteOption {
    /** A single form, like "New project": the narrow column server forms use. */
    narrow?: boolean;
  }
}
