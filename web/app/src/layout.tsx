/**
 * The page frame, matching the server's base.html: skip link, the sidebar (ADR 0176), and
 * the main region.
 */

import { useQuery } from "@tanstack/react-query";
import { Outlet, useMatches, useRouter, useRouterState } from "@tanstack/react-router";
import { useEffect, useSyncExternalStore } from "react";

import { isSignedOut, meQuery, onSignedOutChange, signInUrl } from "./api";
import { useLiveUpdates } from "./events";
import { Brand, Sidebar } from "./sidebar";
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
      {me ? (
        <Sidebar me={me} pathname={pathname} />
      ) : (
        <header className="site-header">
          <Brand />
        </header>
      )}
      <main id="main" className={narrow ? "page-narrow" : "page-wide"} tabIndex={-1}>
        <SignedOut />
        <MessagesProvider>
          <Outlet />
        </MessagesProvider>
      </main>
    </>
  );
}

declare module "@tanstack/react-router" {
  interface StaticDataRouteOption {
    /** A single form, like "New project": the narrow column server forms use. */
    narrow?: boolean;
  }
}
