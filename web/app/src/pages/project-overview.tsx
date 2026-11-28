/** A project's Overview: its dashboard, its cards, and what changed in it lately. */

import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useLayoutEffect, useState } from "react";

import { membersQuery, problemOf, type Project } from "../api";
import { dashboardQuery } from "../apps";
import { entityQuery, typesQuery } from "../entities";
import { usePageTitle } from "../layout";
import { useJoinAsAdmin, useProject } from "../project";
import { Settling, useSettled } from "../settling";
import { Button, Loading, ProblemAlert } from "../ui";
import { AppPanel } from "./app";
import { ProjectCosts } from "./cost-table";
import { PluginSections } from "./plugin-sections";
import { About, Automation, EntityCounts, Members, RecentChanges } from "./project-cards";
import { ProjectRequests, RequestsSettled } from "./project-intake";

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
 * ones, which hold their slots. Measured over 20 loads a browser (#225): at 300 ms, half
 * the loads drew a card in its slot; at 400, one or two in twenty did. */
export const OVERVIEW_BUDGET_MS = 400;

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
