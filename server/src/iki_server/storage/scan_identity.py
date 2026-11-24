"""Resolve physical labels in the caller's current inventory (ADR 0180)."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from django.db.models import Q

from iki_server.accounts.models import User
from iki_server.entities import access, registry
from iki_server.entities.models import HRID_PATTERN, Entity
from iki_server.storage import services
from iki_server.storage.codes import PREFIX, has_iki_prefix
from iki_server.storage.models import Place

type ScanMode = Literal["label", "iki_id"]
type IdentityBasis = Literal["record_link", "iki_id", "position", "choice", "label"]
type EvidenceBasis = Literal[
    "record_code",
    "record_link",
    "iki_id",
    "vendor",
    "hrid",
    "hrid_and_vendor",
    "unmatched",
    "ambiguous",
]

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_LINK = re.compile(r"(?:https?://[^/?#]+)?/entities/([^/?#]+)/?(?:[?#].*)?", re.IGNORECASE)
_LINK_PREFIX = re.compile(r"(?:https?://[^/?#]+)?/entities/", re.IGNORECASE)
_BARE_ID = re.compile(_UUID)
_LINK_ID = re.compile(rf"(?:{_UUID}|[0-9a-fA-F]{{32}})")
_COMPACT = re.compile(re.escape(PREFIX) + r"([0-9A-F]{32})")
_HRID = re.compile(HRID_PATTERN, re.IGNORECASE | re.ASCII)


def record_code(record_id: UUID) -> str:
    """Unique Data Matrix or QR payload printed on an Iki label."""
    return PREFIX + record_id.hex.upper()


def is_explicit(code: str, mode: ScanMode = "label") -> bool:
    text = code.strip()
    return mode == "iki_id" or has_iki_prefix(text) or bool(_LINK_PREFIX.match(text))


def direct_uuid(code: str) -> UUID | None:
    """A canonical hyphenated UUID accepted by a direct destination reference."""
    text = code.strip()
    return UUID(text) if _BARE_ID.fullmatch(text) else None


def _hrid(text: str) -> str | None:
    return text.upper() if _HRID.fullmatch(text) else None


def _identity(text: str) -> tuple[UUID | None, str | None]:
    compact = _COMPACT.fullmatch(text)
    if compact:
        return UUID(compact[1]), None
    link = _LINK.fullmatch(text)
    if link:
        if _LINK_ID.fullmatch(link[1]):
            return UUID(link[1]), None
        return None, _hrid(link[1])
    return None, None


class ScanRefused(Exception):  # noqa: N818 - public domain refusal
    def __init__(self) -> None:
        super().__init__(
            "This scan can’t identify a tube here. "
            "Use its printed Iki ID or ask an authorized reviewer."
        )


class ScanAmbiguous(Exception):  # noqa: N818 - public domain refusal
    def __init__(self, candidates: list[dict[str, Any]], index: int | None = None) -> None:
        self.candidates = candidates
        self.index = index
        super().__init__(
            "More than one readable record matches this scan. Choose the intended record."
        )


@dataclass(frozen=True)
class Match:
    candidates: tuple[Entity, ...]
    basis: IdentityBasis | None = None

    @property
    def status(self) -> Literal["one", "ambiguous", "none"]:
        if len(self.candidates) == 1:
            return "one"
        return "ambiguous" if self.candidates else "none"

    @property
    def record(self) -> Entity | None:
        return self.candidates[0] if len(self.candidates) == 1 else None


class ScanIndex:
    """Batch the supplied codes for one command. Scope IDs also establish which occupants
    the caller may read, without fetching any inaccessible occupant's barcode."""

    def __init__(
        self,
        user: User,
        codes: Iterable[str],
        *,
        scope_ids: Iterable[UUID] = (),
        include_nonstorable: bool = False,
    ) -> None:
        self.user = user
        texts = {value for raw in codes if (value := raw.strip())}
        types = services.storable_types()
        visible = access.visible(user)
        if not include_nonstorable:
            visible = visible.filter(type__in=types)
        by_field: dict[tuple[str, str], list[str]] = defaultdict(list)
        for type_name in types:
            storage = services.spec(type_name)
            if storage and storage.barcode:
                field = storage.barcode
                owner = services._field_owner(type_name, field).qualified_name
                by_field[owner, field].append(type_name)

        vendors: dict[str, set[UUID]] = defaultdict(set)
        wanted = set(scope_ids)
        hrids: set[str] = set()
        plain = {text for text in texts if not is_explicit(text)}
        for text in texts:
            record_id, link_hrid = _identity(text)
            if record_id is None:
                record_id = direct_uuid(text)
            if record_id is not None:
                wanted.add(record_id)
            if link_hrid:
                hrids.add(link_hrid)
        hrids.update(hrid for text in plain if (hrid := _hrid(text)) is not None)
        if plain:
            for (owner, field), configured_types in by_field.items():
                rows = (
                    registry.model(owner)
                    ._default_manager.filter(
                        pk__in=visible.filter(type__in=configured_types).values("pk"),
                        **{f"{field}__in": plain},
                    )
                    .values_list("pk", field)
                )
                for pk, value in rows:
                    vendors[value].add(pk)
                    wanted.add(pk)

        records = visible.filter(Q(pk__in=wanted) | Q(hrid__in=hrids)).order_by("hrid", "pk")
        self._records = {e.pk: e for e in records}
        self.readable_ids = self._records.keys()
        self._hrids: dict[str, set[UUID]] = defaultdict(set)
        for e in self._records.values():
            self._hrids[e.hrid.upper()].add(e.pk)
        self._vendors = vendors

    def resolve(
        self,
        code: str,
        *,
        mode: ScanMode = "label",
        choice: UUID | None = None,
        candidates: Collection[UUID] | None = None,
        occupant: UUID | None = None,
        protected: bool = False,
        has_concealed: bool = False,
        containers_only: bool = False,
        direct_id: bool = False,
    ) -> Match:
        text = code.strip()
        if (
            mode not in ("label", "iki_id")
            or protected
            or (occupant is not None and occupant not in self.readable_ids)
        ):
            raise ScanRefused()
        record_id, link_hrid = _identity(text) if mode == "label" else (None, None)
        explicit = is_explicit(text, mode)
        ids: set[UUID] = set()
        basis: IdentityBasis = "label"
        if record_id is not None:
            ids.add(record_id)
            basis = "record_link"
        elif link_hrid is not None:
            ids.update(self._hrids.get(link_hrid, ()))
            basis = "record_link"
        elif mode == "iki_id" or not explicit:
            if (hrid := _hrid(text)) is not None:
                ids.update(self._hrids.get(hrid, ()))
            if mode == "iki_id":
                basis = "iki_id"
            else:
                ids.update(self._vendors.get(text, ()))
                # A bare UUID in a destination can be a direct reference or the
                # literal instrument barcode of a different container.
                if direct_id and (bare := direct_uuid(text)) is not None:
                    ids.add(bare)
        if candidates is not None:
            ids.intersection_update(candidates)
        matches = {
            pk: self._records[pk]
            for pk in ids
            if pk in self._records
            and (not containers_only or services.is_container(self._records[pk].type))
        }
        if not explicit and occupant is not None and occupant in matches:
            # A shared label can identify the expected tube at its physical position.
            # A choice naming a different tube contradicts that evidence.
            if choice is not None and choice != occupant:
                raise ScanRefused()
            if has_concealed or len(matches) > 1:
                return Match((matches[occupant],), "position")
        elif has_concealed and not explicit and text:
            raise ScanRefused()
        if choice is not None:
            if choice not in matches:
                raise ScanRefused()
            return Match((matches[choice],), basis if explicit and len(matches) == 1 else "choice")
        if explicit and not matches:
            raise ScanRefused()
        if len(matches) == 1:
            return Match(tuple(matches.values()), basis)
        return Match(tuple(sorted(matches.values(), key=lambda e: (e.hrid, e.pk))))

    def evidence_basis(self, code: str, mode: ScanMode, match: Match) -> EvidenceBasis:
        """Which identifiers the receipt's original scan matched, including after a choice."""
        record = match.record
        if record is None:
            return "ambiguous" if match.candidates else "unmatched"
        text = code.strip()
        if mode == "iki_id":
            return "iki_id"
        if _COMPACT.fullmatch(text):
            return "record_code"
        if _LINK.fullmatch(text):
            return "record_link"
        vendor = record.pk in self._vendors.get(text, ())
        if record.hrid == _hrid(text):
            return "hrid_and_vendor" if vendor else "hrid"
        return "vendor" if vendor else "unmatched"

    def ambiguity(self, match: Match, *, index: int | None = None) -> ScanAmbiguous:
        return ScanAmbiguous(_describe(self.user, match.candidates), index)

    def describe(self, match: Match) -> dict[str, Any]:
        return {"status": match.status, "candidates": _describe(self.user, match.candidates)}


def _describe(user: User, records: Sequence[Entity]) -> list[dict[str, Any]]:
    from iki_server.api.entities import _lookup

    if not records:
        return []
    ids = {record.pk for record in records}
    places = {p.entity_id: p for p in Place.objects.filter(entity_id__in=ids)}
    container_ids = {p.container_id for p in places.values() if p.container_id is not None}
    refs, _ = _lookup(user, {"ref": ids | container_ids, "file": set()})
    shapes = services.shapes({pk for pk in container_ids if pk in refs and not refs[pk]["purged"]})
    owners = dict(
        access.readable_projects(user)
        .filter(pk__in={e.owner_project_id for e in records})
        .values_list("pk", "name")
    )
    items = []
    for record in records:
        ref = refs.get(record.pk)
        if not ref or ref["purged"]:
            # Access can change between lookup and rendering. Even the candidate count
            # must not disclose a record that has ceased to be readable.
            raise ScanRefused()
        owner = record.owner_project_id
        place = places.get(record.pk)
        box = None
        position = None
        if place and place.container_id in shapes:
            pk = place.container_id
            box = {"id": str(pk), "hrid": refs[pk]["hrid"], "title": refs[pk]["title"]}
            position = services.label(shapes[pk], place.row, place.column)
        items.append(
            {
                "id": str(record.pk),
                "type": record.type,
                "hrid": ref["hrid"],
                "title": ref["title"],
                "owner_project": {"id": str(owner), "name": owners[owner]}
                if owner in owners
                else None,
                "box": box,
                "position": position,
            }
        )
    return items
