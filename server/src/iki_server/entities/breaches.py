"""Records that break rules they were saved before (ADR 0127), found at deploy.

A plugin's upgrade can add a rule, tighten one, or migrate values: records saved before
may break it. Edits ratchet (:mod:`iki_server.entities.checking`), so they can still be
changed in other ways, but they mustn't be left to fail silently later. So
``sync_plugins`` checks again every record of each type whose checks, or whose plugins'
versions, changed since it last did (:class:`RulesChecked` keeps a digest):

- a record breaking a rule that holds is held, by the deploy, as a save would have held
  it, and the rule's reviewers are told;
- a record breaking any other rule is reported: a :class:`RuleBreach` on it, shown on
  its page and in the API, that the next save checking the rule clears when it's kept;
- a record that keeps a rule it was reported for is cleared.

Each type with records checked is audited (``plugin.rules_checked``) with its counts,
and Org Admins are told when records break rules. A type without records has nothing to
check: its digest is kept, and nothing's audited. ``unique`` and ``no_overlap`` rules aren't checked
here: the database holds them, and the sync stops when records already break one.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import structlog

from iki_server.audit import log as audit
from iki_server.entities import checking, holds, registry
from iki_server.entities.models import Entity, RuleBreach, RuleHold, RulesChecked
from iki_server.notifications import kinds

__all__ = ["BROKEN", "Checked", "check_brought", "recheck"]

log = structlog.get_logger(__name__)

#: Records checked at a time.
CHUNK = 500

BROKEN = kinds.register(
    kinds.Kind(
        "rules.broken",
        "Records break a plugin's rules",
        "A deploy finds records saved before a plugin's rules, or their change, that "
        "break them (ADR 0127).",
        group="Plugins",
        audience=kinds.Audience.ORG_ADMINS,
    )
)


@dataclass
class Checked:
    """What a deploy found of one type's records."""

    type: str
    records: int = 0
    #: Records it reported, and held, by rule.
    broken: dict[str, int] = field(default_factory=dict)
    held: dict[str, int] = field(default_factory=dict)
    cleared: int = 0


def _reason(type_name: str) -> str:
    plugin = registry.types()[type_name].plugin
    version = registry.plugins()[plugin].version if plugin in registry.plugins() else ""
    return (
        f"Found by the deploy of {plugin} {version}".rstrip()
        + ": it was saved before this rule, or before it changed."
    )


def recheck() -> list[Checked]:
    """Checks each type's records whose checks changed since a deploy last did, and says
    what it found of each type with records. Call it in the sync's transaction."""
    out = []
    for type_name in sorted(registry.types()):
        entity_type = registry.types()[type_name]
        if entity_type.abstract or type_name == "iki/core.Entity":
            continue
        checks = checking.checks_of(type_name)
        digest = checking.digest_of(type_name)
        known = RulesChecked.objects.filter(type=type_name).first()
        if known is not None and known.digest == digest:
            continue
        if not checks:
            # Its rules are gone: what was reported of them goes too. Holds stay, to review.
            RuleBreach.objects.filter(entity__type=type_name).delete()
        elif (done := _check(type_name)).records:
            out.append(done)
        RulesChecked.objects.update_or_create(type=type_name, defaults={"digest": digest})
    _tell(out)
    return out


def _check(type_name: str) -> Checked:
    done = Checked(type_name)
    records = (
        Entity.objects.filter(type=type_name, registry_id__isnull=True, purged_at__isnull=True)
        .select_related("owner_project")
        .order_by("pk")
    )
    found = _Found(done, _reason(type_name), None)
    last = None
    while True:
        chunk = list((records.filter(pk__gt=last) if last is not None else records)[:CHUNK])
        if not chunk:
            break
        last = chunk[-1].pk
        found.check(type_name, chunk)
    if not done.records:
        return done
    found.tell(
        lambda n: f"A deploy held {n:,} {_plural(type_name)} for review",
        "They were saved before a rule that holds records, or before it changed, and break it.",
    )
    audit.record(
        "plugin.rules_checked",
        data={
            "type": type_name,
            "records": done.records,
            "broken": done.broken,
            "held": done.held,
            "cleared": done.cleared,
        },
    )
    if done.broken or done.held:
        log.warning("plugins.rules_broken", type=type_name, broken=done.broken, held=done.held)
    return done


def check_brought(entity_ids: list[Any], *, user: Any, reason: str) -> Checked:  # noqa: ANN401 - UUIDs, a User
    """Checks records a bundle brought from another deployment (ADR 0096) against this
    one's rules: they're kept as they were made there, and reported or held here, by
    ``user``, the import's applier, for ``reason``."""
    done = Checked("")
    found = _Found(done, reason, user)
    records = Entity.objects.filter(
        pk__in=entity_ids, registry_id__isnull=True, purged_at__isnull=True
    ).select_related("owner_project")
    by_type: dict[str, list[Entity]] = defaultdict(list)
    for entity in records:
        by_type[entity.type].append(entity)
    for type_name, entities in sorted(by_type.items()):
        if type_name not in registry.types() or not checking.checks_of(type_name):
            continue
        for start in range(0, len(entities), CHUNK):
            found.check(type_name, entities[start : start + CHUNK])
    found.tell(
        lambda n: f"An import held {n:,} records for review",
        "They came from another deployment, and break rules that hold records here.",
    )
    return done


@dataclass
class _Found:
    """What checking records finds, and whom to tell."""

    done: Checked
    reason: str
    user: Any
    reviewers: dict[Any, set[str]] = field(default_factory=lambda: defaultdict(set))
    held_in: dict[Any, int] = field(default_factory=lambda: defaultdict(int))

    def check(self, type_name: str, chunk: list[Entity]) -> None:
        from iki_server.entities import services

        done = self.done
        keys = {r.key for r in checking.checks_of(type_name)}
        done.records += len(chunk)
        values = services.current_values_of(chunk)
        savings = [checking.Saving(values.get(e.pk, {}).get, id=e.pk, hrid=e.hrid) for e in chunk]
        found = checking.broken(type_name, savings)
        ids = [e.pk for e in chunk]
        reported: dict[Any, set[str]] = defaultdict(set)
        for entity_id, rule in RuleBreach.objects.filter(entity_id__in=ids).values_list(
            "entity_id", "rule"
        ):
            reported[entity_id].add(rule)
        open_holds: dict[Any, set[str]] = defaultdict(set)
        for entity_id, rule in RuleHold.objects.filter(
            entity_id__in=ids, reviewed_at__isnull=True
        ).values_list("entity_id", "rule"):
            open_holds[entity_id].add(rule)
        for entity, broken in zip(chunk, found, strict=True):
            refusing = [b for b in broken if not b.held]
            holding = [b for b in broken if b.held and b.rule.key not in open_holds[entity.pk]]
            for b in refusing:
                RuleBreach.objects.update_or_create(
                    entity=entity,
                    rule=b.rule.key,
                    defaults={"label": b.rule.label[:100], "message": b.message},
                )
                done.broken[b.rule.key] = done.broken.get(b.rule.key, 0) + 1
            if holding and entity.owner_project is not None:
                holds.hold(
                    entity,
                    holding,
                    user=self.user,
                    reason=self.reason,
                    revision=entity.revision,
                    tell=False,
                )
                for b in holding:
                    done.held[b.rule.key] = done.held.get(b.rule.key, 0) + 1
                    self.reviewers[entity.owner_project].update(b.rule.hold)
                self.held_in[entity.owner_project] += 1
            kept = (reported[entity.pk] & keys) - {b.rule.key for b in refusing}
            stale = reported[entity.pk] - keys
            if kept or stale:
                RuleBreach.objects.filter(entity=entity, rule__in=kept | stale).delete()
                done.cleared += len(kept | stale)

    def tell(self, title: Any, body: str) -> None:  # noqa: ANN401 - a callable
        from iki_server.notifications import services as notifications

        for project, roles in self.reviewers.items():
            notifications.notify(
                list(holds.reviewers(project, roles)),
                holds.HELD,
                title=title(self.held_in[project]),
                body=body,
                path=f"/projects/{project.slug}/",
                project=project,
                actor=self.user,
            )


def _plural(type_name: str) -> str:
    t = registry.types()[type_name]
    return (t.label_plural or f"{t.label}s").lower()


def _tell(checked: list[Checked]) -> None:
    from iki_server.notifications import services as notifications

    broken = [c for c in checked if c.broken]
    if not broken:
        return
    total = sum(sum(c.broken.values()) for c in broken)
    names = ", ".join(_plural(c.type) for c in broken[:5])
    notifications.notify(
        notifications.org_admins(),
        BROKEN,
        title=f"{total:,} records break plugins' rules",
        body=f"A deploy checked records saved before their plugins' rules changed: {names}. "
        "Each says which rule on its page.",
        path="/admin/plugins/",
    )
