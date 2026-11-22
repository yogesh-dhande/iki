/**
 * The admin area's frame (ADR 0176), matching the server's web/admin_base.html: a sidebar
 * of the area's pages, from the server's navigation.json, that leads back to Iki; then a
 * top bar with the breadcrumb and the page's own actions, over the page.
 *
 * The app's pages of the area are the router's /admin/ route's children (compute, costs,
 * webhooks and outside APIs), which says so with `staticData: { admin: true }`; each page
 * still checks its own permission.
 */

import { useQuery } from "@tanstack/react-query";
import { Link, Outlet, useMatches, useRouter, useRouterState } from "@tanstack/react-router";
import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

import navigation from "../../../server/src/iki_server/web/navigation.json";
import { meQuery, type Me } from "./api";
import { useLiveUpdates } from "./events";
import { Layout, organizationName, SignedOut } from "./layout";
import { markNavigated, MessagesProvider } from "./ui";

/** An item of the admin area's sidebar. */
export interface AdminItem {
  label: string;
  href: string;
  /** The ones of `me.can` it's offered for: any one is enough. */
  requires: (keyof Me["can"])[];
  /** The paths it's the current item for, by prefix: its `href` if left out. */
  current?: string[];
  /** The app routes it, so its link doesn't load a page. */
  app?: boolean;
  /** Its icon's SVG paths, on a 24-unit grid. */
  icon: string[];
}

export interface AdminGroup {
  label: string;
  items: AdminItem[];
}

export const ADMIN_HREF = navigation.admin.href;
export const ADMIN_GROUPS = navigation.admin.groups as AdminGroup[];

/** The groups, with the items this person may open; a group with none is left out. */
export function adminNavigation(me: Me): AdminGroup[] {
  return ADMIN_GROUPS.map((group) => ({
    ...group,
    items: group.items.filter((item) => item.requires.some((need) => me.can[need])),
  })).filter((group) => group.items.length > 0);
}

function isCurrent(item: AdminItem, pathname: string): boolean {
  return (item.current ?? [item.href]).some((prefix) => pathname.startsWith(prefix));
}

/** "page" on the item's own page, "true" on one under it (a worker's, say). */
function currentness(item: AdminItem, pathname: string): "page" | "true" | undefined {
  if (!isCurrent(item, pathname)) return undefined;
  return pathname === item.href ? "page" : "true";
}

/**
 * The app's frame: the admin area's on its pages, for someone who may open any of them,
 * and the main one everywhere else. Someone who may open none sees a page of the area,
 * which says it isn't theirs, in the main frame.
 */
export function Frame() {
  const me = useQuery(meQuery).data;
  const admin = useMatches({ select: (matches) => matches.some((m) => m.staticData.admin) });
  if (admin && me && adminNavigation(me).length > 0) return <AdminLayout me={me} />;
  return <Layout />;
}

const ActionsSlot = createContext<HTMLElement | null>(null);

/** The page's own actions, on the right of the admin area's top bar. */
export function AdminActions({ children }: { children: ReactNode }) {
  const slot = useContext(ActionsSlot);
  return slot ? createPortal(children, slot) : null;
}

function Icon({ paths }: { paths: string[] }) {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      {paths.map((d) => (
        <path key={d} d={d} />
      ))}
    </svg>
  );
}

/** A link the router marks current only on its own page, whatever the query. */
const EXACTLY = { exact: true, includeSearch: false } as const;

function SideLink({ item, pathname }: { item: AdminItem; pathname: string }) {
  const props = {
    className: "admin-link",
    "aria-current": currentness(item, pathname),
  };
  const body = (
    <>
      <Icon paths={item.icon} />
      {item.label}
    </>
  );
  // The router's own "page" only on the item's own page: under it, it's "true".
  return item.app ? (
    <Link to={item.href} activeOptions={EXACTLY} {...props}>
      {body}
    </Link>
  ) : (
    <a href={item.href} {...props}>
      {body}
    </a>
  );
}

function csrfToken(): string {
  return document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content ?? "";
}

/** "Rosalind Franklin" is "RF": the account's badge at the foot of the sidebar. */
export function initials(name: string): string {
  const words = name.split(/\s+/).filter((w) => /^[\p{L}\p{N}]/u.test(w));
  return [words[0], words.length > 1 ? words[words.length - 1] : undefined]
    .map((w) => w?.[0] ?? "")
    .join("")
    .toUpperCase();
}

function Crumbs({ groups, pathname }: { groups: AdminGroup[]; pathname: string }) {
  const item = groups.flatMap((g) => g.items).find((i) => isCurrent(i, pathname));
  let here: ReactNode = null;
  if (item?.href === pathname) {
    here = <span aria-current="page">{item.label}</span>;
  } else if (item) {
    here = item.app ? (
      <Link to={item.href} activeOptions={EXACTLY}>
        {item.label}
      </Link>
    ) : (
      <a href={item.href}>{item.label}</a>
    );
  }
  return (
    <nav className="admin-crumbs" aria-label="Breadcrumb">
      <ol>
        <li>
          <a href={ADMIN_HREF}>Admin</a>
        </li>
        {here ? <li>{here}</li> : null}
      </ol>
    </nav>
  );
}

export function AdminLayout({ me }: { me: Me }) {
  const org = organizationName();
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  const router = useRouter();
  useEffect(() => router.subscribe("onBeforeNavigate", markNavigated), [router]);
  useLiveUpdates(me.kind === "person");
  const [slot, setSlot] = useState<HTMLDivElement | null>(null);
  // On a narrow screen the pages fold under a menu button: open on the page it was opened
  // on, so closed again after each move.
  const [openOn, setOpenOn] = useState<string | null>(null);
  const menuOpen = openOn === pathname;
  const groups = adminNavigation(me);
  return (
    <>
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <div className="admin-frame">
        <nav className="admin-side" aria-label="Admin">
          <div className="admin-side-head">
            <Link className="admin-back" to="/">
              <Icon paths={["M15 6l-6 6 6 6"]} />
              Back to Iki
            </Link>
            <div className="admin-title">
              <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                <path d="M12 3 4 6v6c0 4.5 3.4 8 8 9 4.6-1 8-4.5 8-9V6z" />
              </svg>
              <span>Admin</span>
              {org ? <span className="admin-org">{org}</span> : null}
            </div>
            <button
              className="button admin-menu-button"
              type="button"
              aria-expanded={menuOpen}
              aria-controls="admin-menu"
              onClick={() => {
                setOpenOn(menuOpen ? null : pathname);
              }}
            >
              Menu
            </button>
          </div>
          <div className="admin-menu" id="admin-menu">
            {groups.map((group, i) => (
              <div key={group.label}>
                <p className="admin-group" id={`admin-group-${String(i + 1)}`}>
                  {group.label}
                </p>
                <ul className="admin-links" aria-labelledby={`admin-group-${String(i + 1)}`}>
                  {group.items.map((item) => (
                    <li key={item.href}>
                      <SideLink item={item} pathname={pathname} />
                    </li>
                  ))}
                </ul>
              </div>
            ))}
            <form className="admin-account" method="post" action="/logout/">
              <input type="hidden" name="csrfmiddlewaretoken" value={csrfToken()} />
              <a className="admin-user" href="/account/" title={`${me.email}: your account`}>
                <span className="admin-avatar" aria-hidden="true">
                  {initials(me.name || me.email)}
                </span>
                <span className="admin-user-name">{me.name || me.email}</span>
              </a>
              <button className="button button-quiet" type="submit">
                Sign out
              </button>
            </form>
          </div>
        </nav>
        <div className="admin-column">
          <header className="admin-top">
            <Crumbs groups={groups} pathname={pathname} />
            <div className="admin-actions" ref={setSlot} />
          </header>
          <main id="main" className="admin-main" tabIndex={-1}>
            <SignedOut />
            <ActionsSlot.Provider value={slot}>
              <MessagesProvider>
                <Outlet />
              </MessagesProvider>
            </ActionsSlot.Provider>
          </main>
        </div>
      </div>
    </>
  );
}

declare module "@tanstack/react-router" {
  interface StaticDataRouteOption {
    /** One of the admin area's pages, drawn in its frame. */
    admin?: boolean;
  }
}
