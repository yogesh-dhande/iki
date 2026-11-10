"""Iki's versioned plugin specs: JSON Schemas, the manifest loader and validator, and UCUM units."""

from importlib.metadata import version

__all__ = ["API_VERSION", "__version__"]

__version__: str = version("iki-spec")

#: The spec version this package reads and checks.
API_VERSION = "iki/v1"
