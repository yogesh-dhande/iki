/**
 * The sidebar (ADR 0176), as the server's base.html draws it: both read navigation.json,
 * so the two can't drift apart. From the top: search everything (⌘K), My work and the
 * inbox, Entities (open or closed, as the reader left it), Requests and Runs, the reader's
 * saved views and projects, the organization's pages, and their account. On a narrow
 * screen it's a drawer, opened from the bar above the page.
 */

import { unwrap } from "@ikisci/sdk";
import { queryOptions, useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { Link, useMatch, useNavigate } from "@tanstack/react-router";
import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from "react";

import navigation from "../../../server/src/iki_server/web/navigation.json";
import { iki, projectPagesQuery, projectQuery, unreadQuery, type Me } from "./api";
import { adminNavigation } from "./admin";
import { entityQuery } from "./entities";
import { countsQuery } from "./lists";
import { swatchOf } from "./project-color";
import { openSearch } from "./search";
import { TypeDot } from "./type-color";

/** An item of the sidebar's, as navigation.json lists it (see iki_server.web.navigation). */
export interface NavItem {
  label: string;
  href: string;
  /** The one of `me.can` it needs, if any. */
  requires?: keyof Me["can"];
  /** The paths it's the current item for, by prefix: its `href` if left out. */
  current?: string[];
  /** The app routes it, so its link doesn't load a page. */
  app?: boolean;
  icon?: IconName;
}

export type IconName = keyof typeof navigation.icons;

const SPEC = navigation as unknown as {
  top: (NavItem & { badge?: "unread" })[];
  entities: { label: string; icon: IconName; all: NavItem; type: string };
  work: NavItem[];
  saved: NavItem;
  projects: { label: string; href: string; shown: number; new: NavItem; all: NavItem };
  organization: {
    label: string;
    items: NavItem[];
    admin: { icon: IconName };
  };
  account: NavItem[];
  admin: { label: string; href: string };
};

/** Below this width the sidebar is a drawer (iki.css). */
const DRAWER = "(max-width: 56rem)";
/** Whether Entities is open, once the reader has opened or closed it: the server reads it
 * too. Until then it's open on the entities' own pages only, where its types are the way
 * on, and closed elsewhere, where they'd push the rest of the sidebar out of sight. */
export const ENTITIES_COOKIE = "iki_entities";

/** Whether Entities starts open on a page: as the reader left it, else on its pages. */
export function entitiesOpen(pathname: string, left: string | undefined): boolean {
  if (left === "open" || left === "closed") return left === "open";
  return pathname.startsWith(SPEC.entities.all.href);
}

/** Scrolls a list so the item in it is in view, if it isn't: itself, not the page. */
export function reveal(list: HTMLElement, item: HTMLElement): void {
  const box = list.getBoundingClientRect();
  const at = item.getBoundingClientRect();
  // A row's height of room around it, so it isn't flush against the edge.
  const room = at.height;
  if (at.top < box.top) list.scrollTop -= box.top - at.top + room;
  else if (at.bottom > box.bottom) list.scrollTop += at.bottom - box.bottom + room;
}

export function Icon({ name }: { name: IconName }) {
  return (
    <svg className="icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      {navigation.icons[name].map((d) => (
        <path key={d} d={d} />
      ))}
    </svg>
  );
}

/** A link the sidebar shows: where it goes, and the paths it's current for. */
interface Shown {
  key: string;
  label: string;
  href: string;
  app: boolean;
  prefixes: string[];
  icon?: IconName | undefined;
}

function shown(item: NavItem, key = item.href): Shown {
  return {
    key,
    label: item.label,
    href: item.href,
    app: Boolean(item.app),
    prefixes: item.current ?? [item.href],
    icon: item.icon,
  };
}

/** The one a page is in: the longest prefix of its path wins, the first of those as long. */
export function currentOf(path: string, links: Shown[]): string | undefined {
  let best: { length: number; key: string } | undefined;
  for (const link of links) {
    for (const prefix of link.prefixes) {
      if (path.startsWith(prefix) && (!best || prefix.length > best.length)) {
        best = { length: prefix.length, key: link.key };
      }
    }
  }
  return best?.key;
}

/** Admin, to the admin area (ADR 0176), for whoever may open any page of it: its own frame
 * and sidebar take them through the rest, and its pages don't draw this sidebar. */
export function adminLink(me: Me): Shown | null {
  if (adminNavigation(me).length === 0) return null;
  return {
    key: "admin",
    label: SPEC.admin.label,
    href: SPEC.admin.href,
    // The server's: it sends them on to the first page of the area they may open.
    app: false,
    prefixes: [SPEC.admin.href],
    icon: SPEC.organization.admin.icon,
  };
}

/** A person's mark: the first letters of their first two names. */
export function initials(name: string): string {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((word) => word.slice(0, 1))
    .join("")
    .toUpperCase();
}

function readCookie(name: string): string | undefined {
  return document.cookie
    .split("; ")
    .find((c) => c.startsWith(`${name}=`))
    ?.slice(name.length + 1);
}

function writeCookie(name: string, value: string): void {
  const secure = window.location.protocol === "https:" ? "; secure" : "";
  document.cookie = `${name}=${value}; path=/; max-age=31536000; samesite=lax${secure}`;
}

/** The reader's own saved searches, in every project they can read: the saved views. */
export const savedViewsQuery = queryOptions({
  queryKey: ["saved-searches", "mine"],
  meta: { background: true },
  queryFn: async () =>
    unwrap(
      await iki.GET("/api/v1/saved-searches", {
        params: { query: { mine: true, limit: SPEC.projects.shown } },
      }),
    ),
});

/** The active projects they have a role in, as Home lists them: the same request. */
const myProjects = (): ReturnType<typeof projectPagesQuery> =>
  projectPagesQuery({ member: true, archived: false });

function NavLink({
  link,
  current,
  className = "nav-item",
  children,
  ...rest
}: {
  link: Shown;
  current: boolean;
  className?: string;
  children: ReactNode;
  "aria-label"?: string;
  title?: string;
}) {
  const navigate = useNavigate();
  // An anchor, not the router's Link: the sidebar says which is current, and the router's
  // would also call Admin current on the page it opens, beside the page's own item.
  return (
    <a
      href={link.href}
      className={className}
      aria-current={current ? "page" : undefined}
      {...rest}
      onClick={
        link.app
          ? (event) => {
              if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey) return;
              if (event.altKey) return;
              event.preventDefault();
              void navigate({ to: link.href });
            }
          : undefined
      }
    >
      {children}
    </a>
  );
}

export function Sidebar({ me, pathname }: { me: Me; pathname: string }) {
  const [drawer, setDrawer] = useState(false);
  const opener = useRef<HTMLButtonElement>(null);
  const closer = useRef<HTMLButtonElement>(null);
  const backdrop = useRef<HTMLDivElement>(null);
  // Dismissed, the menu button has the focus again, once it's back within reach; when the
  // reader goes somewhere, the page they go to takes it.
  const refocus = useRef(false);
  const dismiss = useCallback(() => {
    refocus.current = true;
    setDrawer(false);
  }, []);
  // Going somewhere closes it.
  const [shownFor, setShownFor] = useState(pathname);
  if (shownFor !== pathname) {
    setShownFor(pathname);
    setDrawer(false);
  }
  // So does a screen wide enough for it to stay.
  useEffect(() => {
    // jsdom, which the tests run in, has none.
    if (typeof window.matchMedia !== "function") return;
    const narrow = window.matchMedia(DRAWER);
    const changed = (event: MediaQueryListEvent) => {
      if (!event.matches) setDrawer(false);
    };
    narrow.addEventListener("change", changed);
    return () => {
      narrow.removeEventListener("change", changed);
    };
  }, []);
  // Open, it's a dialog: the rest of the page is out of reach, and Escape or a click beside
  // it dismisses it.
  useEffect(() => {
    if (!drawer) return;
    closer.current?.focus();
    const sidebar = document.getElementById("sidebar");
    const outside = [...(sidebar?.parentElement?.children ?? [])].filter(
      (el): el is HTMLElement =>
        el instanceof HTMLElement && el !== sidebar && el !== backdrop.current,
    );
    for (const el of outside) el.inert = true;
    const pressed = (event: KeyboardEvent) => {
      if (event.key === "Escape") dismiss();
    };
    const away = backdrop.current;
    const menu = opener.current;
    document.addEventListener("keydown", pressed);
    away?.addEventListener("click", dismiss);
    return () => {
      for (const el of outside) el.inert = false;
      document.removeEventListener("keydown", pressed);
      away?.removeEventListener("click", dismiss);
      if (refocus.current) menu?.focus();
      refocus.current = false;
    };
  }, [drawer, dismiss]);

  return (
    <>
      <header className="topbar">
        <button
          ref={opener}
          className="topbar-menu"
          type="button"
          aria-controls="sidebar"
          aria-expanded={drawer}
          onClick={() => {
            setDrawer(true);
          }}
        >
          <Icon name="menu" />
          <span className="visually-hidden">Menu</span>
        </button>
        <Brand />
      </header>
      <div
        className="sidebar"
        id="sidebar"
        data-open={drawer ? "true" : undefined}
        {...(drawer ? { role: "dialog", "aria-modal": true, "aria-label": "Menu" } : {})}
      >
        <div className="sidebar-brand">
          <Brand />
          <button ref={closer} className="sidebar-close" type="button" onClick={dismiss}>
            <Icon name="close" />
            <span className="visually-hidden">Close menu</span>
          </button>
        </div>
        <SearchButton />
        <MainNavigation me={me} pathname={pathname} />
        <Account me={me} pathname={pathname} />
      </div>
      <div ref={backdrop} className="sidebar-backdrop" hidden={!drawer} />
    </>
  );
}

export function Brand() {
  const org =
    document.querySelector<HTMLMetaElement>('meta[name="iki-organization"]')?.content.trim() ?? "";
  return (
    <Link className="brand" to="/" activeOptions={{ exact: true }}>
      <svg className="brand-mark" viewBox="0 0 32 32" aria-hidden="true" focusable="false">
        <circle cx="16" cy="16" r="14" fill="none" stroke="currentColor" strokeWidth="2.5" />
        <circle cx="16" cy="16" r="5" fill="currentColor" />
      </svg>
      <span className="brand-name">iki</span>
      {org ? <span className="brand-org">{org}</span> : null}
    </Link>
  );
}

const APPLE = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);

/** Search everything: the palette listens for `iki:search` and, opening, cancels it. Where
 * nothing does, the list of every record, which searches them too. */
/** Search everything: opens the palette, which binds ⌘K (Ctrl+K) itself, once a page. */
function SearchButton() {
  return (
    <button
      className="sidebar-search"
      type="button"
      aria-keyshortcuts="Meta+K Control+K"
      onClick={openSearch}
    >
      <Icon name="search" />
      <span className="sidebar-search-label">Search everything</span>
      <kbd className="kbd" aria-hidden="true">
        {APPLE ? "⌘K" : "Ctrl K"}
      </kbd>
    </button>
  );
}

function MainNavigation({ me, pathname }: { me: Me; pathname: string }) {
  const unread = useQuery(unreadQuery).data ?? 0;
  const [left, setLeft] = useState(() => readCookie(ENTITIES_COOKIE));
  const open = entitiesOpen(pathname, left);
  const navRef = useRef<HTMLElement>(null);
  // As the server's sidebar names it: there's one sidebar on a page.
  const entitiesId = "nav-entities";
  // How many of each type they can read, across their projects, as All entities counts
  // them: where counting would be slow, each type that can have records, uncounted.
  const counts = useQuery({ ...countsQuery(), meta: { background: true } });
  const projects = useInfiniteQuery({ ...myProjects(), meta: { background: true } });
  const saved = useQuery(savedViewsQuery).data ?? [];

  const top = SPEC.top.map((item) => ({ ...shown(item), badge: item.badge }));
  const allEntities = shown(SPEC.entities.all);
  // Each type with records the reader can read, which only the counts say: if they can't
  // be read, All entities alone.
  const types: (Shown & { type: string; count: number | null })[] =
    counts.data?.types.map((t) => ({
      ...shown({ label: t.label, href: `${SPEC.entities.type}${t.type}/`, app: allEntities.app }),
      type: t.type,
      count: t.count,
    })) ?? [];
  const work = SPEC.work.map((item) => shown(item));

  const here = pathname.startsWith(SPEC.projects.href)
    ? (pathname.slice(SPEC.projects.href.length).split("/")[0] ?? "")
    : "";
  const first = (projects.data?.pages[0]?.items ?? []).slice(0, SPEC.projects.shown);
  const further = useQuery({
    ...projectQuery(here),
    enabled:
      Boolean(here) &&
      here !== "new" &&
      Boolean(projects.data) &&
      !first.some((p) => p.slug === here),
  }).data;
  const mine = [
    ...first,
    ...(further?.my_role && !further.archived && !first.some((p) => p.slug === here)
      ? [further]
      : []),
  ].map((p) => ({
    ...shown({ label: p.name, href: `${SPEC.projects.href}${p.slug}/`, app: true }),
    swatch: swatchOf(p.id),
  }));
  const newProject =
    !SPEC.projects.new.requires || me.can[SPEC.projects.new.requires]
      ? shown(SPEC.projects.new, "new-project")
      : null;
  const allProjects = shown(SPEC.projects.all, "all-projects");
  const admin = adminLink(me);
  const organization = [
    ...SPEC.organization.items.map((item) => shown(item)),
    ...(admin ? [admin] : []),
  ];

  // A record's page is in its type: the record's in the cache, which its page loaded.
  const record = useMatch({ from: "/entities/$ref", shouldThrow: false });
  const recordType = useQuery({
    ...entityQuery(record?.params.ref ?? "", record?.search.project),
    enabled: false,
  }).data?.type;
  const recordTypeLink =
    record && recordType ? types.find((t) => t.type === recordType) : undefined;
  const current =
    recordTypeLink?.key ??
    currentOf(pathname, [
      ...top,
      allEntities,
      ...types,
      ...work,
      ...mine,
      allProjects,
      ...organization,
      ...SPEC.account.map((item) => shown(item)),
      ...(newProject ? [newProject] : []),
    ]);
  const icon = (link: Shown) => (link.icon ? <Icon name={link.icon} /> : null);
  // Its current item in view, where the list scrolls: on load, as what's above it arrives
  // (types, saved views, projects), and as the reader moves on.
  const above = [types.length, saved.length, mine.length].join();
  useEffect(() => {
    const list = navRef.current;
    const item = list?.querySelector<HTMLElement>('[aria-current="page"]');
    if (list && item) reveal(list, item);
  }, [current, open, above]);

  return (
    <nav className="sidebar-nav" aria-label="Main" ref={navRef}>
      {top.map((link) => {
        const badge = link.badge === "unread" && unread ? unread : 0;
        return (
          <NavLink key={link.key} link={link} current={current === link.key}>
            {icon(link)}
            <span className="nav-label">{link.label}</span>
            {/* Named by what it shows, "Inbox 3", then "unread": a flex row draws none of the
                spaces between them, but a screen reader reads them. */}
            {badge ? (
              <>
                {" "}
                <span className="nav-badge">{badge > 99 ? "99+" : String(badge)}</span>{" "}
                <span className="visually-hidden">unread</span>
              </>
            ) : null}
          </NavLink>
        );
      })}
      <div className="nav-gap" />
      <button
        className="nav-item nav-toggle"
        type="button"
        aria-expanded={open}
        aria-controls={entitiesId}
        onClick={() => {
          const now = open ? "closed" : "open";
          writeCookie(ENTITIES_COOKIE, now);
          setLeft(now);
        }}
      >
        <Icon name={SPEC.entities.icon} />
        <span className="nav-label">{SPEC.entities.label}</span>
        <Icon name="chevron" />
      </button>
      <div className="nav-sub" id={entitiesId} hidden={!open}>
        <NavLink link={allEntities} current={current === allEntities.key}>
          <span className="nav-label">{allEntities.label}</span>
          {counts.data?.total != null ? <Count n={counts.data.total} /> : null}
        </NavLink>
        {types.map((link) => (
          <NavLink key={link.key} link={link} current={current === link.key}>
            <TypeDot type={link.type} />
            <span className="nav-label">{link.label}</span>
            {link.count !== null ? <Count n={link.count} /> : null}
          </NavLink>
        ))}
      </div>
      {work.map((link) => (
        <NavLink key={link.key} link={link} current={current === link.key}>
          {icon(link)}
          <span className="nav-label">{link.label}</span>
        </NavLink>
      ))}
      {saved.length ? (
        <Group label={navigation.saved.label}>
          {saved.map((s) => (
            <NavLink
              key={s.id}
              link={shown({ label: s.name, href: `${SPEC.saved.href}${s.id}/`, app: true })}
              current={false}
            >
              <Icon name="saved" />
              <span className="nav-label">{s.name}</span>
            </NavLink>
          ))}
        </Group>
      ) : null}
      <Group
        label={SPEC.projects.label}
        action={
          newProject ? (
            <NavLink
              link={newProject}
              current={current === newProject.key}
              className="nav-add"
              title={newProject.label}
            >
              <Icon name="plus" />
              <span className="visually-hidden">{newProject.label}</span>
            </NavLink>
          ) : null
        }
      >
        {mine.map((link) => (
          <NavLink key={link.key} link={link} current={current === link.key}>
            <span className="swatch swatch-large" data-swatch={link.swatch} aria-hidden="true" />
            <span className="nav-label">{link.label}</span>
          </NavLink>
        ))}
        <NavLink
          link={allProjects}
          current={current === allProjects.key}
          className="nav-item nav-quiet"
        >
          {icon(allProjects)}
          <span className="nav-label">{allProjects.label}</span>
        </NavLink>
      </Group>
      <Group label={SPEC.organization.label}>
        {organization.map((link) => (
          <NavLink key={link.key} link={link} current={current === link.key}>
            {icon(link)}
            <span className="nav-label">{link.label}</span>
          </NavLink>
        ))}
      </Group>
    </nav>
  );
}

/** How many: after a space, which a flex row doesn't draw but a screen reader reads. */
function Count({ n }: { n: number }) {
  return (
    <>
      {" "}
      <span className="nav-count">{n.toLocaleString()}</span>
    </>
  );
}

function Group({
  label,
  action,
  children,
}: {
  label: string;
  action?: ReactNode;
  children: ReactNode;
}) {
  // Named by aria-label, not aria-labelledby: Playwright's Firefox crashes working out the
  // name a page's element takes from another where a plugin's frame is (see test_browser).
  return (
    <div className="nav-group" role="group" aria-label={label}>
      <div className="nav-heading">
        <span aria-hidden="true">{label}</span>
        {action}
      </div>
      {children}
    </div>
  );
}

function csrfToken(): string {
  return document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content ?? "";
}

/** The account menu, at the foot of the sidebar and of the admin area's: opens over it, and
 * closes on Escape, a click elsewhere, or moving on from it. */
export function Account({ me, pathname }: { me: Me; pathname: string }) {
  const [open, setOpen] = useState(false);
  const button = useRef<HTMLButtonElement>(null);
  const box = useRef<HTMLDivElement>(null);
  const menuId = useId();
  const name = me.name || me.email;
  const [shownFor, setShownFor] = useState(pathname);
  if (shownFor !== pathname) {
    setShownFor(pathname);
    setOpen(false);
  }
  useEffect(() => {
    const account = box.current;
    if (!open || !account) return;
    account.querySelector<HTMLElement>(".account-menu a, .account-menu button")?.focus();
    const pressed = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      // Not the drawer it's in, too.
      event.stopPropagation();
      setOpen(false);
      button.current?.focus();
    };
    const left = (event: FocusEvent) => {
      if (!account.contains(event.relatedTarget as Node | null)) setOpen(false);
    };
    const clicked = (event: MouseEvent) => {
      if (!account.contains(event.target as Node)) setOpen(false);
    };
    account.addEventListener("keydown", pressed);
    account.addEventListener("focusout", left);
    document.addEventListener("click", clicked);
    return () => {
      account.removeEventListener("keydown", pressed);
      account.removeEventListener("focusout", left);
      document.removeEventListener("click", clicked);
    };
  }, [open]);
  const links = SPEC.account.map((item) => shown(item));
  const current = currentOf(pathname, links);
  return (
    <div className="account" ref={box}>
      <button
        ref={button}
        className="account-button"
        type="button"
        aria-expanded={open}
        aria-controls={menuId}
        title={me.email}
        onClick={() => {
          setOpen(!open);
        }}
      >
        <span className="avatar" data-swatch={swatchOf(me.id)} aria-hidden="true">
          {initials(name)}
        </span>
        <span className="nav-label">{name}</span>
        <Icon name="account" />
      </button>
      <div className="account-menu" id={menuId} hidden={!open}>
        <p className="account-who">
          <span className="account-who-name">{name}</span>
          <span className="account-who-email">{me.email}</span>
        </p>
        {links.map((link) => (
          <a
            key={link.key}
            className="account-item"
            href={link.href}
            aria-current={current === link.key ? "page" : undefined}
          >
            {link.label}
          </a>
        ))}
        <form className="account-sign-out" method="post" action="/logout/">
          <input type="hidden" name="csrfmiddlewaretoken" value={csrfToken()} />
          <button className="account-item" type="submit">
            Sign out
          </button>
        </form>
      </div>
    </div>
  );
}
