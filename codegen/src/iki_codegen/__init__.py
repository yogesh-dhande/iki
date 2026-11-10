"""Compiles Iki plugin manifests into Django apps (ADR 0006, ADR 0033).

``iki plugin build`` and ``iki plugin makemigrations`` call :func:`build`.
"""

from iki_codegen.build import build, in_dependency_order
from iki_codegen.models import render_models

__all__ = ["REGISTRY", "build", "in_dependency_order", "render_models"]

#: The package listing the generated apps, which the server's settings install.
REGISTRY = "iki_installed_plugins"
