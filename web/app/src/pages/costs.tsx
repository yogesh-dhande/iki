/**
 * What runs cost (ADR 0070): the organization's report, for Compute Admins, Auditors and
 * Org Admins, a month at a time.
 */

import { useQuery, useSuspenseQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate, useSearch } from "@tanstack/react-router";

import { meQuery } from "../api";
import {
  budgetsQuery,
  monthName,
  orgCostsQuery,
  recordsUrl,
  shiftMonth,
  thisMonth,
  type CostSummary,
  type OrgCostsBy,
} from "../costs";
import { usePageTitle } from "../layout";
import { Button, buttonClass, Loading, PageHeader, SelectField } from "../ui";
import { validateCostsSearch, BY } from "../costs-search";
import { BudgetDialog, Budgets, type BudgetScope } from "./budgets";
import { CostTable } from "./cost-table";
import { NotFound } from "./errors";

export function OrgCosts() {
  usePageTitle("Costs");
  const me = useSuspenseQuery(meQuery).data;
  const search = useSearch({ from: "/costs" });
  const navigate = useNavigate({ from: "/costs" });
  const current = thisMonth();
  const period = search.month ? shiftMonth(`${search.month}-01`, 0) : current;
  const by = search.by ?? "project";
  const report = useQuery({ ...orgCostsQuery(by, period), enabled: me.can.read_costs });
  const budgets = useQuery({ ...budgetsQuery, enabled: me.can.read_costs }).data;
  const [setting, setSetting] = useState<BudgetScope | undefined>();
  if (!me.can.read_costs) return <NotFound />;
  const canManage = me.can.manage_compute;
  const budgeted = new Set(
    (budgets ?? []).map((b) => b.project?.id ?? b.team?.id).filter((id) => id !== undefined),
  );
  const settable = canManage && (by === "project" || by === "team") && budgets !== undefined;
  const extra: Parameters<typeof CostTable>[0]["extra"] = settable
    ? {
        label: "Budget",
        cell: (group: CostSummary["groups"][number]) =>
          group.key === null || budgeted.has(group.key) ? null : (
            <Button
              variant="quiet"
              label={`Set a budget for ${group.label}`}
              onClick={() => {
                if (group.key !== null)
                  setSetting({
                    kind: by === "team" ? "team" : "project",
                    id: group.key,
                    name: group.label,
                  });
              }}
            >
              Set budget
            </Button>
          ),
      }
    : undefined;

  function go(month: string, groupBy: OrgCostsBy) {
    const wanted = month === current.since.slice(0, 7) ? undefined : month;
    void navigate({ search: validateCostsSearch({ month: wanted, by: groupBy }) });
  }
  const previous = shiftMonth(period.since, -1).since.slice(0, 7);
  const next = shiftMonth(period.since, 1).since.slice(0, 7);
  const latest = period.since === current.since;
  const label = BY.find((option) => option.value === by)?.label ?? "Project";

  return (
    <>
      <PageHeader
        title="Costs"
        actions={
          <div className="actions">
            <a className={buttonClass()} href={recordsUrl(period)} download>
              Download records (CSV)
            </a>
          </div>
        }
      />
      <p className="muted">
        What the machines Iki starts for runs cost, at their compute templates’ prices. A machine’s
        cost counts when it’s recorded: when it ends, or when a warm one finishes a run. Each run
        shows its own cost too.
      </p>
      <div className="toolbar">
        <SelectField
          label="Group by"
          value={by}
          options={BY}
          onChange={(value) => {
            go(period.since.slice(0, 7), value);
          }}
        />
        <div className="toolbar-end month-stepper" role="group" aria-label="Month">
          <button
            type="button"
            className={buttonClass("quiet")}
            onClick={() => {
              go(previous, by);
            }}
          >
            ← {monthName(`${previous}-01`)}
          </button>
          <strong aria-live="polite">{monthName(period.since)}</strong>
          <button
            type="button"
            className={buttonClass("quiet")}
            disabled={latest}
            onClick={() => {
              go(next, by);
            }}
          >
            {monthName(`${next}-01`)} →
          </button>
        </div>
      </div>
      {report.data ? (
        report.data.groups.length ? (
          <CostTable summary={report.data} groupLabel={label} extra={extra} />
        ) : (
          <p>No machine time was recorded in {monthName(period.since)}.</p>
        )
      ) : (
        <Loading label="Adding up costs…" />
      )}
      {budgets ? <Budgets budgets={budgets} canManage={canManage} onSet={setSetting} /> : null}
      {setting ? (
        <BudgetDialog
          scope={setting}
          onClose={() => {
            setSetting(undefined);
          }}
        />
      ) : null}
    </>
  );
}
