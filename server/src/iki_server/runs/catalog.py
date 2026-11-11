"""The actions installed plugins provide, and whether this deployment can run each one.

Actions come from the plugins' manifests (:mod:`iki_server.entities.registry`).
Which can run here comes from the deployment's compute (ADR 0041): the
``compute.json`` that ``iki deploy build`` writes beside the plugins. A
client-tier action runs on the person's own machine, so it needs no
template; every other action needs its environment bound on its tier.
``IKI_COMPUTE_FILE`` points at another file (development and tests).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import cache
from importlib.util import find_spec
from pathlib import Path

from django.conf import settings

from iki_server.entities import registry
from iki_spec.compute import Compute, Template, compute_from_dict
from iki_spec.model import Action, Environment

__all__ = ["Availability", "action", "actions", "availability", "compute", "environment"]

COMPUTE_FILE = "compute.json"


def actions() -> dict[str, Action]:
    """Every installed action, by qualified name, in name order."""
    found = {a.qualified_name: a for p in registry.plugins().values() for a in p.actions.values()}
    return dict(sorted(found.items()))


def action(name: str) -> Action | None:
    return actions().get(name)


def environment(of: Action) -> Environment:
    return registry.plugins()[of.plugin].environments[of.environment]


@cache
def compute() -> Compute:
    """The deployment's compute templates and bindings; none when it has no compute.json."""
    file = _compute_file()
    if file is None or not file.is_file():
        return Compute()
    return compute_from_dict(json.loads(file.read_text("utf-8")))


def _compute_file() -> Path | None:
    configured = os.environ.get("IKI_COMPUTE_FILE")
    if configured:
        return Path(configured)
    spec = find_spec("iki_installed_plugins")
    if spec is None or not spec.submodule_search_locations:
        return None
    return Path(next(iter(spec.submodule_search_locations))).parent / COMPUTE_FILE


@dataclass(frozen=True)
class Availability:
    available: bool
    #: Why not, for people: shown where they'd launch it.
    reason: str | None = None
    #: The templates its jobs may run on, best first.
    templates: tuple[Template, ...] = ()


def availability(of: Action) -> Availability:
    if of.tier == "client":
        return Availability(True)
    if of.network and not settings.IKI_EGRESS_PROXY_URL:
        return Availability(
            False,
            f"It reaches {_hosts(of.network)} on the internet, and this deployment gives runs "
            "no network access. An Iki administrator can turn on the egress proxy.",
        )
    deployment = compute()
    qualified = f"{of.plugin}.{of.environment}"
    binding = deployment.bindings.get(qualified)
    bound = [
        (priority, deployment.templates[name])
        for name, priority in (binding.templates if binding else ())
        if name in deployment.templates and deployment.templates[name].tier == of.tier
    ]
    if not bound:
        return Availability(
            False,
            f"This deployment has no {of.tier}-tier machines for `{qualified}`. A compute "
            "admin can bind it in the deployment's compute.yaml.",
        )
    ordered = sorted(bound, key=lambda pair: (pair[0], _size(pair[1])))
    return Availability(True, templates=tuple(t for _, t in ordered))


def _hosts(network: tuple[str, ...]) -> str:
    return ", ".join(network) if len(network) <= 3 else f"{', '.join(network[:3])} and others"


def _size(template: Template) -> tuple[float, int, int]:
    c = template.capacity
    return (c.gpu_count, c.memory or 0, int((c.cpu or 0) * 1000))
