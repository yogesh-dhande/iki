/**
 * What runs cost (ADR 0070): the organization's report, for Compute Admins, Auditors and
 * Org Admins, a month at a time.
 */

import { useQuery, useSuspenseQuery } from "@tanstack/react-query";
import { useNavigate, useSearch } from "@tanstack/react-router";

import { meQuery } from "../api";
import {
  monthName,
  orgCostsQuery,
  recordsUrl,
  shiftMonth,
  thisMonth,
  type OrgCostsBy,
} from "../costs";
import { usePageTitle } from "../layout";
import { buttonClass, Loading, PageHeader, SelectField } from "../ui";
import { validateCostsSearch, BY } from "../costs-search";
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
  if (!me.can.read_costs) return <NotFound />;

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
          <CostTable summary={report.data} groupLabel={label} />
        ) : (
          <p>No machine time was recorded in {monthName(period.since)}.</p>
        )
      ) : (
        <Loading label="Adding up costs…" />
      )}
    </>
  );
}
