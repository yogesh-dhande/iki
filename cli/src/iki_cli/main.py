"""Entry point for the `iki` console script."""

import argparse
from collections.abc import Sequence

from iki_cli import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="iki",
        description="Iki command-line tool.",
    )
    parser.add_argument("--version", action="version", version=f"iki {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0
