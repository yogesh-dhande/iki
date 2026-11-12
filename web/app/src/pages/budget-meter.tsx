/** A budget's spending this month, as a gauge, and what it does once it's spent (ADR 0071). */

import { money, type BudgetStatus } from "../costs";
import { Badge } from "../ui";

/** What's been spent of a budget this month, as a gauge and in words. */
export function BudgetMeter({ status, label }: { status: BudgetStatus; label: string }) {
  return (
    <div className="budget-meter">
      <meter
        min={0}
        max={status.amount}
        value={Math.min(status.spent, status.amount)}
        low={status.amount * 0.8}
        high={status.amount * 0.999}
        optimum={0}
        aria-label={label}
      />
      <span>
        {money(status.spent, status.currency)} of {money(status.amount, status.currency)}{" "}
        <span className="muted">({status.percent}%)</span>
      </span>
    </div>
  );
}

/** What a budget does once it's spent, as a badge. */
export function EnforceBadge({ status }: { status: BudgetStatus }) {
  if (status.stopped) return <Badge>Spent: new runs wait</Badge>;
  return status.enforce ? (
    <Badge muted>Stops new runs when spent</Badge>
  ) : (
    <Badge muted>Warns only</Badge>
  );
}
