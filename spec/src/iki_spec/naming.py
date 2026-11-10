"""The database and Django names Iki generates for a plugin's types.

Codegen, the validator (which checks the names fit PostgreSQL) and the SQL
views all use these, so a name is decided in one place.

Plugin and field names use single underscores only and type names have none,
so the double underscore marks what Iki adds to a name:

==========================  ===============================================
``protein_protein``         the table of ``protein.Protein``
``protein_protein__history`` its history: one row per revision
``protein_protein__parents`` a list of references or files, one row per item
``structure_prediction__protein_proteinstructure``
                            the fields ``structure_prediction`` adds to
                            ``protein.ProteinStructure``
==========================  ===============================================
"""

from __future__ import annotations

import hashlib

__all__ = [
    "MAX_IDENTIFIER",
    "constraint_name",
    "extension_model",
    "extension_related_name",
    "extension_table",
    "history_model",
    "history_table",
    "list_model",
    "list_table",
    "package",
    "parent_link",
    "type_table",
]

#: PostgreSQL truncates identifiers longer than this.
MAX_IDENTIFIER = 63


def package(plugin: str) -> str:
    """The Python package ``iki plugin build`` generates for a plugin."""
    return f"iki_plugin_{plugin}"


def type_table(qualified: str) -> str:
    plugin, name = qualified.split(".")
    return f"{plugin}_{name.lower()}"


def extension_table(plugin: str, target: str) -> str:
    """The table holding the fields ``plugin`` adds to the type ``target``."""
    return f"{plugin}__{type_table(target)}"


def history_table(table: str) -> str:
    return f"{table}__history"


def list_table(table: str, field: str) -> str:
    """The table holding a list of references or files, one row per item."""
    return f"{table}__{field}"


def extension_model(target: str) -> str:
    """The model class, in the extending plugin's app, for its fields on ``target``."""
    plugin, name = target.split(".")
    return f"Ext_{plugin}_{name}"


def history_model(model: str) -> str:
    return f"{model}_History"


def list_model(model: str, field: str) -> str:
    return f"{model}_{field}"


def parent_link(parent: str) -> str:
    """The field linking a subtype's row to its parent type's row (Django's own convention)."""
    return f"{parent.split('.')[1].lower()}_ptr"


def extension_related_name(plugin: str) -> str:
    """From a type's row to the fields ``plugin`` adds: ``structure.structure_prediction_Ext``."""
    return f"{plugin}_Ext"


def constraint_name(table: str, field: str, rule: str) -> str:
    """A CHECK constraint's name, such as ``protein_protein__sequence__format``.

    Names that would be too long keep their start and gain a short hash, so
    they stay unique and recognizable.
    """
    name = f"{table}__{field}__{rule}"
    if len(name) <= MAX_IDENTIFIER:
        return name
    digest = hashlib.sha256(name.encode()).hexdigest()[:10]
    return f"{name[: MAX_IDENTIFIER - 12]}__{digest}"
