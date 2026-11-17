"""URL converters for plugins' names, which have a ``/`` in them (ADR 0113).

A plugin is ``namespace/name`` (``iki/protein``), and its types, actions,
workflows and components are named under it (``iki/protein.Protein``). A path
takes the name as it is, ``/entity-types/iki/protein.Protein``, or with its
``/`` escaped, ``/entity-types/iki%2Fprotein.Protein``: the server decodes
``%2F`` before it matches a path, so both reach the same view. Iki's own
clients send the ``/`` as it is.

- ``plugin`` matches a plugin's name: exactly one ``/``.
- ``qualified`` matches a name in a plugin: a ``.`` after at most one ``/``.
  An unqualified name (``protein.Protein``) matches too, so the view can say
  it doesn't know it rather than the path not being found. A segment after
  it (``/runs``) has no ``.`` before its ``/``, so it's never taken for part
  of the name.
"""

from django.urls import register_converter


class PluginConverter:
    regex = r"[a-z][a-z0-9_]*/[a-z][a-z0-9_]*"

    def to_python(self, value: str) -> str:
        return value

    def to_url(self, value: str) -> str:
        return value


class QualifiedConverter:
    regex = r"(?:[^/]+/)?[^/.]*\.[^/]+"

    def to_python(self, value: str) -> str:
        return value

    def to_url(self, value: str) -> str:
        return value


def register() -> None:
    """Registers ``plugin`` and ``qualified`` for paths, before any URLconf is read."""
    register_converter(PluginConverter, "plugin")
    register_converter(QualifiedConverter, "qualified")
