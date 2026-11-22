/**
 * The app's pages, at the same addresses the server-rendered pages had, so
 * links and bookmarks keep working. The server answers every one of these
 * paths with the app's page; anything else is a server-rendered page.
 */

import {
  createRootRouteWithContext,
  createRoute,
  createRouter,
  lazyRouteComponent,
  Outlet,
  Navigate,
  type RouterHistory,
} from "@tanstack/react-router";
import type { QueryClient } from "@tanstack/react-query";

import { meQuery, projectQuery, projectsQuery, queryClient, rolesQuery, watchQuery } from "./api";
import { outsideApisQuery } from "./outside-apis";
import { Frame } from "./admin";
import { ChooseProject, choicesIn, EntityError, ErrorPage, NotFound } from "./pages/errors";
import { Home } from "./pages/home";
import { componentsQuery } from "./components";
import {
  entityQuery,
  savedSearchesQuery,
  typesQuery,
  validateEntityPageSearch,
  validateEntitySearch,
  validateNewEntitySearch,
} from "./entities";
import { validateAuditSearch } from "./audit-trail";
import { validateLineageSearch } from "./lineage";
import { validateListSearch } from "./lists";
import { validateRequestsSearch } from "./requests";
import { validateWorkSearch, Work } from "./pages/work";
import { entryQuery } from "./registries";

import { validateCostsSearch } from "./costs-search";
import { validateWorkersSearch } from "./compute";
import { ProjectList } from "./pages/project-list";
import { ProjectNew } from "./pages/project-new";
import { ProjectOverview, ProjectTabs } from "./pages/project-overview";
import { ProjectSettings } from "./pages/project-settings";
import { AppDraft } from "./pages/app-draft";
import { validateNewRunSearch } from "./pages/new-run-search";
import {
  actionsQuery,
  batchQuery,
  validateRunsSearch,
  runQuery,
  STATUS,
  workflowRunQuery,
  type RunStatus,
} from "./runs";

// Pages fetched when they're first opened, so the first page loads quickly.
const entityPages = () => import("./pages/entity");
const typePages = () => import("./pages/types");
const webhookPages = () => import("./pages/webhooks");
const outsideApiPages = () => import("./pages/outside-apis");
const OrgCosts = lazyRouteComponent(() => import("./pages/costs"), "OrgCosts");
const ComputeVolumes = lazyRouteComponent(() => import("./pages/volumes"), "ComputeVolumes");
const ComputeWorkersPage = lazyRouteComponent(
  () => import("./pages/compute-workers"),
  "ComputeWorkersPage",
);
const ComputeQuotasPage = lazyRouteComponent(
  () => import("./pages/compute-quotas"),
  "ComputeQuotasPage",
);
const ComputeSlurmPage = lazyRouteComponent(
  () => import("./pages/compute-slurm"),
  "ComputeSlurmPage",
);
const ComputeOverviewPage = lazyRouteComponent(
  () => import("./pages/compute"),
  "ComputeOverviewPage",
);
const Notifications = lazyRouteComponent(() => import("./pages/notifications"), "Notifications");
const ProjectMembers = lazyRouteComponent(
  () => import("./pages/project-members"),
  "ProjectMembers",
);
const ProjectEntities = lazyRouteComponent(
  () => import("./pages/project-entities"),
  "ProjectEntities",
);
const ProjectRequestsPage = lazyRouteComponent(
  () => import("./pages/project-requests"),
  "ProjectRequestsPage",
);
const entityFormPages = () => import("./pages/entity-form");
const runPages = () => import("./pages/runs");
const ProjectRuns = lazyRouteComponent(runPages, "ProjectRuns");
const AllRuns = lazyRouteComponent(runPages, "AllRuns");
const listPages = () => import("./pages/lists");
const AllEntities = lazyRouteComponent(listPages, "AllEntities");
const TypeTable = lazyRouteComponent(listPages, "TypeTable");
const AllRequests = lazyRouteComponent(listPages, "AllRequests");
const RunPage = lazyRouteComponent(runPages, "RunPage");
const NewRun = lazyRouteComponent(() => import("./pages/new-run"), "NewRun");
const BatchPage = lazyRouteComponent(() => import("./pages/batches"), "BatchPage");
const WorkflowRunPage = lazyRouteComponent(
  () => import("./pages/workflow-runs"),
  "WorkflowRunPage",
);
const workflowPages = () => import("./pages/workflows");
const ProjectWorkflows = lazyRouteComponent(workflowPages, "ProjectWorkflows");
const NewWorkflowRun = lazyRouteComponent(workflowPages, "NewWorkflowRun");
const ProjectTriggers = lazyRouteComponent(() => import("./pages/triggers"), "ProjectTriggers");
const EntityOverview = lazyRouteComponent(entityPages, "EntityOverview");
const NewEntity = lazyRouteComponent(entityFormPages, "NewEntity");
const EditEntity = lazyRouteComponent(entityFormPages, "EditEntity");
const EntityAudit = lazyRouteComponent(() => import("./pages/entity-audit"), "EntityAudit");
const EntityLineage = lazyRouteComponent(() => import("./pages/lineage"), "EntityLineage");
const EntityViewer = lazyRouteComponent(() => import("./pages/entity-viewer"), "EntityViewer");
const EntityComponent = lazyRouteComponent(
  () => import("./pages/entity-component"),
  "EntityComponent",
);
const EntityApp = lazyRouteComponent(() => import("./pages/entity-app"), "EntityApp");
const registryPages = () => import("./pages/registries");
const RegistryEntries = lazyRouteComponent(registryPages, "RegistryEntries");
const EntryPage = lazyRouteComponent(registryPages, "EntryPage");
const savedSearchPages = () => import("./pages/saved-searches");
const SavedSearchPage = lazyRouteComponent(savedSearchPages, "SavedSearchPage");
const TypeList = lazyRouteComponent(typePages, "TypeList");
const TypeDetail = lazyRouteComponent(typePages, "TypeDetail");
const OrgWebhooks = lazyRouteComponent(webhookPages, "OrgWebhooks");
const OutsideApisSettings = lazyRouteComponent(outsideApiPages, "OutsideApisSettings");
const OrgWebhook = lazyRouteComponent(webhookPages, "OrgWebhook");
const ProjectWebhooks = lazyRouteComponent(webhookPages, "ProjectWebhooks");
const ProjectWebhook = lazyRouteComponent(webhookPages, "ProjectWebhook");

const rootRoute = createRootRouteWithContext<{ queryClient: QueryClient }>()({
  // Who's signed in, and the role names every page shows, before the first render.
  loader: ({ context }) =>
    Promise.all([context.queryClient.query(meQuery), context.queryClient.query(rolesQuery)]),
  component: Frame,
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

const workRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "work",
  // ?open=true: work open to your lab roles; ?project=, ?type=, ?state=, ?sort=updated.
  validateSearch: validateWorkSearch,
  component: Work,
});

// A saved search, by its own address: the sidebar's saved views (ADR 0176) link here.
const savedSearchRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "saved-searches/$savedId",
  loader: async ({ context, params }) => {
    const { openSavedSearch } = await savedSearchPages();
    await openSavedSearch(context.queryClient, params.savedId);
  },
  component: SavedSearchPage,
});

const costsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "costs",
  staticData: { admin: true },
  // ?month=2026-11&by=team: another month, grouped another way.
  validateSearch: validateCostsSearch,
  component: OrgCosts,
});

const computeRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "compute",
  staticData: { admin: true },
});

const computeOverviewRoute = createRoute({
  getParentRoute: () => computeRoute,
  path: "/",
  component: ComputeOverviewPage,
});

const computeWorkersRoute = createRoute({
  getParentRoute: () => computeRoute,
  path: "workers",
  // ?tier=backend: only that tier's workers.
  validateSearch: validateWorkersSearch,
  component: ComputeWorkersPage,
});

const computeQuotasRoute = createRoute({
  getParentRoute: () => computeRoute,
  path: "quotas",
  component: ComputeQuotasPage,
});

const computeSlurmRoute = createRoute({
  getParentRoute: () => computeRoute,
  path: "slurm",
  component: ComputeSlurmPage,
});

const computeVolumesRoute = createRoute({
  getParentRoute: () => computeRoute,
  path: "volumes",
  component: ComputeVolumes,
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

const projectEntitiesRoute = createRoute({
  getParentRoute: () => projectRoute,
  path: "entities",
  // ?type=iki/dna.DNASequence: one type and its subtypes; ?show=archived|all.
  validateSearch: validateEntitySearch,
  // All the toolbar and the rows above the grid show, before they're drawn: nothing there
  // arrives late and moves what someone is reaching for.
  loader: ({ context, params }) =>
    Promise.all([
      context.queryClient.query(typesQuery),
      // Plugins' other views of the results, and the project's saved searches: the page
      // shows without them if they can't be listed.
      context.queryClient.query(componentsQuery).catch(() => undefined),
      context.queryClient
        .query(projectQuery(params.slug))
        .then((project) => context.queryClient.query(savedSearchesQuery(project.id)))
        .catch(() => undefined),
    ]),
  component: ProjectEntities,
});

const projectRequestsRoute = createRoute({
  getParentRoute: () => projectRoute,
  path: "requests",
  // ?side=received|sent: the requests sent to it as a service, or those it sent (ADR 0132);
  // the rest as the entities grid's.
  validateSearch: validateRequestsSearch,
  // Its grid's toolbar draws once, as the entities grid's does: with the plugins' other
  // views of the results, if they can be listed.
  loader: ({ context }) =>
    Promise.all([
      context.queryClient.query(typesQuery),
      context.queryClient.query(componentsQuery).catch(() => undefined),
    ]),
  component: ProjectRequestsPage,
});

const projectEntityNewRoute = createRoute({
  getParentRoute: () => projectRoute,
  path: "entities/new",
  // ?type=iki/dna.DNASequence: its form; without one, a choice of types first.
  validateSearch: validateNewEntitySearch,
  loader: ({ context }) => context.queryClient.query(typesQuery),
  component: NewEntity,
});

const projectRunsRoute = createRoute({
  getParentRoute: () => projectRoute,
  path: "runs",
  // ?status=failed: only runs with that status.
  validateSearch: (search: Record<string, unknown>): { status?: RunStatus } =>
    typeof search["status"] === "string" && search["status"] in STATUS
      ? { status: search["status"] as RunStatus }
      : {},
  component: ProjectRuns,
});

const projectRunNewRoute = createRoute({
  getParentRoute: () => projectRoute,
  path: "runs/new",
  // ?entities=PRT-000001,PRT-000002: what it runs on; ?action=iki/protein.compute_properties.
  validateSearch: validateNewRunSearch,
  loader: ({ context }) => context.queryClient.query(typesQuery),
  component: NewRun,
});

const WORKFLOW_STATUSES = ["running", "succeeded", "failed", "cancelled"] as const;

const projectWorkflowsRoute = createRoute({
  getParentRoute: () => projectRoute,
  path: "workflows",
  // ?status=failed: only workflow runs with that status.
  validateSearch: (
    search: Record<string, unknown>,
  ): { status?: (typeof WORKFLOW_STATUSES)[number] } => {
    const status = WORKFLOW_STATUSES.find((s) => s === search["status"]);
    return status ? { status } : {};
  },
  component: ProjectWorkflows,
});

const projectWorkflowNewRoute = createRoute({
  getParentRoute: () => projectRoute,
  path: "workflows/new",
  // ?workflow=iki/protein.translate_and_characterize: its form.
  validateSearch: (search: Record<string, unknown>): { workflow?: string | undefined } =>
    typeof search["workflow"] === "string" && search["workflow"]
      ? { workflow: search["workflow"] }
      : {},
  loader: ({ context }) => context.queryClient.query(typesQuery),
  component: NewWorkflowRun,
});

const projectTriggersRoute = createRoute({
  getParentRoute: () => projectRoute,
  path: "triggers",
  // ?trigger=iki/protein.compute_new_protein_properties: only what that trigger fired.
  validateSearch: (search: Record<string, unknown>): { trigger?: string | undefined } =>
    typeof search["trigger"] === "string" && search["trigger"]
      ? { trigger: search["trigger"] }
      : {},
  component: ProjectTriggers,
});

const projectSettingsRoute = createRoute({
  getParentRoute: () => projectRoute,
  path: "settings",
  component: ProjectSettings,
});

const projectImportRoute = createRoute({
  getParentRoute: () => projectRoute,
  path: "imports/$importId",
  component: lazyRouteComponent(() => import("./pages/project-imports"), "ImportPage"),
});

const projectAppDraftRoute = createRoute({
  getParentRoute: () => projectRoute,
  path: "apps/drafts/$draft",
  component: AppDraft,
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

const typesRoute = createRoute({ getParentRoute: () => rootRoute, path: "types" });

const typeListRoute = createRoute({
  getParentRoute: () => typesRoute,
  path: "/",
  loader: ({ context }) => context.queryClient.query(typesQuery),
  component: TypeList,
});

// A type's name is `namespace/plugin.Type`: the rest of the path, `/` and all (ADR 0113).
const typeRoute = createRoute({
  getParentRoute: () => typesRoute,
  path: "$",
  loader: ({ context }) => context.queryClient.query(typesQuery),
  component: TypeDetail,
});

// Every record its reader can read, across projects (ADR 0176); a type's table; a record.
const entitiesRoute = createRoute({ getParentRoute: () => rootRoute, path: "entities" });

/** What the lists across projects read before they draw: the types, and the projects their
 * Project filter offers, so nothing in the toolbar arrives late. */
const listLoader = ({ context }: { context: { queryClient: QueryClient } }) =>
  Promise.all([
    context.queryClient.query(typesQuery),
    context.queryClient.query(projectsQuery),
    context.queryClient.query(componentsQuery).catch(() => undefined),
  ]);

const allEntitiesRoute = createRoute({
  getParentRoute: () => entitiesRoute,
  path: "/",
  // ?type=, ?project=atlas, ?q=, ?filters=, ?sort=: as a project's Entities tab, across them.
  validateSearch: validateListSearch,
  loader: listLoader,
  component: AllEntities,
});

const typeTablesRoute = createRoute({ getParentRoute: () => entitiesRoute, path: "types" });

const typeTablesIndexRoute = createRoute({
  getParentRoute: () => typeTablesRoute,
  path: "/",
  component: () => <Navigate to="/entities/" replace />,
});

// A type's name is `namespace/plugin.Type`: the rest of the path, `/` and all (ADR 0113).
const typeTableRoute = createRoute({
  getParentRoute: () => typeTablesRoute,
  path: "$",
  validateSearch: validateListSearch,
  loader: listLoader,
  component: TypeTable,
});

const requestsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "requests",
  validateSearch: validateListSearch,
  loader: listLoader,
  component: AllRequests,
});

const runsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "runs",
  // ?status=failed, ?project=atlas.
  validateSearch: validateRunsSearch,
  loader: ({ context }) =>
    Promise.all([
      context.queryClient.query(projectsQuery),
      context.queryClient.query(actionsQuery),
    ]),
  component: AllRuns,
});

const entityRoute = createRoute({
  getParentRoute: () => entitiesRoute,
  path: "$ref",
  // ?project=atlas: that project's record, when the HRID is a registry entry's (ADR 0115).
  validateSearch: validateEntityPageSearch,
  loaderDeps: ({ search }) => ({ project: search.project }),
  loader: async ({ context, params, deps }) => {
    const [choices] = await Promise.all([
      // An HRID that several of the reader's projects have a record of: the page asks
      // which, as an answer rather than an error.
      context.queryClient.query(entityQuery(params.ref, deps.project)).then(
        // Whether the reader watches it, so its actions are all in place when they show:
        // a button arriving later would move the others along.
        async (entity) => {
          if (entity.owner_project) {
            await context.queryClient.query(watchQuery(entity.id)).catch(() => undefined);
          }
          return null;
        },
        (error: unknown) => {
          const records = choicesIn(error);
          if (records) return records;
          throw error;
        },
      ),
      context.queryClient.query(typesQuery),
      // Plugins' tabs: the page shows without them if they can't be listed.
      context.queryClient.query(componentsQuery).catch(() => undefined),
    ]);
    return { choices };
  },
  component: EntityOrChoice,
  errorComponent: EntityError,
});

function EntityOrChoice() {
  const { choices } = entityRoute.useLoaderData();
  return choices ? <ChooseProject records={choices} /> : <Outlet />;
}

const entityOverviewRoute = createRoute({
  getParentRoute: () => entityRoute,
  path: "/",
  component: EntityOverview,
});

const entityEditRoute = createRoute({
  getParentRoute: () => entityRoute,
  path: "edit",
  component: EditEntity,
});

// Its audit trail (ADR 0176): ?view=fields, and narrowed by ?person= and ?field=.
const entityAuditRoute = createRoute({
  getParentRoute: () => entityRoute,
  path: "audit",
  validateSearch: validateAuditSearch,
  component: EntityAudit,
});

const entityLineageRoute = createRoute({
  getParentRoute: () => entityRoute,
  path: "lineage",
  validateSearch: validateLineageSearch,
  component: EntityLineage,
});

const entityViewerRoute = createRoute({
  getParentRoute: () => entityRoute,
  path: "viewers/$viewer",
  component: EntityViewer,
});

// A component's name is `namespace/plugin.component`, `/` and all.
const entityComponentRoute = createRoute({
  getParentRoute: () => entityRoute,
  path: "components/$",
  loader: ({ context }) => context.queryClient.query(componentsQuery),
  component: EntityComponent,
});

const entityAppRoute = createRoute({
  getParentRoute: () => entityRoute,
  path: "apps/$app",
  component: EntityApp,
});

// A registry's entries, for its registrars (ADR 0115).
const registryEntriesRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "registries/$registryId/entries",
  loader: ({ context }) => context.queryClient.query(typesQuery),
  component: RegistryEntries,
});

// A registry entry, by its ID or HRID: for those with a record of it, and its registrars.
const entryRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "entries/$ref",
  loader: ({ context, params }) =>
    Promise.all([
      context.queryClient.query(entryQuery(params.ref)),
      context.queryClient.query(typesQuery),
    ]),
  component: EntryPage,
});

const runRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "runs/$ref",
  loader: ({ context, params }) =>
    Promise.all([
      context.queryClient.query(runQuery(params.ref)),
      context.queryClient.query(actionsQuery),
    ]),
  component: RunPage,
});

const batchRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "batches/$ref",
  // ?status=failed: only its runs with that status.
  validateSearch: (search: Record<string, unknown>): { status?: RunStatus } =>
    typeof search["status"] === "string" && search["status"] in STATUS
      ? { status: search["status"] as RunStatus }
      : {},
  loader: ({ context, params }) =>
    Promise.all([
      context.queryClient.query(batchQuery(params.ref)),
      context.queryClient.query(actionsQuery),
    ]),
  component: BatchPage,
});

const workflowRunRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "workflow-runs/$ref",
  loader: ({ context, params }) => context.queryClient.query(workflowRunQuery(params.ref)),
  component: WorkflowRunPage,
});

const settingsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "settings",
  staticData: { admin: true },
});

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

const outsideApisRoute = createRoute({
  getParentRoute: () => settingsRoute,
  path: "outside-apis",
  loader: ({ context }) => context.queryClient.query(outsideApisQuery),
  component: OutsideApisSettings,
});

const routeTree = rootRoute.addChildren([
  savedSearchRoute,
  homeRoute,
  notificationsRoute,
  workRoute,
  costsRoute,
  computeRoute.addChildren([
    computeOverviewRoute,
    computeWorkersRoute,
    computeQuotasRoute,
    computeSlurmRoute,
    computeVolumesRoute,
  ]),
  projectsRoute.addChildren([
    projectListRoute,
    projectNewRoute,
    projectRoute.addChildren([
      projectOverviewRoute,
      projectMembersRoute,
      projectEntitiesRoute,
      projectRequestsRoute,
      projectEntityNewRoute,
      projectRunsRoute,
      projectRunNewRoute,
      projectWorkflowsRoute,
      projectWorkflowNewRoute,
      projectTriggersRoute,
      projectSettingsRoute,
      projectImportRoute,
      projectAppDraftRoute,
      projectWebhooksRoute.addChildren([projectWebhookListRoute, projectWebhookRoute]),
    ]),
  ]),
  typesRoute.addChildren([typeListRoute, typeRoute]),
  entitiesRoute.addChildren([
    allEntitiesRoute,
    typeTablesRoute.addChildren([typeTablesIndexRoute, typeTableRoute]),
    entityRoute.addChildren([
      entityOverviewRoute,
      entityEditRoute,
      entityAuditRoute,
      entityLineageRoute,
      entityViewerRoute,
      entityComponentRoute,
      entityAppRoute,
    ]),
  ]),
  requestsRoute,
  runsRoute,
  registryEntriesRoute,
  entryRoute,
  runRoute,
  batchRoute,

  workflowRunRoute,
  settingsRoute.addChildren([
    orgWebhooksRoute.addChildren([orgWebhookListRoute, orgWebhookRoute]),
    outsideApisRoute,
  ]),
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
