/**
 * What runs cost (ADR 0070), as a table: the organization's report uses it, and so does a
 * project's overview, which shows this month's.
 */

import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";

import type { Project } from "../api";
import { machineTime, money, monthName, projectCostsQuery, type CostSummary } from "../costs";

/** The groups of a summary, most first, with their totals below. */
export function CostTable({ summary, groupLabel }: { summary: CostSummary; groupLabel: string }) {
  return (
    <div className="table-scroll">
      <table className="table">
        <thead>
          <tr>
            <th scope="col">{groupLabel}</th>
            <th scope="col">Runs</th>
            <th scope="col">Machine time</th>
            <th scope="col">Cost</th>
          </tr>
        </thead>
        <tbody>
          {summary.groups.map((group) => (
            <tr key={`${group.key ?? "idle"}:${group.currency ?? ""}`}>
              <th scope="row">
                {group.key === null ? (
                  <>
                    {group.label}
                    <span className="cell-meta">
                      Machines up with no run to do: the organization’s, not a project’s
                    </span>
                  </>
                ) : summary.by === "action" || summary.by === "template" ? (
                  <code>{group.label}</code>
                ) : (
                  group.label
                )}
              </th>
              <td>{group.runs}</td>
              <td>{machineTime(group.seconds)}</td>
              <td>{group.currency ? money(group.amount, group.currency) : "No price"}</td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          {summary.totals.map((total) => (
            <tr key={total.currency ?? ""}>
              <th scope="row">Total</th>
              <td />
              <td>{machineTime(total.seconds)}</td>
              <td>
                <strong>{total.currency ? money(total.amount, total.currency) : "No price"}</strong>
              </td>
            </tr>
          ))}
        </tfoot>
      </table>
    </div>
  );
}

/** This month's cost of the project's runs, on its overview; nothing if none cost anything. */
export function ProjectCosts({ project }: { project: Project }) {
  const report = useQuery(projectCostsQuery(project.id)).data;
  if (!report?.groups.length) return null;
  const month = monthName(report.since);
  return (
    <section className="card" aria-labelledby="project-costs-title">
      <h2 className="section-title" id="project-costs-title">
        What runs cost in {month}
      </h2>
      <CostTable summary={report} groupLabel="Action" />
      <p className="muted">
        At its compute templates’ prices.{" "}
        <Link to="/projects/$slug/runs/" params={{ slug: project.slug }}>
          Each run
        </Link>{" "}
        shows its own cost.
      </p>
    </section>
  );
}
