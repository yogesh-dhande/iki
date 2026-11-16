"""``iki-server check_restore``: whether a restored deployment is whole (ADR 0099).

Run it after restoring the database and the files, before people use it. It exits 1 if
anything is wrong, naming what. ``--json`` prints the report on one line, for a script to compare
with the deployment the backup came from.
"""

import json
from argparse import ArgumentParser
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from iki_server.core import restore


class Command(BaseCommand):
    help = "Check a restored deployment: its schema, encrypted values and stored files."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--sample",
            type=int,
            metavar="N",
            help="Check N files of each kind, chosen at random, rather than every one.",
        )
        parser.add_argument(
            "--hashes",
            action="store_true",
            help="Read each file checked and compare its SHA-256 with the one recorded.",
        )
        parser.add_argument(
            "--external",
            action="store_true",
            help="Also check files registered from other buckets.",
        )
        parser.add_argument(
            "--json", action="store_true", help="Print the report as JSON, on one line."
        )

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401 - Django's signature
        sample = options["sample"]
        if sample is not None and sample < 1:
            raise CommandError("--sample takes a number of files, 1 or more.")
        report = restore.check(
            sample=sample, hashes=options["hashes"], external=options["external"]
        )
        if options["json"]:
            # One line: logs go to stdout too, and a script picks this out by its keys.
            self.stdout.write(json.dumps(report.as_json()))
        else:
            self._say(report)
        if not report.ok:
            raise CommandError("The restore isn't whole: see above.")

    def _say(self, report: restore.Report) -> None:
        until = report.data_until.isoformat() if report.data_until else "-"
        self.stdout.write(f"Data until {until} ({restore.behind(report.data_until)}).")
        self.stdout.write(
            "Holds "
            + ", ".join(f"{n} {name.replace('_', ' ')}" for name, n in report.counts.items())
            + "."
        )
        for check in report.checks:
            mark = "ok" if check.ok else "FAILED"
            self.stdout.write(f"{mark:>6}  {check.name}: {check.checked} checked")
            for problem in check.problems:
                self.stdout.write(f"        - {problem}")
            if check.problem_count > len(check.problems):
                more = check.problem_count - len(check.problems)
                self.stdout.write(f"        and {more} more")
            if check.note:
                self.stdout.write(f"        {check.note}")
