/**
 * The page frame, matching the server's base.html: skip link, header with
 * the main navigation and sign-out, and the main region.
 */

import { useQuery } from "@tanstack/react-query";
import { Link, Outlet, useMatches, useRouter, useRouterState } from "@tanstack/react-router";
import { useEffect, useSyncExternalStore } from "react";

import navigation from "../../../server/src/iki_server/web/navigation.json";
import { isSignedOut, meQuery, onSignedOutChange, signInUrl, unreadQuery, type Me } from "./api";
import { useLiveUpdates } from "./events";
import { isAppPath } from "./search";
import { SearchPalette } from "./search-palette";
import { markNavigated, MessagesProvider } from "./ui";

export function organizationName(): string {
  return (
    document.querySelector<HTMLMetaElement>('meta[name="iki-organization"]')?.content.trim() ?? ""
  );
}

/**
 * Whether accounts come only from the identity provider, over SCIM (ADR 0026). Then a
 * first sign-in from someone it hasn't provisioned is refused, so the forms that add
 * people say to provision them rather than to sign in.
 */
export function provisionedOverScim(): boolean {
  return (
    document.querySelector<HTMLMetaElement>('meta[name="iki-provisioning"]')?.content === "scim"
  );
}

/** When someone new can be added: once they have an account. */
export function onceTheyHaveAnAccount(): string {
  return provisionedOverScim()
    ? "once your identity provider provisions them"
    : "once they’ve signed in to Iki for the first time";
}

/** Sets the document title the way server pages do: "Page · Organization". */
export function usePageTitle(title: string | undefined): void {
  useEffect(() => {
    if (title === undefined) return;
    const org = organizationName();
    document.title = org ? `${title} · ${org}` : title;
  }, [title]);
}

/** An item of the header's main navigation (the server's navigation.json, which its
 * base.html renders too, so the two headers can't drift apart). */
export interface NavItem {
  label: string;
  href: string;
  /** The one of `me.can` it needs, if any. */
  requires?: keyof Me["can"];
  /** The paths it's the current section for, by prefix: its `href` if left out. */
  current?: string[];
  /** The app routes it, so its link doesn't load a page. */
  app?: boolean;
}

export const MAIN_NAVIGATION = navigation.main as NavItem[];

function MainNavigation({ me, pathname }: { me: Me; pathname: string }) {
  return (
    <nav className="site-nav" aria-label="Main">
      {MAIN_NAVIGATION.filter((item) => !item.requires || me.can[item.requires]).map((item) => {
        const current = (item.current ?? [item.href]).some((p) => pathname.startsWith(p));
        const props = { "aria-current": current ? ("page" as const) : undefined };
        return item.app ? (
          <Link key={item.href} to={item.href} {...props}>
            {item.label}
          </Link>
        ) : (
          <a key={item.href} href={item.href} {...props}>
            {item.label}
          </a>
        );
      })}
    </nav>
  );
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

/** Shown when Iki found out, in the background, that the session ended. */
function SignedOut() {
  const signedOut = useSyncExternalStore(onSignedOutChange, isSignedOut);
  if (!signedOut) return null;
  return (
    <div className="alert alert-warning" role="alert">
      <p>
        You’ve been signed out. <a href={signInUrl()}>Sign in again</a> to carry on; this page stays
        as it is until you do.
      </p>
    </div>
  );
}

export function Layout() {
  const me = useQuery(meQuery).data;
  const org = organizationName();
  const pathname = useRouterState({ select: (state) => state.location.pathname });
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
            <MainNavigation me={me} pathname={pathname} />
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
        <SignedOut />
        <MessagesProvider>
          <Outlet />
        </MessagesProvider>
      </main>
      <footer className="site-footer">
        <span>Iki</span>
      </footer>
      {me ? (
        <SearchPalette
          navigate={(href) => {
            if (isAppPath(href)) void router.navigate({ href });
            else window.location.assign(href);
          }}
        />
      ) : null}
    </>
  );
}

declare module "@tanstack/react-router" {
  interface StaticDataRouteOption {
    /** A single form, like "New project": the narrow column server forms use. */
    narrow?: boolean;
  }
}
