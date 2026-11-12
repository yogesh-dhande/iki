/**
 * What runs cost (ADR 0070), as a table: the organization's report uses it, and so does a
 * project's overview, which shows this month's.
 */

import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { Link } from "@tanstack/react-router";

import type { Project } from "../api";
import { machineTime, money, monthName, projectCostsQuery, type CostSummary } from "../costs";
import { BudgetMeter, EnforceBadge } from "./budget-meter";

/** The groups of a summary, most first, with their totals below. */
export function CostTable({
  summary,
  groupLabel,
  extra,
}: {
  summary: CostSummary;
  groupLabel: string;
  /** A last column, such as a Compute Admin's "Set budget". */
  extra?: { label: string; cell: (group: CostSummary["groups"][number]) => ReactNode } | undefined;
}) {
  return (
    <div className="table-scroll">
      <table className="table">
        <thead>
          <tr>
            <th scope="col">{groupLabel}</th>
            <th scope="col">Runs</th>
            <th scope="col">Machine time</th>
            <th scope="col">Cost</th>
            {extra ? (
              <th scope="col">
                <span className="visually-hidden">{extra.label}</span>
              </th>
            ) : null}
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
              {extra ? <td className="row-actions">{extra.cell(group)}</td> : null}
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
              {extra ? <td /> : null}
            </tr>
          ))}
        </tfoot>
      </table>
    </div>
  );
}

/** This month's cost of the project's runs, and its budget, on its overview; nothing if
 * it has no budget and its runs cost nothing. */
export function ProjectCosts({ project }: { project: Project }) {
  const report = useQuery(projectCostsQuery(project.id)).data;
  if (!report || (!report.groups.length && !report.budget)) return null;
  const month = monthName(report.since);
  const { budget } = report;
  return (
    <section className="card" aria-labelledby="project-costs-title">
      <h2 className="section-title" id="project-costs-title">
        What runs cost in {month}
      </h2>
      {budget?.stopped ? (
        <div className="alert alert-warning" role="note">
          <p>
            The project’s budget for {month} is spent, so its runs on the container and VM tiers
            wait until next month, or until a Compute Admin raises it.
          </p>
        </div>
      ) : null}
      {budget ? (
        <div className="budget-summary">
          <span>Budget</span>
          <BudgetMeter status={budget} label="Spent of the project’s budget" />
          <EnforceBadge status={budget} />
        </div>
      ) : null}
      {report.groups.length ? <CostTable summary={report} groupLabel="Action" /> : null}
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
