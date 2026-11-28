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
  redirect,
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
import { validateEntrySearch, validateTypePageSearch } from "./lab-lists";
import { validateRequestsSearch } from "./requests";
import { validateWorkSearch } from "./people";
import { entryQuery } from "./registries";

import { validateCostsSearch } from "./costs-search";
import { validateWorkersSearch } from "./compute";
import { validateQuarantineSearch } from "./quarantine";
import { fileQuery, validateFileSearch } from "./files";
import {
  containerQuery,
  placesQuery,
  receiptsQuery,
  reconciliationQuery,
  validateContainerSearch,
} from "./storage";
import { ProjectTabs } from "./pages/project-header";
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

// Pages fetched when they're first opened (or a link to one is pointed at), so the first page
// loads quickly. Only the frame and Home load with the app: `make web` checks what that weighs.
const Work = lazyRouteComponent(() => import("./pages/work"), "Work");
const ProjectList = lazyRouteComponent(() => import("./pages/project-list"), "ProjectList");
const ProjectNew = lazyRouteComponent(() => import("./pages/project-new"), "ProjectNew");
const ProjectOverview = lazyRouteComponent(
  () => import("./pages/project-overview"),
  "ProjectOverview",
);
const ProjectSettings = lazyRouteComponent(
  () => import("./pages/project-settings"),
  "ProjectSettings",
);
const AppDraft = lazyRouteComponent(() => import("./pages/app-draft"), "AppDraft");
const filePages = () => import("./pages/files");
const ProjectFiles = lazyRouteComponent(filePages, "ProjectFiles");
const AllFiles = lazyRouteComponent(filePages, "AllFiles");
const FilePage = lazyRouteComponent(filePages, "FilePage");
const storagePages = () => import("./pages/storage");
const StoragePage = lazyRouteComponent(storagePages, "StoragePage");
const ContainerPage = lazyRouteComponent(storagePages, "ContainerPage");
const ReceivePage = lazyRouteComponent(() => import("./pages/storage-receive"), "ReceivePage");
const ReconcilePage = lazyRouteComponent(
  () => import("./pages/storage-reconcile"),
  "ReconcilePage",
);
const entityPages = () => import("./pages/entity");
const typePages = () => import("./pages/types");
const webhookPages = () => import("./pages/webhooks");
const outsideApiPages = () => import("./pages/outside-apis");
const OrgCosts = lazyRouteComponent(() => import("./pages/costs"), "OrgCosts");
const ComputeVolumes = lazyRouteComponent(() => import("./pages/volumes"), "ComputeVolumes");
const ComputeImages = lazyRouteComponent(() => import("./pages/compute-images"), "ComputeImages");
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
const ProjectStock = lazyRouteComponent(() => import("./pages/project-stock"), "ProjectStock");
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
const RecordPages = lazyRouteComponent(entityPages, "RecordPages");
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
const revisionPages = () => import("./pages/record-revision");
const RecordRevision = lazyRouteComponent(revisionPages, "RecordRevision");
const EntryRevision = lazyRouteComponent(revisionPages, "EntryRevision");
const registryPages = () => import("./pages/registries");
const RegistryEntries = lazyRouteComponent(registryPages, "RegistryEntries");
const EntryPage = lazyRouteComponent(registryPages, "EntryPage");
const savedSearchPages = () => import("./pages/saved-search");
const SavedSearchPage = lazyRouteComponent(savedSearchPages, "SavedSearchPage");
const TypeList = lazyRouteComponent(typePages, "TypeList");
const TypeDetail = lazyRouteComponent(typePages, "TypeDetail");
const OrgWebhooks = lazyRouteComponent(webhookPages, "OrgWebhooks");
const OutsideApisSettings = lazyRouteComponent(outsideApiPages, "OutsideApisSettings");
const OrgWebhook = lazyRouteComponent(webhookPages, "OrgWebhook");
const quarantinePages = () => import("./pages/quarantine");
const QuarantinePage = lazyRouteComponent(quarantinePages, "QuarantinePage");
const QuarantinedPage = lazyRouteComponent(quarantinePages, "QuarantinedPage");
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

// The admin area (ADR 0176): its pages are in its frame, not the main layout.
const adminRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "admin",
  staticData: { admin: true },
});

const costsRoute = createRoute({
  getParentRoute: () => adminRoute,
  path: "costs",
  // ?month=2026-11&by=team: another month, grouped another way.
  validateSearch: validateCostsSearch,
  component: OrgCosts,
});

const computeRoute = createRoute({ getParentRoute: () => adminRoute, path: "compute" });

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

const computeImagesRoute = createRoute({
  getParentRoute: () => computeRoute,
  path: "images",
  component: ComputeImages,
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

const projectStockRoute = createRoute({
  getParentRoute: () => projectRoute,
  path: "stock",
  component: ProjectStock,
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

const projectFilesRoute = createRoute({
  getParentRoute: () => projectRoute,
  path: "files",
  // ?q=, ?format=, ?acquired_from=, ?acquired_to=, ?sample=, ?run=, ?sort= (ADR 0177).
  validateSearch: validateFileSearch,
  component: ProjectFiles,
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
  getParentRoute: () => projectAutomationRoute,
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
  getParentRoute: () => projectAutomationRoute,
  path: "workflows/new",
  // ?workflow=acme/protein_translation.translate_and_characterize: its form.
  validateSearch: (search: Record<string, unknown>): { workflow?: string | undefined } =>
    typeof search["workflow"] === "string" && search["workflow"]
      ? { workflow: search["workflow"] }
      : {},
  loader: ({ context }) => context.queryClient.query(typesQuery),
  component: NewWorkflowRun,
});

const projectTriggersRoute = createRoute({
  getParentRoute: () => projectAutomationRoute,
  path: "triggers",
  // ?trigger=iki/protein.compute_new_protein_properties: only what that trigger fired.
  validateSearch: (search: Record<string, unknown>): { trigger?: string | undefined } =>
    typeof search["trigger"] === "string" && search["trigger"]
      ? { trigger: search["trigger"] }
      : {},
  component: ProjectTriggers,
});

// A project's Automation tab (ADR 0176): its workflows, triggers and webhooks, each a tab
// of its own under it. Its own address opens the first of them the reader may see.
const projectAutomationRoute = createRoute({
  getParentRoute: () => projectRoute,
  path: "automation",
});

const projectAutomationIndexRoute = createRoute({
  getParentRoute: () => projectAutomationRoute,
  path: "/",
  beforeLoad: async ({ context, params }) => {
    const project = await context.queryClient.query(projectQuery(params.slug));
    redirect({
      throw: true,
      to:
        !project.can.read && project.can.manage_webhooks
          ? "/projects/$slug/automation/webhooks/"
          : "/projects/$slug/automation/workflows/",
      params,
      replace: true,
    });
  },
});

// Importing a spreadsheet (ADR 0178): ?type=acme/lab.Sample, its rows' type; then its steps.
const sheetPages = () => import("./pages/spreadsheet-import");
const projectSheetImportRoute = createRoute({
  getParentRoute: () => projectRoute,
  path: "entities/import",
  validateSearch: validateNewEntitySearch,
  loader: ({ context }) => context.queryClient.query(typesQuery),
  component: lazyRouteComponent(sheetPages, "SpreadsheetImportStart"),
});

const projectSheetImportPageRoute = createRoute({
  getParentRoute: () => projectRoute,
  path: "entities/import/$importId",
  loader: ({ context }) => context.queryClient.query(typesQuery),
  component: lazyRouteComponent(sheetPages, "SpreadsheetImportPage"),
});

const projectSettingsRoute = createRoute({
  getParentRoute: () => projectRoute,
  path: "settings",
  component: ProjectSettings,
});

// The starters a project's Admins set it up from (ADR 0198): Settings' tab stays current.
const projectStartersRoute = createRoute({
  getParentRoute: () => projectRoute,
  path: "settings/starters",
  component: lazyRouteComponent(() => import("./pages/project-starters"), "ProjectStarters"),
});

const projectImportRoute = createRoute({
  getParentRoute: () => projectRoute,
  path: "imports/$importId",
  component: lazyRouteComponent(() => import("./pages/project-import"), "ImportPage"),
});

const projectAppDraftRoute = createRoute({
  getParentRoute: () => projectRoute,
  path: "apps/drafts/$draft",
  component: AppDraft,
});

const projectWebhooksRoute = createRoute({
  getParentRoute: () => projectAutomationRoute,
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
  // A lab list's options are a registry's (ADR 0270): ?project=atlas or ?registry= says whose.
  validateSearch: validateTypePageSearch,
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
  return choices ? <ChooseProject records={choices} /> : <RecordPages />;
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

// The record as it was at one of its revisions, read only (ADR 0189): what a witness opens
// from the evidence they sign, and its audit trail's rows open.
const entityRevisionRoute = createRoute({
  getParentRoute: () => entityRoute,
  path: "revisions/$number",
  component: RecordRevision,
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
  // ?project=atlas: a superseded lab list option's, whose records the project moves.
  validateSearch: validateEntrySearch,
  loader: ({ context, params }) =>
    Promise.all([
      context.queryClient.query(entryQuery(params.ref)),
      context.queryClient.query(typesQuery),
    ]),
  component: EntryPage,
});

// A registry entry as it was at one of its revisions, read only, as its list tells it.
const entryRevisionRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "entries/$ref/revisions/$number",
  loader: ({ context, params }) =>
    Promise.all([
      context.queryClient.query(entryQuery(params.ref)),
      context.queryClient.query(typesQuery),
    ]),
  component: EntryRevision,
});

const filesRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "files",
  // As a project's Files, and ?project=atlas.
  validateSearch: validateFileSearch,
  loader: ({ context }) => context.queryClient.query(projectsQuery),
  component: AllFiles,
});

const fileRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "files/$id",
  // The page says why if it can't be read.
  loader: ({ context, params }) =>
    context.queryClient.query(fileQuery(params.id)).catch(() => undefined),
  component: FilePage,
});

const storageRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "storage",
  loader: ({ context }) => context.queryClient.query(placesQuery).catch(() => undefined),
  component: StoragePage,
});

const containerRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "storage/$ref",
  // A position to mark: where a scanned tube is.
  validateSearch: validateContainerSearch,
  // The page says why if it can't be read.
  loader: ({ context, params }) =>
    context.queryClient.query(containerQuery(params.ref)).catch(() => undefined),
  component: ContainerPage,
});

const receiveRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "storage/receive/$ref",
  loader: ({ context, params }) =>
    context.queryClient.query(receiptsQuery(params.ref)).catch(() => undefined),
  component: ReceivePage,
});

const reconcileRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "storage/reconciliations/$id",
  loader: ({ context, params }) =>
    context.queryClient.query(reconciliationQuery(params.id)).catch(() => undefined),
  component: ReconcilePage,
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

const orgWebhooksRoute = createRoute({ getParentRoute: () => adminRoute, path: "webhooks" });

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
  getParentRoute: () => adminRoute,
  path: "outside-apis",
  loader: ({ context }) => context.queryClient.query(outsideApisQuery),
  component: OutsideApisSettings,
});

// Admin → Quarantine (ADR 0217): the files the malware scan holds, and one's finding.
const quarantineRoute = createRoute({ getParentRoute: () => adminRoute, path: "quarantine" });

const quarantineListRoute = createRoute({
  getParentRoute: () => quarantineRoute,
  path: "/",
  // ?status=released&state=infected&q=plate: released ones, malware findings, by name.
  validateSearch: validateQuarantineSearch,
  component: QuarantinePage,
});

const quarantinedRoute = createRoute({
  getParentRoute: () => quarantineRoute,
  path: "$decisionId",
  component: QuarantinedPage,
});

const routeTree = rootRoute.addChildren([
  savedSearchRoute,
  homeRoute,
  notificationsRoute,
  workRoute,
  projectsRoute.addChildren([
    projectListRoute,
    projectNewRoute,
    projectRoute.addChildren([
      projectOverviewRoute,
      projectMembersRoute,
      projectEntitiesRoute,
      projectRequestsRoute,
      projectStockRoute,
      projectEntityNewRoute,
      projectSheetImportRoute,
      projectSheetImportPageRoute,
      projectRunsRoute,
      projectRunNewRoute,
      projectFilesRoute,
      projectAutomationRoute.addChildren([
        projectAutomationIndexRoute,
        projectWorkflowsRoute,
        projectWorkflowNewRoute,
        projectTriggersRoute,
        projectWebhooksRoute.addChildren([projectWebhookListRoute, projectWebhookRoute]),
      ]),
      projectSettingsRoute,
      projectStartersRoute,
      projectImportRoute,
      projectAppDraftRoute,
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
      entityRevisionRoute,
    ]),
  ]),
  requestsRoute,
  runsRoute,
  filesRoute,
  fileRoute,
  storageRoute,
  receiveRoute,
  reconcileRoute,
  containerRoute,
  registryEntriesRoute,
  entryRoute,
  entryRevisionRoute,
  runRoute,
  batchRoute,

  workflowRunRoute,
  adminRoute.addChildren([
    costsRoute,
    computeRoute.addChildren([
      computeOverviewRoute,
      computeWorkersRoute,
      computeQuotasRoute,
      computeSlurmRoute,
      computeVolumesRoute,
      computeImagesRoute,
    ]),
    orgWebhooksRoute.addChildren([orgWebhookListRoute, orgWebhookRoute]),
    outsideApisRoute,
    quarantineRoute.addChildren([quarantineListRoute, quarantinedRoute]),
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
