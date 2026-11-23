"""Check a built plugin's manifest against the deployment using the admin's API sign-in."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

from iki_spec import API_VERSION, schemas

if TYPE_CHECKING:
    from iki.schema import PluginPreflightOut


def add_command(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = commands.add_parser(
        "preflight",
        help="Preview the effect of a proposed plugin on existing records.",
        description=(
            "Send a built plugin's manifest.json to the deployment for a read-only check. "
            "Requires an Org Admin sign-in with the admin scope and access to all projects. "
            "Exits 0 for a clear report, 1 for blocking findings and 2 if the check failed."
        ),
    )
    parser.add_argument("manifest", type=Path, help="The proposed plugin package's manifest.json.")
    parser.add_argument("--url", help="The deployment URL (defaults to IKI_URL or your sign-in).")
    parser.add_argument("--json", action="store_true", help="Print the complete report as JSON.")
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    from httpx2 import HTTPError

    from iki import Client, IkiError

    try:
        document = json.loads(args.manifest.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or document.get("apiVersion") != API_VERSION:
            raise ValueError(f"Expected a built {API_VERSION} plugin manifest.")
        plugin = document.get("plugin")
        name = plugin.get("name") if isinstance(plugin, dict) else None
        if not isinstance(name, str) or schemas.check("field", name, part="pluginName"):
            raise ValueError("The manifest needs a qualified plugin name.")
        with Client(args.url) as client:
            report = client.request("POST", f"/plugins/{name}/preflight", json=document)
    except (IkiError, HTTPError, OSError, ValueError) as error:
        sys.stderr.write(f"iki plugin preflight: {args.manifest}: {error}\n")
        return 2
    if args.json:
        json.dump(report, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        _print_report(report, sys.stdout)
    return int(report["blocking"])


def _print_report(report: PluginPreflightOut, out: TextIO) -> None:
    out.write(f"{report['plugin']} {report['version']}\n")
    out.write(f"Checked configuration  {report['configuration_revision']}\n")
    out.write(f"Candidate manifest     {report['candidate_revision']}\n")
    for row in report["types"]:
        population = f"{row['total']:,} {'record' if row['total'] == 1 else 'records'}"
        out.write(f"\n{row['type']} ({population})\n")
        if row["sampled"]:
            checks = ", ".join(row["sampled_checks"])
            out.write(f"  Sampled {row['checked']:,} of {population} for {checks}.\n")
        for finding in row["findings"]:
            severity = "Blocking" if finding["blocking"] else "Info"
            precision = "At least" if finding["sampled"] else "Exactly"
            out.write(
                f"  {severity}  {finding['subject']} ({finding['kind']})  {finding['message']}\n"
                f"    {precision} {finding['count']:,} affected."
            )
            if finding["examples"]:
                out.write(" Examples: " + ", ".join(finding["examples"]))
            out.write("\n")
    if any(row["sampled"] for row in report["types"]):
        out.write("\nSample coverage is incomplete. Deployment requires review.\n")
    elif report["blocking"]:
        out.write("\nBlocking findings.\n")
    else:
        out.write("\nNo blocking findings.\n")
