"""Set up an assay using the installed starters and the admin's API sign-in."""

from __future__ import annotations

import argparse
import json
import sys
from typing import IO, TYPE_CHECKING
from urllib.parse import quote
from uuid import UUID

if TYPE_CHECKING:
    from iki.schema import StarterOut, StarterSetupOut


def _role_assignment(value: str) -> tuple[str, str]:
    role, separator, membership = value.partition("=")
    if not role or not separator:
        raise argparse.ArgumentTypeError("expected ROLE=MEMBERSHIP_UUID")
    try:
        return role, str(UUID(membership))
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected a project membership UUID after '='") from error


def _plan(value: str) -> str:
    if len(value) != 64 or value.strip("0123456789abcdef"):
        raise argparse.ArgumentTypeError("expected the plan 'iki starter preview' printed")
    return value


def add_commands(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    starter = subparsers.add_parser("starter", help="Set up an assay from an installed starter.")
    commands = starter.add_subparsers(dest="starter_command", metavar="COMMAND")

    def show_help(_args: argparse.Namespace) -> int:
        starter.print_help()
        return 0

    starter.set_defaults(handler=show_help)
    for name, description in (
        ("list", "List installed starters and their review flows."),
        ("preview", "Preview what applying a starter would change."),
        ("apply", "Apply the starter version and review flow to a project."),
    ):
        parser = commands.add_parser(name, help=description, description=description)
        parser.add_argument(
            "--url", help="The deployment URL (defaults to IKI_URL or your sign-in)."
        )
        parser.add_argument("--json", action="store_true", help="Print the API response as JSON.")
        parser.add_argument(
            "--project", type=UUID, required=name != "list", help="The assay service project UUID."
        )
        if name != "list":
            parser.add_argument("starter", help="The installed starter ID from 'iki starter list'.")
            parser.add_argument("--version", required=True, help="The starter version to apply.")
            parser.add_argument("--flow", required=True, choices=["research", "regulated_qc"])
            parser.add_argument(
                "--role",
                metavar="ROLE=MEMBERSHIP_UUID",
                type=_role_assignment,
                action="append",
                default=[],
                help="Assign a qualified lab role to a project membership. Repeat for more.",
            )
        if name == "apply":
            parser.add_argument(
                "--plan",
                required=True,
                type=_plan,
                help="The plan 'iki starter preview' printed for these arguments: what you "
                "reviewed. If what applying does has changed since, nothing is applied.",
            )
        parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    from httpx2 import HTTPError

    from iki import ApiError, Client, IkiError

    operation = args.starter_command
    try:
        with Client(args.url) as client:
            if operation == "list":
                params = {"project": str(args.project)} if args.project else {}
                result = client.request("GET", "/starters", params=params)
            else:
                # Starter IDs occupy one URL path segment.
                if not args.starter or args.starter in (".", "..") or "/" in args.starter:
                    raise ValueError("Expected a starter ID from 'iki starter list'.")
                path = f"/starters/{quote(args.starter, safe='')}/{operation}"
                assignments: dict[str, list[str]] = {}
                for role, membership in args.role:
                    assignments.setdefault(role, []).append(membership)
                result = client.request(
                    "POST",
                    path,
                    json={
                        "project": str(args.project),
                        "version": args.version,
                        "flow": args.flow,
                        "role_assignments": assignments,
                        **({"plan": args.plan} if operation == "apply" else {}),
                    },
                )
    except IkiError as error:
        sys.stderr.write(f"iki starter {operation}: {error}\n")
        if isinstance(error, ApiError) and error.code == "starter_plan_changed":
            # What applying does now: review it, then apply again with its plan.
            _print_application(error.problem["plan"], preview=True, out=sys.stderr)
        return 2
    except (HTTPError, ValueError) as error:
        sys.stderr.write(f"iki starter {operation}: {error}\n")
        return 2
    if args.json:
        json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    elif operation == "list":
        _print_starters(result)
    else:
        _print_application(result, preview=operation == "preview")
    return 0


def _print_starters(starters: list[StarterOut]) -> None:
    if not starters:
        sys.stdout.write("No starters installed.\n")
    for starter in starters:
        sys.stdout.write(f"{starter['id']} {starter['version']}  {starter['label']}\n")
        sys.stdout.write(f"  {starter['description']}\n")
        for flow in starter["flows"]:
            sys.stdout.write(f"  {flow['id']}  {flow['label']}  {flow['result_type']}\n")
        for role in starter["roles"]:
            sys.stdout.write(f"  {role['id']}  {role['label']}\n")
        if current := starter["applied"]:
            sys.stdout.write(f"  Applied  {current['version']}  {current['flow']}\n")


def _print_application(
    result: StarterSetupOut, *, preview: bool, out: IO[str] | None = None
) -> None:
    write = (out or sys.stdout).write
    status = "Would apply" if preview else "Applied"
    if not result["changed"]:
        status = "Already applied"
    write(f"{status}  {result['starter']} {result['version']}\n")
    write(f"  Service project   {result['project']}\n")
    write(f"  Review flow       {result['flow']}  {result['result_type']}\n")
    for role, memberships in result["role_assignments"].items():
        write(f"  {role}  {', '.join(memberships)}\n")
    if preview:
        for role, memberships in result["grants_to_add"].items():
            write(f"  Grant {role} to {', '.join(memberships)}\n")
    if preview and result["changed"]:
        write(f"  Plan              {result['plan']}  (apply it with --plan)\n")
