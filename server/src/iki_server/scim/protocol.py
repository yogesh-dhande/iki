"""SCIM 2.0 documents (RFC 7643, RFC 7644): filters, PATCH operations and resources.

Iki implements what Microsoft Entra ID and Okta use:

- **Filters:** ``eq`` on the attributes they look people and groups up by,
  joined with ``and``.
- **PATCH:** both styles of operation. With a ``path`` (Okta,
  ``members[value eq "…"]``), and without one, with a value object whose
  keys may be dotted paths (Entra, ``{"name.givenName": "Ada"}``).
- **Leniency:** operation names are case-insensitive, and booleans may arrive
  as strings (Entra sends ``"False"``). Attributes Iki doesn't store are
  ignored rather than refused, so an identity provider's default attribute
  mappings work unchanged.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from django.urls import reverse

from iki_server.accounts.models import User
from iki_server.scim.models import ScimGroup
from iki_server.scim.services import GroupPatch, Profile, invalid

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
ENTERPRISE_USER_SCHEMA = "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"

MAX_PAGE = 200


# --- Filters -----------------------------------------------------------------------


@dataclass(frozen=True)
class Clause:
    attribute: str
    """Lower-cased attribute path, such as ``username`` or ``emails.value``."""
    value: str


_CLAUSE = re.compile(
    r'\s*([A-Za-z0-9_.:]+(?:\[[^\]]*\])?(?:\.\w+)?)\s+eq\s+"((?:[^"\\]|\\.)*)"\s*',
    re.IGNORECASE,
)
_AND = re.compile(r"and\b", re.IGNORECASE)
#: ``emails[type eq "work"].value`` and ``members[value eq "x"]`` mean the same as the dotted path.
_VALUE_FILTER = re.compile(
    r'^(\w+)\[(?:type eq "\w+"|value eq "([^"]*)")\](?:\.(\w+))?$', re.IGNORECASE
)


def _normalize_attribute(attribute: str) -> str:
    attribute = attribute.strip()
    for schema in (USER_SCHEMA, GROUP_SCHEMA):
        if attribute.lower().startswith(schema.lower() + ":"):
            attribute = attribute[len(schema) + 1 :]
    match = _VALUE_FILTER.match(attribute)
    if match:
        attribute = f"{match.group(1)}.{match.group(3) or 'value'}"
    return attribute.lower()


def parse_filter(text: str, allowed: set[str]) -> list[Clause]:
    """Parses ``a eq "x" and b eq "y"``. Anything else is a SCIM invalidFilter error."""
    clauses, position = [], 0
    while True:
        match = _CLAUSE.match(text, position)
        if not match:
            raise _invalid_filter(text)
        attribute = _normalize_attribute(match.group(1))
        if attribute not in allowed:
            raise _invalid_filter(text)
        clauses.append(Clause(attribute, re.sub(r"\\(.)", r"\1", match.group(2))))
        position = match.end()
        if position == len(text):
            return clauses
        joiner = _AND.match(text, position)
        if not joiner:
            raise _invalid_filter(text)
        position = joiner.end()


def _invalid_filter(text: str) -> Exception:
    error = invalid(f"Unsupported filter: {text[:200]}")
    error.scim_type = "invalidFilter"
    return error


# --- Values ------------------------------------------------------------------------


def boolean(value: Any) -> bool:  # noqa: ANN401 - JSON
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    raise invalid(f"Expected true or false, got {value!r}.")


def string(value: Any, name: str) -> str:  # noqa: ANN401 - JSON
    if value is None:
        return ""
    if not isinstance(value, str):
        raise invalid(f"{name} must be a string.")
    return value.strip()


def uuid(value: Any) -> UUID | None:  # noqa: ANN401 - JSON
    try:
        return UUID(str(value))
    except ValueError:
        return None


def _primary_email(emails: Any) -> str | None:  # noqa: ANN401 - JSON
    if not isinstance(emails, list) or not emails:
        return None
    entries = [e for e in emails if isinstance(e, dict) and e.get("value")]
    if not entries:
        return None
    chosen = next(
        (e for e in entries if e.get("primary") in (True, "true", "True")),
        next((e for e in entries if e.get("type") == "work"), entries[0]),
    )
    return string(chosen["value"], "emails.value")


_NAME_PARTS = ("formatted", "givenName", "familyName", "middleName")


def _name(value: Any) -> dict[str, str]:  # noqa: ANN401 - JSON
    if not isinstance(value, dict):
        return {}
    return {part: string(value[part], f"name.{part}") for part in _NAME_PARTS if part in value}


def profile(document: dict[str, Any]) -> Profile:
    """A User resource (POST or PUT) as a :class:`Profile`."""
    return Profile(
        user_name=string(document.get("userName"), "userName"),
        external_id=string(document.get("externalId"), "externalId"),
        email=_primary_email(document.get("emails")),
        name=_name(document.get("name")),
        display_name=string(document.get("displayName"), "displayName"),
        active=boolean(document["active"]) if "active" in document else None,
    )


def _operations(document: dict[str, Any]) -> list[tuple[str, str, Any]]:
    operations = document.get("Operations")
    if not isinstance(operations, list):
        raise invalid("A PATCH needs a list of Operations.")
    result = []
    for operation in operations:
        if not isinstance(operation, dict):
            raise invalid("Each operation must be an object.")
        op = str(operation.get("op", "")).lower()
        if op not in ("add", "replace", "remove"):
            raise invalid(f"Unknown operation {operation.get('op')!r}.")
        result.append((op, str(operation.get("path") or ""), operation.get("value")))
    return result


def _user_attribute(profile: Profile, path: str, value: Any, op: str) -> None:  # noqa: ANN401
    attribute = _normalize_attribute(path)
    removing = op == "remove"
    if attribute == "active":
        if not removing:
            profile.active = boolean(value)
    elif attribute == "username":
        profile.user_name = string(value, "userName")
    elif attribute == "externalid":
        profile.external_id = "" if removing else string(value, "externalId")
    elif attribute == "displayname":
        profile.display_name = "" if removing else string(value, "displayName")
    elif attribute == "emails" and not removing:
        profile.email = _primary_email(value)
    elif attribute == "emails.value" and not removing:
        profile.email = string(value, "emails.value")
    elif attribute == "name" and not removing:
        profile.name |= _name(value)
    elif attribute.startswith("name."):
        part = next((p for p in _NAME_PARTS if p.lower() == attribute[5:]), None)
        if part:
            profile.name[part] = "" if removing else string(value, path)
    # Anything else (title, phone numbers, the enterprise extension, ...) isn't stored.


def user_patch(document: dict[str, Any]) -> Profile:
    profile = Profile()
    for op, path, value in _operations(document):
        if path:
            _user_attribute(profile, path, value, op)
        elif isinstance(value, dict) and op != "remove":
            for key, item in value.items():
                _user_attribute(profile, key, item, op)
        else:
            raise invalid("An operation without a path needs an object value.")
    return profile


def member_ids(value: Any) -> list[UUID]:  # noqa: ANN401 - JSON
    if value is None:
        return []
    if not isinstance(value, list):
        value = [value]
    ids = []
    for member in value:
        member_id = uuid(member.get("value") if isinstance(member, dict) else member)
        if member_id is not None:
            ids.append(member_id)
    return ids


def group_patch(document: dict[str, Any]) -> GroupPatch:
    patch = GroupPatch()
    for op, path, value in _operations(document):
        targets = [(path, value)] if path else []
        if not path:
            if not isinstance(value, dict):
                raise invalid("An operation without a path needs an object value.")
            targets = list(value.items())
        for target, item in targets:
            raw = target.strip()
            attribute = _normalize_attribute(raw)
            match = _VALUE_FILTER.match(raw)
            if attribute == "displayname" and op != "remove":
                patch.display_name = string(item, "displayName")
            elif attribute == "externalid":
                patch.external_id = "" if op == "remove" else string(item, "externalId")
            elif attribute in ("members", "members.value"):
                ids = member_ids(item)
                if match and match.group(2):
                    single = uuid(match.group(2))
                    ids = [single] if single else []
                if op == "add":
                    patch.add += ids
                elif op == "remove":
                    if ids:
                        patch.remove += ids
                    else:
                        patch.remove_all = True
                else:
                    patch.replace = ids
    return patch


# --- Resources ---------------------------------------------------------------------


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def user_resource(
    user: User, base: str, *, groups: list[ScimGroup] | None = None
) -> dict[str, Any]:
    location = base + reverse("scim:user", kwargs={"resource_id": user.pk})
    name = {k: v for k, v in user.scim_name.items() if v and k != "displayName"}
    name.setdefault("formatted", user.name)
    resource: dict[str, Any] = {
        "schemas": [USER_SCHEMA],
        "id": str(user.pk),
        "userName": user.scim_user_name,
        "displayName": user.name,
        "name": name,
        "emails": [{"value": user.email, "type": "work", "primary": True}],
        "active": user.is_active,
        "meta": {
            "resourceType": "User",
            "created": _timestamp(user.date_joined),
            "location": location,
        },
    }
    if user.scim_external_id:
        resource["externalId"] = user.scim_external_id
    if groups is not None:
        resource["groups"] = [
            {
                "value": str(g.pk),
                "display": g.display_name,
                "$ref": base + reverse("scim:group", kwargs={"resource_id": g.pk}),
            }
            for g in groups
        ]
    return resource


def group_resource(group: ScimGroup, base: str, *, members: bool = True) -> dict[str, Any]:
    resource: dict[str, Any] = {
        "schemas": [GROUP_SCHEMA],
        "id": str(group.pk),
        "displayName": group.display_name,
        "meta": {
            "resourceType": "Group",
            "created": _timestamp(group.created_at),
            "lastModified": _timestamp(group.updated_at),
            "location": base + reverse("scim:group", kwargs={"resource_id": group.pk}),
        },
    }
    if group.external_id:
        resource["externalId"] = group.external_id
    if members:
        resource["members"] = [
            {
                "value": str(m.user_id),
                "display": m.user.email,
                "$ref": base + reverse("scim:user", kwargs={"resource_id": m.user_id}),
            }
            for m in group.memberships.select_related("user").order_by("user__email")
        ]
    return resource


def list_response(resources: list[dict[str, Any]], total: int, start: int) -> dict[str, Any]:
    return {
        "schemas": [LIST_SCHEMA],
        "totalResults": total,
        "startIndex": start,
        "itemsPerPage": len(resources),
        "Resources": resources,
    }


def error(status: int, detail: str, scim_type: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"schemas": [ERROR_SCHEMA], "status": str(status), "detail": detail}
    if scim_type:
        body["scimType"] = scim_type
    return body
