"""The manifests that describe the database as of its last successful sync (ADR 0181)."""

import hashlib
import json

from iki_server.entities import registry
from iki_server.plugins.models import PluginBaseline
from iki_spec.model import Plugin, plugin_from_dict, plugin_to_dict


def keep() -> None:
    """Save the baseline in the same transaction as the schema and its guards."""
    manifests = {name: plugin_to_dict(plugin) for name, plugin in registry.plugins().items()}
    revision = (registry.installed() or {}).get("lockfile_sha256")
    if not revision:
        # A build made with 'iki plugin build' has no deployment lockfile.
        encoded = json.dumps(manifests, sort_keys=True, separators=(",", ":")).encode()
        revision = hashlib.sha256(encoded).hexdigest()
    PluginBaseline.objects.update_or_create(
        pk=1, defaults={"revision": revision, "manifests": manifests}
    )


def current() -> tuple[str, dict[str, Plugin]] | None:
    row = PluginBaseline.objects.filter(pk=1).first()
    if row is None:
        return None
    return row.revision, {name: plugin_from_dict(data) for name, data in row.manifests.items()}
