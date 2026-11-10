/**
 * The app's pages, at the same addresses the server-rendered pages had, so
 * links and bookmarks keep working. The server answers every one of these
 * paths with the app's page; anything else is a server-rendered page.
 */

import {
  createRootRouteWithContext,
  createRoute,
  createRouter,
  Outlet,
  type RouterHistory,
} from "@tanstack/react-router";
import type { QueryClient } from "@tanstack/react-query";

import { meQuery, projectQuery, queryClient, rolesQuery } from "./api";
import { Layout } from "./layout";
import { ErrorPage, NotFound } from "./pages/errors";
import { Home } from "./pages/home";
import { Notifications } from "./pages/notifications";
import { ProjectList } from "./pages/project-list";
import { ProjectMembers } from "./pages/project-members";
import { ProjectNew } from "./pages/project-new";
import { ProjectOverview, ProjectTabs } from "./pages/project-overview";
import { ProjectSettings } from "./pages/project-settings";
import { OrgWebhook, OrgWebhooks, ProjectWebhook, ProjectWebhooks } from "./pages/webhooks";

const rootRoute = createRootRouteWithContext<{ queryClient: QueryClient }>()({
  // Who's signed in, and the role names every page shows, before the first render.
  loader: ({ context }) =>
    Promise.all([context.queryClient.query(meQuery), context.queryClient.query(rolesQuery)]),
  component: Layout,
  notFoundComponent: NotFound,
  errorComponent: ErrorPage,
});

const homeRoute = createRoute({ getParentRoute: () => rootRoute, path: "/", component: Home });

const notificationsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "notifications",
  // ?show=unread: only unread ones.
  validateSearch: (search: Record<string, unknown>): { show?: "unread" } =>
    search["show"] === "unread" ? { show: "unread" } : {},
  component: Notifications,
});

const projectsRoute = createRoute({ getParentRoute: () => rootRoute, path: "projects" });

const projectListRoute = createRoute({
  getParentRoute: () => projectsRoute,
  path: "/",
  component: ProjectList,
});

const projectNewRoute = createRoute({
  getParentRoute: () => projectsRoute,
  path: "new",
  staticData: { narrow: true },
  // ?team=<slug> preselects the team, for "New project" on a team's page.
  validateSearch: (search: Record<string, unknown>): { team?: string } =>
    typeof search["team"] === "string" ? { team: search["team"] } : {},
  component: ProjectNew,
});

const projectRoute = createRoute({
  getParentRoute: () => projectsRoute,
  path: "$slug",
  loader: ({ context, params }) => context.queryClient.query(projectQuery(params.slug)),
  component: () => (
    <>
      <ProjectTabs />
      <Outlet />
    </>
  ),
});

const projectOverviewRoute = createRoute({
  getParentRoute: () => projectRoute,
  path: "/",
  component: ProjectOverview,
});

const projectMembersRoute = createRoute({
  getParentRoute: () => projectRoute,
  path: "members",
  component: ProjectMembers,
});

const projectSettingsRoute = createRoute({
  getParentRoute: () => projectRoute,
  path: "settings",
  component: ProjectSettings,
});

const projectWebhooksRoute = createRoute({
  getParentRoute: () => projectRoute,
  path: "webhooks",
});

const projectWebhookListRoute = createRoute({
  getParentRoute: () => projectWebhooksRoute,
  path: "/",
  component: ProjectWebhooks,
});

const projectWebhookRoute = createRoute({
  getParentRoute: () => projectWebhooksRoute,
  path: "$webhookId",
  component: ProjectWebhook,
});

const settingsRoute = createRoute({ getParentRoute: () => rootRoute, path: "settings" });

const orgWebhooksRoute = createRoute({ getParentRoute: () => settingsRoute, path: "webhooks" });

const orgWebhookListRoute = createRoute({
  getParentRoute: () => orgWebhooksRoute,
  path: "/",
  component: OrgWebhooks,
});

const orgWebhookRoute = createRoute({
  getParentRoute: () => orgWebhooksRoute,
  path: "$webhookId",
  component: OrgWebhook,
});

const routeTree = rootRoute.addChildren([
  homeRoute,
  notificationsRoute,
  projectsRoute.addChildren([
    projectListRoute,
    projectNewRoute,
    projectRoute.addChildren([
      projectOverviewRoute,
      projectMembersRoute,
      projectSettingsRoute,
      projectWebhooksRoute.addChildren([projectWebhookListRoute, projectWebhookRoute]),
    ]),
  ]),
  settingsRoute.addChildren([orgWebhooksRoute.addChildren([orgWebhookListRoute, orgWebhookRoute])]),
]);

/** The app's router; tests pass a memory history. */
export function createAppRouter(history?: RouterHistory) {
  return createRouter({
    routeTree,
    context: { queryClient },
    trailingSlash: "always",
    defaultPreload: "intent",
    // Loaders read through TanStack Query, which does the caching.
    defaultPreloadStaleTime: 0,
    scrollRestoration: true,
    ...(history ? { history } : {}),
  });
}

export const router = createAppRouter();

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
