"""What a live test's run cost: the framework each cloud's report fills in.

A run's cost is each billable resource's lifetime, from when the cloud recorded making it
to when it recorded deleting it (Google Cloud's audit logs, AWS's CloudTrail), times its
list price (the Cloud Billing Catalog, the AWS Price List). gcp_cost.py and aws_cost.py
find the lifetimes and prices; this is what they share: a resource's life, a line of the
report, and the report itself, as text and JSON in the run's work directory.
"""

import datetime
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

HOURS_PER_MONTH = 730
GIB = 1024**3


class Window(Protocol):
    """The run's span: when its first resource could have been made, and its last gone."""

    start: datetime.datetime
    end: datetime.datetime


def parse_time(value: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass
class Life:
    """A resource's first create and last delete in the cloud's record of what was done;
    with neither, the window's start or end."""

    kind: str
    name: str
    start: datetime.datetime | None = None
    end: datetime.datetime | None = None
    request: dict[str, Any] = field(default_factory=dict)

    def hours(self, window: Window) -> float:
        start = self.start or window.start
        end = self.end or window.end
        return max(0.0, (end - start).total_seconds() / 3600)

    def saw(
        self, when: datetime.datetime, *, made: bool, request: dict[str, Any] | None = None
    ) -> None:
        """An entry for it: the earliest making, the latest deleting."""
        if made:
            if self.start is None or when < self.start:
                self.start = when
                self.request = request or {}
        elif self.end is None or when > self.end:
            self.end = when


@dataclass
class Line:
    resource: str
    lifetime_hours: float
    quantity: float
    unit: str
    sku: str
    description: str
    unit_price: float
    cost: float
    note: str = ""


@dataclass
class Report:
    window: dict[str, str]
    lines: list[Line] = field(default_factory=list)

    @property
    def total(self) -> float:
        return sum(line.cost for line in self.lines)

    def heading(self) -> str:
        return (
            f"Cost of {self.window['name']}, {self.window['start']} to {self.window['end']} "
            "(list prices, USD)"
        )

    def footer(self) -> list[str]:
        """Lines after the total: what else the cloud says, such as its billing records."""
        return []

    def text(self) -> str:
        out = [
            self.heading(),
            "",
            f"{'resource':<44} {'hours':>6} {'quantity':>12} {'unit price':>12} {'cost':>8}  SKU",
        ]
        for line in self.lines:
            quantity = f"{line.quantity:.3f} {line.unit}"
            out.append(
                f"{line.resource[:44]:<44} {line.lifetime_hours:>6.2f} {quantity:>12} "
                f"{line.unit_price:>12.6f} {line.cost:>8.4f}  {line.sku} {line.description}"
            )
            if line.note:
                out.append(f"{'':<44}   {line.note}")
        out += ["", f"{'total':<44} {'':>6} {'':>12} {'':>12} {self.total:>8.4f}", ""]
        out += self.footer()
        return "\n".join(out) + "\n"


def write(result: Report, workdir: Path, cloud: str) -> str:
    """<cloud>-cost.txt and <cloud>-cost.json in the work directory; returns the text."""
    text = result.text()
    (workdir / f"{cloud}-cost.txt").write_text(text)
    (workdir / f"{cloud}-cost.json").write_text(
        json.dumps({**asdict(result), "total": result.total}, indent=1, default=str)
    )
    return text


def prorate(usd: float, unit: str, quantity_hours: float) -> float:
    """The cost of quantity_hours of a unit-hour price, or of a unit-month one prorated."""
    # Google Cloud's GiBy.mo, AWS's GB-Mo.
    if unit.lower().endswith(("mo", "month")):
        return usd * quantity_hours / HOURS_PER_MONTH
    return usd * quantity_hours
