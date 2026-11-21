"""An audit event's data, as the audit page shows it: what changed, then the rest.

Events record their details as JSON (``AuditEvent.data``). Read raw, a change is two
dictionaries to compare by eye, and Postgres's ``jsonb`` doesn't even keep their order
("after" sorts before "before"). So the page reads the shapes events record changes
in, and shows each changed value as a row: what it is, under the label people set it
with, before and after.

- ``before`` and ``after``: two objects, compared key by key (a run quota, a budget), or
  two values of the one thing the action changes (a project's Slurm account).
- ``from`` and ``to``: the same, as some events name them.
- ``x`` and ``previous_x``: one value and what it was (a project member's role).
- ``changes``: a record's fields, each ``{"before", "after"}``; or a list of changes,
  each with ``from`` and ``to`` and what it's about.
- A pair ``[before, after]`` under a key that records one (``registry``, ``app``).

What's left are facts, sorted by label. The data exactly as recorded stays a click
away on the page.
"""

import json
from dataclasses import dataclass
from typing import Any

#: Labels for keys, as the forms that set them say. Others read as their key does.
LABELS = {
    "max_running": "Jobs running at once",
    "max_queued": "Runs waiting",
    "tier": "Tier",
    "amount": "A month’s budget",
    "warn_at": "Warn at (%)",
    "enforce": "Stop new container and VM runs once it’s spent",
    "tokens_revoked": "Tokens revoked",
    "sessions": "Sessions ended",
    "sha256": "SHA-256",
    "hrid": "ID",
    "client_id": "Client ID",
    "external_id": "External ID",
    "media_type": "Media type",
    "self": "Their own",
    "previous_team": "Team before",
}

#: What a lone ``before``/``after`` or ``from``/``to`` pair is, by action.
SUBJECTS = {
    "slurm_account.changed": "Slurm account",
    "notifications.watch_changed": "Watching",
    "user.placeholder_claimed": "From",
}

#: Keys whose value is a ``[before, after]`` pair.
PAIRS = frozenset({"registry", "app"})

#: Values with a name of their own, by key.
VALUES: dict[str, dict[Any, str]] = {
    "tier": {
        None: "Every tier together",
        "self_hosted": "Self-hosted",
        "backend": "Backend",
        "container": "Container",
        "vm": "VM",
    },
    "max_running": {None: "No limit"},
    "max_queued": {None: "No limit"},
}


@dataclass(frozen=True)
class Change:
    label: str
    before: str
    after: str


@dataclass(frozen=True)
class Fact:
    label: str
    value: str


@dataclass(frozen=True)
class Details:
    changes: list[Change]
    facts: list[Fact]
    #: The data as recorded, as JSON, for whoever needs it exactly.
    recorded: str


def label(key: str) -> str:
    if key in LABELS:
        return LABELS[key]
    words = key.replace("_", " ").strip()
    return words[:1].upper() + words[1:] if words else key


def shown(value: Any, key: str = "") -> str:  # noqa: ANN401
    named = VALUES.get(key, {})
    if isinstance(value, str | int | None) and not isinstance(value, bool) and value in named:
        return named[value]
    if value is None or value == "" or value == []:
        return "None"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, list):
        return ", ".join(shown(v) for v in value)
    if isinstance(value, dict):
        return "; ".join(f"{label(k)}: {shown(v, k)}" for k, v in sorted(value.items()))
    return str(value)


def _is_change(value: Any) -> bool:  # noqa: ANN401
    return isinstance(value, dict) and set(value) == {"before", "after"}


def describe(action: str, data: dict[str, Any]) -> Details:
    rest = dict(data)
    changes: list[Change] = []
    facts: list[Fact] = []
    subject = SUBJECTS.get(action, "Value")

    def compare(before: Any, after: Any) -> None:  # noqa: ANN401
        if isinstance(before, dict | None) and isinstance(after, dict | None):
            old, new = before or {}, after or {}
            for key in sorted({*old, *new}, key=label):
                if key in old and key in new and old[key] == new[key]:
                    facts.append(Fact(label(key), shown(new[key], key)))
                else:
                    changes.append(
                        Change(label(key), shown(old.get(key), key), shown(new.get(key), key))
                    )
        else:
            changes.append(Change(subject, shown(before), shown(after)))

    for old, new in (("before", "after"), ("from", "to")):
        if old in rest or new in rest:
            compare(rest.pop(old, None), rest.pop(new, None))
    for key in [k for k in rest if k.startswith("previous_") and k[len("previous_") :] in rest]:
        name = key[len("previous_") :]
        changes.append(
            Change(label(name), shown(rest.pop(key), name), shown(rest.pop(name), name))
        )
    for key in [k for k in rest if k in PAIRS]:
        value = rest[key]
        if isinstance(value, list) and len(value) == 2:
            del rest[key]
            changes.append(Change(label(key), shown(value[0], key), shown(value[1], key)))
    if isinstance(rest.get("changes"), dict) and all(map(_is_change, rest["changes"].values())):
        for name, change in sorted(rest.pop("changes").items()):
            changes.append(Change(label(name), shown(change["before"]), shown(change["after"])))
    elif isinstance(rest.get("changes"), list):
        for item in rest.pop("changes"):
            if not (isinstance(item, dict) and {"from", "to"} <= set(item)):
                facts.append(Fact("Change", shown(item)))
                continue
            about = {k: v for k, v in item.items() if k not in {"from", "to"}}
            name = ", ".join(shown(v) for _, v in sorted(about.items())) or subject
            changes.append(Change(name, shown(item["from"]), shown(item["to"])))
    for key in [k for k in rest if _is_change(rest[k])]:
        change = rest.pop(key)
        changes.append(Change(label(key), shown(change["before"]), shown(change["after"])))
    facts += [Fact(label(k), shown(v, k)) for k, v in rest.items()]
    facts.sort(key=lambda fact: fact.label)
    return Details(
        changes=changes,
        facts=facts,
        recorded=json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False),
    )
