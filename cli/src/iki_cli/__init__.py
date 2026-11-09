"""The `iki` command-line tool."""

from importlib.metadata import version

__all__ = ["__version__"]

__version__: str = version("iki-cli")
