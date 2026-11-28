/** A project's heading and tabs, above each of its pages. */

import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useRouterState } from "@tanstack/react-router";
import { Button as AriaButton, Menu, MenuItem, MenuTrigger, Popover } from "react-aria-components";

import { membersQuery, rolesQuery, type Project } from "../api";
import { typesQuery } from "../entities";
import { countsQuery } from "../lists";
import { TopBar } from "../page-top";
import { useProject } from "../project";
import { swatchColor } from "../project-color";
import { useRequestSides } from "../requests";
import { TabStrip } from "../tab-strip";
import { Badge, PageTitle } from "../ui";
import { ProjectNotifications } from "./project-notifications";

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
