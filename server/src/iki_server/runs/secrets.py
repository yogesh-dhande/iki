"""The secrets actions declare, as the deployment keeps them (ADR 0054).

Each is a file in ``IKI_RUN_SECRETS_DIR`` named ``<plugin>.<NAME>``, such as
``dna.NCBI_API_KEY``: one plugin's secrets are never another's, even under the same
name. They're read each time a job is leased, so a rotated secret reaches the next run
without a restart. Only their names are ever kept or shown.
"""

from pathlib import Path
from typing import Any

from django.conf import settings

#: Larger files aren't secrets a command takes as an environment variable.
MAX_BYTES = 64 * 1024


def _file(plugin: str, name: str) -> Path | None:
    if not settings.IKI_RUN_SECRETS_DIR:
        return None
    return Path(settings.IKI_RUN_SECRETS_DIR) / f"{plugin}.{name}"


def key(plugin: str, name: str) -> str:
    """Where an administrator puts it: its key in the run secrets."""
    return f"{plugin}.{name}"


def value(plugin: str, name: str) -> str | None:
    """The secret's value, or None when the deployment hasn't set it."""
    file = _file(plugin, name)
    if file is None:
        return None
    try:
        data = file.read_bytes()[: MAX_BYTES + 1]
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        return None
    if len(data) > MAX_BYTES:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    # Secret managers and editors often end a file with a newline the secret doesn't have.
    text = text.rstrip("\r\n")
    return text or None


def is_set(plugin: str, name: str) -> bool:
    return value(plugin, name) is not None


def names(declared: list[Any]) -> list[str]:
    """The names a run's action declared: runs from before secrets could be optional kept
    plain names."""
    return [d if isinstance(d, str) else d["name"] for d in declared]


def for_run(plugin: str, declared: list[Any]) -> dict[str, str]:
    """The secrets a run's action declares that the deployment has set, by name."""
    found = {name: value(plugin, name) for name in names(declared)}
    return {name: v for name, v in found.items() if v is not None}
