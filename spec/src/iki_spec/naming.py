"""Plugin names, and the database and Django names Iki generates for a plugin's types.

A plugin's name is ``namespace/name`` (ADR 0113), as a Terraform provider's
is: ``iki/protein``, ``acme/assays``. Its types, actions and other parts are
``namespace/name.Part``: ``iki/protein.Protein``. Names are what manifests,
the API, MCP, the SDK and the web app say.

Where a ``/`` can't go (a Django app label, a Python package, a table, a
Secret's key, a file name) a plugin goes by its *label*, ``namespace__name``:
``iki__protein``, ``acme__assays``. The same rule holds for every namespace,
Iki's included, so two plugins with the same name in different namespaces
never share a table, and a table says whose it is.

Codegen, the validator (which checks the names fit PostgreSQL) and the SQL
views all use these, so a name is decided in one place.

Namespaces, plugin and field names use single underscores only and type names
have none, so the double underscore marks what Iki adds to a name:

=====================================================  ==============================
``iki__protein_protein``                               the table of
                                                       ``iki/protein.Protein``
``iki__protein_protein__history``                      its history: one row per
                                                       revision
``iki__protein_protein__parents``                      a list of references or
                                                       files, one row per item
``acme__lims__iki__protein_protein``                   the fields ``acme/lims``
                                                       adds to ``iki/protein.Protein``
``iki__protein_protein__entry``                        the registry entries' fields
                                                       of a type with an identity
                                                       (ADR 0115), with history and
                                                       lists of their own
=====================================================  ==============================

Django's field and reverse names can't hold a double underscore, so where a
plugin is part of one it is ``namespace_name`` (:func:`identifier`). The build
refuses two plugins that would share it (``acme_bio/assays`` and
``acme/bio_assays``).
"""

from __future__ import annotations

import hashlib
from collections.abc import Collection

__all__ = [
    "CORE",
    "ENTITY",
    "FIRST_PARTY",
    "IKI_NAMESPACE",
    "IKI_REPOSITORY",
    "MAX_IDENTIFIER",
    "MAX_INDEX_NAME",
    "app_label",
    "constraint_name",
    "entry_model",
    "entry_related_name",
    "entry_table",
    "extension_model",
    "extension_related_name",
    "extension_table",
    "history_model",
    "history_table",
    "identifier",
    "index_name",
    "is_reserved",
    "list_model",
    "list_table",
    "model_label",
    "package",
    "parent_link",
    "plugin_of",
    "plugin_of_label",
    "short_name",
    "split",
    "type_table",
]

#: PostgreSQL truncates identifiers longer than this.
MAX_IDENTIFIER = 63

#: The namespace of Iki's own plugins. Only they may use it (ADR 0113).
IKI_NAMESPACE = "iki"

#: The core plugin, which ships with Iki.
CORE = "iki/core"

#: The type every entity is, whatever else it is: the server's own model.
ENTITY = f"{CORE}.Entity"

#: Iki's own plugins: the core plugin, and those under ``plugins/`` in Iki's repository.
#: The only names in the ``iki`` namespace.
FIRST_PARTY = frozenset(
    {
        CORE,
        "iki/dna",
        "iki/protein",
        "iki/antibody",
        "iki/small_molecule",
        "iki/oligo",
        "iki/plasmid",
        "iki/complex",
        "iki/conjugate",
        "iki/lots",
        "iki/example_lims",
        "iki/example_models",
    }
)

#: Where Iki's own plugins come from (ADR 0039): its repository, at ``plugins/<name>``.
IKI_REPOSITORY = "https://github.com/yogesh-dhande/iki.git"


def is_reserved(namespace: str) -> bool:
    """Whether a namespace is Iki's own (``iki``) or could pass for it (``iki_labs``)."""
    return namespace == IKI_NAMESPACE or namespace.startswith(f"{IKI_NAMESPACE}_")


def split(plugin: str) -> tuple[str, str]:
    """``("iki", "protein")`` for ``iki/protein``."""
    namespace, _, name = plugin.partition("/")
    return namespace, name


def short_name(plugin: str) -> str:
    """``protein`` for ``iki/protein``."""
    return plugin.rpartition("/")[2]


def plugin_of(qualified: str) -> str:
    """The plugin of a type, action or other part: ``iki/protein`` for ``iki/protein.Protein``."""
    return qualified.partition(".")[0]


def app_label(plugin: str) -> str:
    """The plugin's label, ``namespace__name``: its name where a ``/`` can't go."""
    namespace, name = split(plugin)
    return f"{namespace}__{name}"


def plugin_of_label(label: str) -> str | None:
    """The plugin whose label is ``label`` (the inverse of :func:`app_label`), or None if it
    isn't a plugin's: Iki's and Django's own apps have no double underscore."""
    namespace, separator, name = label.partition("__")
    return f"{namespace}/{name}" if separator and namespace and name else None


def identifier(plugin: str) -> str:
    """The plugin in a Django field or reverse name, which can't hold ``__``: ``iki_protein``."""
    namespace, name = split(plugin)
    return f"{namespace}_{name}"


def model_label(qualified: str) -> str:
    """The Django model of a type, as ``app_label.Model``: ``iki__protein.Protein``."""
    plugin, name = qualified.split(".")
    return f"{app_label(plugin)}.{name}"


def package(plugin: str) -> str:
    """The Python package ``iki plugin build`` generates for a plugin."""
    return f"iki_plugin_{app_label(plugin)}"


def type_table(qualified: str) -> str:
    plugin, name = qualified.split(".")
    return f"{app_label(plugin)}_{name.lower()}"


def extension_table(plugin: str, target: str) -> str:
    """The table holding the fields ``plugin`` adds to the type ``target``."""
    return f"{app_label(plugin)}__{type_table(target)}"


def history_table(table: str) -> str:
    return f"{table}__history"


def list_table(table: str, field: str) -> str:
    """The table holding a list of references or files, one row per item."""
    return f"{table}__{field}"


def extension_model(target: str) -> str:
    """The model class, in the extending plugin's app, for its fields on ``target``."""
    plugin, name = target.split(".")
    return f"Ext_{identifier(plugin)}_{name}"


def entry_table(table: str) -> str:
    """The table of a type's (or an extension's) registry entry fields (ADR 0115)."""
    return f"{table}__entry"


def entry_model(model: str) -> str:
    """The model of a type's (or an extension's) registry entry fields: ``Protein_Entry``.
    Type names have no underscore, so it can't be another type's name."""
    return f"{model}_Entry"


def entry_parent_link(parent: str) -> str:
    """The field linking a subtype's registry entry row to its parent type's (ADR 0165):
    ``complex_entry_ptr``. Its column is ``entity_id``, as every entity table's is."""
    return f"{entry_model(parent.split('.')[1]).lower()}_ptr"


def entry_related_name(plugin: str) -> str:
    """From an entry's row to the entry fields ``plugin`` adds: ``acme_lims_Entry``."""
    return f"{identifier(plugin)}_Entry"


def history_model(model: str) -> str:
    return f"{model}_History"


def list_model(model: str, field: str) -> str:
    return f"{model}_{field}"


def parent_link(parent: str, inherited: Collection[str] = ()) -> str:
    """The field linking a subtype's row to its parent type's row (Django's own convention).

    ``inherited`` is the links the parent's row already has, from its own ancestors. A
    type named like one of them (``iki/dna.Sequence``, extending ``iki/core.Sequence``) would give
    its subtypes a link of the same name, so theirs is named with its plugin too."""
    plugin, name = parent.split(".")
    link = f"{name.lower()}_ptr"
    return f"{identifier(plugin)}_{link}" if link in inherited else link


def extension_related_name(plugin: str) -> str:
    """From a type's row to the fields ``plugin`` adds: ``structure.acme_lims_Ext``."""
    return f"{identifier(plugin)}_Ext"


#: Django's limit on index names, shorter than Postgres's.
MAX_INDEX_NAME = 30


def index_name(table: str, field: str, kind: str) -> str:
    """An index's name, such as ``name_trgm_3f2a9c01d4e7``: the field's start, the kind
    of index, and a hash of the table, field and kind, so it's unique in the database
    and within Django's 30 characters."""
    digest = hashlib.sha256(f"{table}.{field}.{kind}".encode()).hexdigest()
    stem = f"{field[:10].rstrip('_')}_{kind}_"
    return stem + digest[: MAX_INDEX_NAME - len(stem)]


def constraint_name(table: str, field: str, rule: str) -> str:
    """A CHECK constraint's name, such as ``iki__protein_protein__sequence__format``.

    Names that would be too long keep their start and gain a short hash, so
    they stay unique and recognizable.
    """
    name = f"{table}__{field}__{rule}"
    if len(name) <= MAX_IDENTIFIER:
        return name
    digest = hashlib.sha256(name.encode()).hexdigest()[:10]
    return f"{name[: MAX_IDENTIFIER - 12]}__{digest}"
