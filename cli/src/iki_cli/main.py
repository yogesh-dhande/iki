"""Entry point for the `iki` console script."""

import argparse
from collections.abc import Sequence

from iki_cli import __version__, deploy, plugin


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="iki",
        description="Iki command-line tool.",
    )
    parser.add_argument("--version", action="version", version=f"iki {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    plugin.add_commands(commands)
    deploy.add_commands(commands)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 0
    result: int = handler(args)
    return result
