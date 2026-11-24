import { unwrap } from "@ikisci/sdk";
import { useQuery, useSuspenseQuery } from "@tanstack/react-query";
import { Link, useNavigate, useParams, useRouterState } from "@tanstack/react-router";
import { useLayoutEffect, useState } from "react";
import { Button as AriaButton, Menu, MenuItem, MenuTrigger, Popover } from "react-aria-components";

import {
  iki,
  meQuery,
  membersQuery,
  problemOf,
  projectQuery,
  rolesQuery,
  useApiMutation,
  type Project,
} from "../api";
import { dashboardQuery } from "../apps";
import { entityQuery, typesQuery } from "../entities";
import { usePageTitle } from "../layout";
import { TopBar } from "../page-top";
import { swatchColor } from "../project-color";
import { TabStrip } from "../tab-strip";
import { countsQuery } from "../lists";
import { useRequestSides } from "../requests";
import { Settling, useSettled } from "../settling";
import { Badge, Button, Loading, PageTitle, ProblemAlert, useMessages } from "../ui";
import { AppPanel } from "./app";
import { ProjectCosts } from "./cost-table";
import { PluginSections } from "./plugin-sections";
import { About, Automation, EntityCounts, Members, RecentChanges } from "./project-cards";
import { ProjectNotifications } from "./project-notifications";
import { ProjectRequests, RequestsSettled } from "./project-intake";

/** The project the current page is about, from the `$slug` in its address. */
export function useProject(): Project {
  const { slug } = useParams({ from: "/projects/$slug" });
  return useSuspenseQuery(projectQuery(slug)).data;
}

const tab = { "aria-current": "page" } as const;

/** "an Editor", "a Viewer": a role's label in a sentence. */
function withArticle(label: string): string {
  return `${/^[AEIOU]/i.test(label) ? "an" : "a"} ${label}`;
}

/** How many there are, after a tab's name; nothing until it's known. */
function TabCount({ count }: { count: number | undefined }) {
  // The space keeps it apart from the name, for screen readers: "Entities 10".
  return count === undefined ? null : (
    <>
      {" "}
      <span className="tab-count">{count}</span>
    </>
  );
}

/** The project's heading and tabs, shared by its pages: where it is, what it's called, who's
 * in it, and its tabs. Automation's three are a row of their own under it. */
export function ProjectTabs() {
  const project = useProject();
  const sides = useRequestSides(project);
  const path = useRouterState({ select: (state) => state.location.pathname });
  const section = path.slice(`/projects/${project.slug}/`.length).split("/")[0] ?? "";
  const inAutomation = section === "automation";
  const role = useQuery(rolesQuery).data?.find((r) => r.name === project.my_role);
  // What decides which tabs there are and how wide each is. Until each has answered, or
  // failed, the tabs aren't drawn: one pressed as they arrived would move under the finger,
  // and a press would end on its neighbour, or on nothing.
  const read = {
    members: useQuery(membersQuery(project.id)),
    counts: useQuery({ ...countsQuery(project.id), enabled: project.can.read }),
    // The types installed: a plugin's tab is there or not by them.
    types: useQuery(typesQuery),
  };
  const members = read.members.data;
  const counts = read.counts.data;
  const types = read.types.data;
  const settled =
    Object.values(read).every((q) => !q.isLoading) && (sides.ready || read.types.isError);
  // Its request types' records: unknown if any of them is uncounted.
  const requestCounts =
    counts && types
      ? counts.types
          .filter((c) => types.find((t) => t.name === c.type)?.request)
          .map((c) => c.count)
      : undefined;
  const requests =
    requestCounts && !requestCounts.includes(null)
      ? requestCounts.reduce<number>((sum, c) => sum + (c ?? 0), 0)
      : undefined;
  // What a tab's width changes with: the tab you're on is brought into view again.
  const widths = `${String(counts?.total)} ${String(requests)} ${String(members?.length)}`;
  const summary = [
    project.description.split("\n")[0]?.trim(),
    members ? `${String(members.length)} ${members.length === 1 ? "member" : "members"}` : "",
    role ? `you’re ${withArticle(role.label)}` : "you’re not a member",
  ].filter(Boolean);
  return (
    <>
      {/* Where it is, and what can be done from anywhere in it: in the bar across the top of
          the page (ADR 0176). Its name is the current page on its Overview. */}
      <TopBar
        crumbs={[
          <Link to="/projects/">Projects</Link>,
          <Link
            to="/projects/$slug/"
            params={{ slug: project.slug }}
            activeOptions={{ exact: true }}
          >
            {project.name}
          </Link>,
        ]}
        actions={
          <>
            {project.can.read ? <ProjectNotifications project={project} /> : null}
            <NewMenu project={project} />
          </>
        }
      />
      <div className="project-heading">
        <div className="project-name">
          <span
            className="swatch swatch-large"
            style={{ background: swatchColor(project.id) }}
            aria-hidden="true"
          />
          <PageTitle>
            {project.name}
            {project.archived ? (
              <>
                {" "}
                <Badge muted>Archived</Badge>
              </>
            ) : null}
            {project.allow_sharing ? null : (
              <>
                {" "}
                <Badge muted>Not shared outside this project</Badge>
              </>
            )}
          </PageTitle>
        </div>
        <p className="project-summary">
          {summary.join(" · ")}
          {" · "}
          <a href={`/teams/${project.team.slug}/`}>{project.team.name}</a>
        </p>
      </div>
      <TabStrip
        label="Project"
        className="project-tabs"
        settled={settled}
        sure={[
          "Overview",
          ...(project.can.read ? ["Entities", "Runs", "Files"] : []),
          ...(project.can.read || project.can.manage_webhooks ? ["Automation"] : []),
          "Members",
        ]}
        widths={widths}
      >
        <Link
          to="/projects/$slug/"
          params={{ slug: project.slug }}
          activeOptions={{ exact: true }}
          activeProps={tab}
        >
          Overview
        </Link>
        {project.can.read ? (
          <Link to="/projects/$slug/entities/" params={{ slug: project.slug }} activeProps={tab}>
            Entities
            <TabCount count={counts?.total ?? undefined} />
          </Link>
        ) : null}
        {project.can.read && types?.some((t) => t.name === "iki/reagents.StockItem") ? (
          <Link to="/projects/$slug/stock/" params={{ slug: project.slug }} activeProps={tab}>
            Stock
          </Link>
        ) : null}
        {/* For a service, or a project that asks one (ADR 0132). */}
        {sides.received || sides.sent ? (
          <Link to="/projects/$slug/requests/" params={{ slug: project.slug }} activeProps={tab}>
            Requests
            <TabCount count={requests} />
          </Link>
        ) : null}
        {project.can.read ? (
          <Link to="/projects/$slug/runs/" params={{ slug: project.slug }} activeProps={tab}>
            Runs
          </Link>
        ) : null}
        {project.can.read ? (
          <Link to="/projects/$slug/files/" params={{ slug: project.slug }} activeProps={tab}>
            Files
          </Link>
        ) : null}
        {/* It opens the first of its three the reader may see, and is current on any. */}
        {project.can.read || project.can.manage_webhooks ? (
          <Link to="/projects/$slug/automation/" params={{ slug: project.slug }} activeProps={tab}>
            Automation
          </Link>
        ) : null}
        <Link to="/projects/$slug/members/" params={{ slug: project.slug }} activeProps={tab}>
          Members
          <TabCount count={members?.length} />
        </Link>
        {/* Org Admins choose a new project's registry, without a role in it (ADR 0115). */}
        {project.can.manage || project.can.change_registry ? (
          <Link to="/projects/$slug/settings/" params={{ slug: project.slug }} activeProps={tab}>
            Settings
          </Link>
        ) : null}
      </TabStrip>
      {inAutomation ? (
        <nav className="subtabs" aria-label="Automation">
          {project.can.read ? (
            <Link
              to="/projects/$slug/automation/workflows/"
              params={{ slug: project.slug }}
              activeProps={tab}
            >
              Workflows
            </Link>
          ) : null}
          {project.can.read ? (
            <Link
              to="/projects/$slug/automation/triggers/"
              params={{ slug: project.slug }}
              activeProps={tab}
            >
              Triggers
            </Link>
          ) : null}
          {project.can.manage_webhooks ? (
            <Link
              to="/projects/$slug/automation/webhooks/"
              params={{ slug: project.slug }}
              activeProps={tab}
            >
              Webhooks
            </Link>
          ) : null}
        </nav>
      ) : null}
    </>
  );
}

/** What can be made in the project from its page: a record, a run, a workflow's run. */
function NewMenu({ project }: { project: Project }) {
  const navigate = useNavigate();
  const params = { slug: project.slug };
  const items = [
    ...(project.can.edit && !project.archived
      ? [{ id: "entity", label: "Entity", to: "/projects/$slug/entities/new/" } as const]
      : []),
    ...(project.can.run.length
      ? [
          { id: "run", label: "Run", to: "/projects/$slug/runs/new/" } as const,
          {
            id: "workflow",
            label: "Workflow run",
            to: "/projects/$slug/automation/workflows/new/",
          } as const,
        ]
      : []),
  ];
  if (!items.length) return null;
  return (
    <MenuTrigger>
      <AriaButton className="button button-primary" aria-label={`New in ${project.name}`}>
        <svg className="icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
          <path d="M12 5v14M5 12h14" />
        </svg>
        New
        <svg className="icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
          <path d="m6 9 6 6 6-6" />
        </svg>
      </AriaButton>
      <Popover className="menu-popover" placement="bottom end">
        <Menu
          className="menu"
          aria-label={`New in ${project.name}`}
          onAction={(key) => {
            const chosen = items.find((i) => i.id === key);
            if (chosen) void navigate({ to: chosen.to, params });
          }}
        >
          {items.map((item) => (
            <MenuItem key={item.id} id={item.id} className="menu-item">
              {item.label}
            </MenuItem>
          ))}
        </Menu>
      </Popover>
    </MenuTrigger>
  );
}

/** An Org Admin who isn't a project Admin makes themselves one. The server records it. */
export function useJoinAsAdmin(project: Project) {
  const me = useSuspenseQuery(meQuery).data;
  const { say } = useMessages();
  const join = useApiMutation(
    async () =>
      unwrap(
        await iki.PUT("/api/v1/projects/{project_id}/members/users/{user_id}", {
          params: { path: { project_id: project.id, user_id: me.id } },
          body: { role: "admin" },
        }),
      ),
    [["projects"], ["me"]],
  );
  return {
    join: () => {
      join.mutate(undefined, {
        onSuccess: () => {
          say(`You’re now an Admin of ${project.name}.`);
        },
      });
    },
    busy: join.isPending,
    problem: join.error ? problemOf(join.error) : undefined,
  };
}

export function ProjectOverview() {
  const project = useProject();
  usePageTitle(project.name);
  // Whether its dashboard came after the rest was drawn: then it's at the foot.
  const [dashboardLate, setDashboardLate] = useState(false);

  return (
    <>
      {project.archived ? (
        <div className="alert alert-warning" role="note">
          <p>
            This project is archived and read-only.
            {project.can.manage ? (
              <>
                {" "}
                You can restore it in{" "}
                <Link to="/projects/$slug/settings/" params={{ slug: project.slug }}>
                  Settings
                </Link>
                .
              </>
            ) : null}
          </p>
        </div>
      ) : null}
      {project.can.read ? (
        // Its cards are drawn together where they stay, once each has what decides its
        // lines or the budget's spent: a slow one holds its slot, and none moves under a press
        // as another's answer comes.
        <Settling
          key={project.id}
          label="Opening its overview…"
          className="project-overview"
          budget={OVERVIEW_BUDGET_MS}
        >
          <div className="project-main">
            <Dashboard project={project} onLate={setDashboardLate} />
            <EntityCounts project={project} />
            <RecentChanges project={project} />
          </div>
          <aside className="project-aside" aria-label="Project summary">
            <About project={project} />
            <Members project={project} />
            <Automation project={project} />
            {/* What it takes as a service, its queue, and what it sent (ADR 0132). Last: drawn
                late, or its latest requests late, it moves nothing below it. */}
            <ProjectRequests project={project} />
          </aside>
          {/* What may or may not be there: at the foot of the page (on a phone, under the
              summary), drawn together once all of it, and the summary's last card, is in. */}
          <Settling className="project-tail">
            {/* What plugins' components find in the project (ADR 0117). */}
            <PluginSections kind="project.page" project={project} />
            <ProjectCosts project={project} />
            <ChooseDashboard project={project} />
            <RequestsSettled project={project} />
            {dashboardLate ? <LateDashboard project={project} /> : null}
          </Settling>
        </Settling>
      ) : (
        <NotAMember project={project} />
      )}
    </>
  );
}

/** How long the Overview waits for its cards' reads before it's drawn without the late
 * ones, which hold their slots: most loads have them all by then (see the PR's timings). */
export const OVERVIEW_BUDGET_MS = 300;

/** What the project's page opens with, and whether it's all in. */
function useDashboard(project: Project) {
  const dashboard = useQuery(dashboardQuery(project.id));
  const chosen = dashboard.data?.app;
  const app = useQuery({ ...entityQuery(chosen?.hrid ?? ""), enabled: Boolean(chosen) });
  const types = useQuery(typesQuery);
  return {
    dashboard,
    chosen,
    app,
    types,
    ready: !dashboard.isLoading && (!chosen || (!app.isLoading && !types.isLoading)),
  };
}

/** The app the project's Admins chose for its page (ADR 0090), if the reader can read it:
 * at the top, if it's in by the Overview's budget. Late, it can't hold a place above the
 * rest (it can't tell how tall it'll be, or whether there's one at all), so it's drawn at
 * the foot of the main column instead, when it comes, where it moves nothing. */
function Dashboard({ project, onLate }: { project: Project; onLate: (late: boolean) => void }) {
  const shown = useDashboard(project);
  const late = useSettled(shown.ready);
  useLayoutEffect(() => {
    onLate(late);
  }, [late, onLate]);
  return late ? null : <DashboardView project={project} shown={shown} />;
}

/** The dashboard that came late, after what was drawn without it. */
function LateDashboard({ project }: { project: Project }) {
  return <DashboardView project={project} shown={useDashboard(project)} />;
}

function DashboardView({
  shown: { dashboard, chosen, app, types },
}: {
  project: Project;
  shown: ReturnType<typeof useDashboard>;
}) {
  if (dashboard.isError) return <ProblemAlert problem={problemOf(dashboard.error)} />;
  if (!dashboard.data) return <Loading label="Opening its dashboard…" />;
  // Its Admins hear how to choose one at the foot of the page: below what it holds.
  if (!chosen) return null;
  const problem = app.error ?? types.error;
  if (problem) return <ProblemAlert problem={problemOf(problem)} />;
  if (!app.data || !types.data) return <Loading label={`Opening ${chosen.name}…`} />;
  return (
    <section className="dashboard" aria-labelledby="dashboard-title">
      <div className="dashboard-heading">
        <h2 className="section-title" id="dashboard-title">
          {chosen.name}
        </h2>
        <Link to="/entities/$ref/" params={{ ref: chosen.hrid }} className="muted">
          How it’s built ({chosen.hrid})
        </Link>
      </div>
      <AppPanel app={app.data} types={types.data} />
    </section>
  );
}

/** For its Admins, while the project has no app for its page: how to choose one. */
function ChooseDashboard({ project }: { project: Project }) {
  const dashboard = useQuery(dashboardQuery(project.id));
  useSettled(!dashboard.isLoading);
  if (!dashboard.data || dashboard.data.app || !project.can.manage || project.archived) {
    return null;
  }
  return (
    <p className="project-hint muted">
      Open this page with one of the project’s apps: choose it in{" "}
      <Link to="/projects/$slug/settings/" params={{ slug: project.slug }}>
        Settings
      </Link>
      .
    </p>
  );
}

function NotAMember({ project }: { project: Project }) {
  const members = useQuery(membersQuery(project.id)).data;
  const admins = members?.filter((m) => m.role === "admin").length;
  const { join, busy, problem } = useJoinAsAdmin(project);
  return (
    <section className="card">
      <h2 className="section-title">You’re not a member of this project</h2>
      <p>
        As an Org Admin you can see that it exists and who its members are, but not its data. To
        work in it, add yourself as an Admin. This is recorded.
      </p>
      {admins === 0 ? (
        <p>
          <strong>This project has no Admins.</strong> Add yourself to manage it, then hand it to
          someone on the team.
        </p>
      ) : null}
      <ProblemAlert problem={problem} />
      <Button variant="primary" busy={busy} busyText="Adding…" onClick={join}>
        Add me as Admin
      </Button>
    </section>
  );
}
