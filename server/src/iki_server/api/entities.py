"""Entities of every type, through one typed API (ADR 0035).

- ``/entity-types`` lists the installed types, each with a JSON Schema of
  its values.
- ``/entities`` lists, creates, reads and changes entities. An entity is
  addressed by its ID or its HRID (``DNA-000042``).
- An entity of a type with an identity is a project's **record** of a registry
  entry (ADR 0115), with the entry's HRID: each project holding it has one. An
  HRID names the one you can read, or with ``?project=``, that project's;
  when you read several, it answers 409 (``choose_project``), listing them.
  Creating one registers its identity: ``registered`` says whether the entry is
  new, and ``reused`` that the project had a record of it already.
- Every read carries an ``ETag``, the entity's revision. A change must send
  it back as ``If-Match``. If someone else changed the entity since, the
  change is refused (412) rather than overwriting theirs. A change without
  ``If-Match`` is refused too (428).
- Entities you can't read answer 404, as if they didn't exist.
"""

import json
import re
from collections import defaultdict
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import F, Q, QuerySet
from django.http import HttpRequest, HttpResponse, StreamingHttpResponse
from ninja import Field, HeaderEx, P, Query, Router, Schema, Status
from pydantic import ConfigDict, ValidationError

from iki_server.accounts.models import User
from iki_server.amounts import eligibility
from iki_server.amounts.models import DrawPurpose
from iki_server.api import common, csvs
from iki_server.api.common import caller, project, require
from iki_server.api.pagination import DEFAULT_LIMIT, MAX_LIMIT, PageParams, page
from iki_server.api.problems import Problem
from iki_server.api.schemas import (
    AgentOut,
    Page,
    PersonRef,
    ProjectRef,
    RegistryRef,
    UserOut,
    agent_out,
)
from iki_server.audit import log as audit
from iki_server.entities import (
    access,
    assignments,
    checking,
    counts,
    exports,
    holds,
    people,
    registry,
    requests,
    schema,
    search,
    search_exports,
    services,
    states,
    unique,
)
from iki_server.entities.models import (
    Entity,
    ProjectLink,
    Revision,
    RevisionKind,
    RuleBreach,
    RuleHold,
)
from iki_server.entities.values import Invalid, Shape, shape, subtypes, to_json
from iki_server.notifications import declared
from iki_server.org import lab_roles, permissions
from iki_server.org.permissions import Action
from iki_server.runs.models import EntityMaking, ExternalOrigin, MadeBy, RunEffect
from iki_server.runs.services import visible_runs
from iki_server.signatures import changes as sig_changes
from iki_server.signatures import models as signing_models
from iki_server.signatures import services as sig_services
from iki_spec import titles, units
from iki_spec.model import (
    SIGNATURE_MEANINGS,
    EntityType,
    Rule,
    State,
    Track,
    Viewer,
    names_people,
    person_role,
)
from iki_spec.model import Field as SpecField

router = Router(tags=["entities"])

RegistryMark = Literal["identity", "derived", "fact"]


# --- Schemas -------------------------------------------------------------------------------------


class UnitOption(Schema):
    code: str = Field(description="UCUM, as the API takes it.")
    symbol: str = Field(description="As people write it.")


Meaning = Literal["authorship", "review", "approval", "verification", "responsibility", "witness"]


class WantedSignatureOut(Schema):
    """A signature setting a state needs (ADR 0126)."""

    meaning: Meaning = Field(description="What signing means (21 CFR 11.50).")
    meaning_label: str = Field(description="The meaning as people read it: Approval.")
    verb: str = Field(description="How its manifestation reads: “Approved by …”.")
    label: str = Field(description="How the plugin names it: “QA approval”.")
    statement: str = Field(description="What its signer attests to.")
    roles: list[str] = Field(
        description="The lab roles whoever gives it holds, any one: its own, or the state's "
        "`set_by`."
    )


class GuardOut(Schema):
    """A condition on setting a state field to a state (ADR 0128), in CEL."""

    name: str
    description: str | None = None
    check: str = Field(description="True when the change may be made.")
    when: str | None = Field(None, description="It applies only while this is true.")
    message: str = Field(description="What whoever's refused is told; `{{ }}` holds CEL.")


class HoldRuleOut(Schema):
    reviewed_by: list[str] = Field(
        description="The lab roles (ADR 0116) whose holders review a record it holds."
    )


class OverlapOut(Schema):
    from_: str = Field(alias="from", description="The field its range starts at.")
    to: str = Field(description="The field its range ends at.")
    per: list[str] = Field(description="Ranges only clash with the same values of these.")


class RuleOut(Schema):
    """A rule its records keep at every save (ADR 0127): a CEL `check`, or one the
    database holds (`unique` together, ranges that don't overlap)."""

    name: str = Field(description="Its name; an extension's is `plugin:name`.")
    plugin: str
    label: str
    description: str | None = None
    kind: Literal["check", "unique", "no_overlap"]
    check: str | None = Field(None, description="CEL over `record`, `now` and `today`.")
    when: str | None = Field(None, description="It applies only while this is true.")
    message: str = Field(description="What a save that breaks it is told; `{{ }}` holds CEL.")
    fields: list[str] = Field(description="The fields its message points at.")
    reads: list[str] = Field(
        description="The fields it reads: an edit is checked against it only if it changes "
        "one of them."
    )
    hold: HoldRuleOut | None = Field(
        None,
        description="It holds a record that breaks it for review, rather than refuse it: a "
        "save that breaks it needs `hold_reason`.",
    )
    unique: list[str] = Field(
        default_factory=list, description="No two of a project's records share these together."
    )
    no_overlap: OverlapOut | None = None
    while_: dict[str, list[str]] = Field(
        default_factory=dict,
        alias="while",
        description="For `unique` and `no_overlap`: only records with one of these values of "
        "each field count.",
    )


class StateOut(Schema):
    """One of a state field's states (ADR 0125)."""

    name: str
    label: str
    description: str | None = None
    set_by: list[str] = Field(
        description="The lab roles (ADR 0116) whose holders may set the field to it, from any "
        "other state: any one, in the record's project. Empty for an initial state no one "
        "sets."
    )
    locks: bool = Field(
        description="Locks the record against value edits, archiving, restoring and moving. "
        "A declared `on_edit` permits a reasoned correction."
    )
    on_edit: str | None = Field(
        None,
        description="The state this field returns to when an editable value changes. The "
        "correction needs a reason and permission to enter this state (ADR 0179).",
    )
    holds_amount: bool = Field(
        False,
        description="A record in it is held (ADR 0139): none of its amount can be drawn, "
        "transferred or split off until its state field is set to a state that doesn't.",
    )
    reason: Literal["required", "optional"] | None = Field(
        description="Whether setting it asks why: required, optional, or not at all (null)."
    )
    signatures: list[WantedSignatureOut] = Field(
        description="The electronic signatures setting it needs (ADR 0126): it's set by the "
        "last of them, once they're all given on the same revision, never directly. Empty for "
        "a state set directly."
    )
    guards: list[GuardOut] = Field(
        default_factory=list,
        description="Conditions on setting the field to it, besides `set_by` (ADR 0128): each "
        "must be true. `can.states` says, for a record, whether one refuses you.",
    )


class RowRoleOut(Schema):
    name: str = Field(description="The role: a value of the row's `by` field.")
    label: str
    min: int | None = Field(description="The fewest rows with this role, or null for none.")
    max: int | None = Field(description="The most rows with this role, or null for any.")
    to: dict[str, list[str]] = Field(
        description="For each of the row's references, by name: the types a row with this "
        "role may refer to, each with its subtypes."
    )


class RowOut(Schema):
    fields: list["FieldOut"] = Field(description="Each row's fields, in order.")
    identity: list[str] = Field(
        description="For a list in its type's identity (ADR 0138): the row fields that tell "
        "two rows apart. Empty: all of them."
    )
    by: str | None = Field(
        description="The row field, an enum or a string, that holds each row's role."
    )
    closed: bool = Field(
        description="Whether each row's role must be one of `roles`: a subtype's, which "
        "refines its parent's rows (ADR 0164), or one held by a string field. Otherwise a "
        "role `roles` doesn't name takes any count."
    )
    roles: list[RowRoleOut] = Field(description="What each role's rows take, in order.")


class FieldOut(Schema):
    name: str
    label: str
    description: str | None
    type: str
    format: str | None = Field(
        None,
        description="A string's format, such as `dna_sequence`: a sequence's takes `motif` "
        "in a search (ADR 0175).",
    )
    plugin: str = Field(description="The plugin that defines the field.")
    required: bool
    immutable: bool = Field(description="Can be set once, and can't change after.")
    computed: bool = Field(description="Set only by actions.")
    unique: bool = Field(
        description="No two entities in a project have the same value, such as a LIMS record's ID."
    )
    detached: bool = Field(
        description="Its plugin is detached (ADR 0091): it keeps its value, which can't change.",
    )
    link: str | None = Field(
        description="Where a value leads in another system: an https URL with `{value}` in it, "
        "for the value, URL-encoded."
    )
    unit: str | None = Field(description="Quantities: the UCUM unit values are in.")
    unit_symbol: str | None = Field(description="The unit as people write it, such as Å for Ao.")
    display_unit: str | None
    display_unit_symbol: str | None
    display_scale: float | None = Field(
        description="With a display unit: shown as value * display_scale + display_offset."
    )
    display_offset: float | None
    input_units: list[UnitOption] = Field(
        description="Quantities: common units values can be given in. Any convertible UCUM "
        "unit works."
    )
    to: list[str] = Field(
        description="References: the types they may refer to, any one of them or one of its "
        "subtypes (ADR 0136); `iki/core.Entity` for any entity. For a list, its items'. Empty "
        "otherwise."
    )
    values: list[str] = Field(
        description="Enums, and lists of them: the allowed values (for a list, its items')."
    )
    items: str | None = Field(description="Lists: the type of each item.")
    role: str | None = Field(
        description="For a field naming people (`person`, or a list of them): the lab role "
        "whoever it names must hold in the record's project (ADR 0116), or null for anyone "
        "who can read it.",
    )
    role_label: str | None = Field(description="What people call that role.")
    assignment: bool = Field(
        description="Whoever it names is assigned the record (ADR 0122): it's on their My work "
        "while the record's state field `assignment_field` is in one of `assignment_states`, "
        "and they're told.",
    )
    assignment_field: str | None = Field(
        description="The state field (ADR 0125) whose states say when it's their work. Null: "
        "it's their work in every state."
    )
    assignment_states: list[str] = Field(
        description="The states of `assignment_field` in which it's their work."
    )
    initial: str | None = Field(
        description="State fields (ADR 0125): the state new records start in. Null otherwise."
    )
    states: list[StateOut] = Field(
        description="State fields (ADR 0125): its states, in order. It's set on its own, with "
        "POST /entities/{ref}/states/{field}. A correction can also return it to the "
        "current state's `on_edit` destination. Empty otherwise.",
    )
    registry: RegistryMark | None = Field(
        None,
        description="Its registry entry's (ADR 0115), every holding project's: part of its "
        "identity, derived from it, or a fact its registrars curate. A fact is set while it's "
        "empty; after that only a registrar changes it. Null: each project's own.",
    )
    row: RowOut | None = Field(
        None,
        description="A list of rows (`items` is `row`, ADR 0137): each row's fields, and what "
        "each role takes. Its value is a list of objects, one per row. Null otherwise.",
    )


RowOut.model_rebuild()


class TrackOut(Schema):
    field: str = Field(description="The json field holding one number per residue, or null.")
    label: str = Field(description="What the track is called.")
    min: float | None = Field(
        description="The least value its field's schema allows, which the track's scale starts "
        "from, or null to fit the values."
    )
    max: float | None = Field(
        description="The greatest value its field's schema allows, or null to fit the values."
    )


class SequenceViewerOut(Schema):
    viewer: Literal["sequence"] = Field(
        description="`sequence`: the sequence, with its features and tracks along it."
    )
    label: str = Field(description="What its tab is called.")
    sequence: str = Field(description="The field holding the sequence.")
    alphabet: Literal["dna", "rna", "protein"] = Field(
        description="What it's a sequence of: DNA's bases, RNA's (U, not T) or amino acids."
    )
    strands: str | None = Field(
        description="For DNA or RNA: the enum field saying whether a record is double- or "
        "single-stranded (`double` or `single`), as its sequence field's `strands` names it, "
        "so its length is counted in bp or nt; null when it has none."
    )
    topology: str | None = Field(
        description="The enum field saying whether a record's sequence is circular "
        "(`circular`), as its sequence field's `topology` names it, so a search for bases "
        "reads across a circle's origin and not across a line's ends; null when it has none."
    )
    features: str | None = Field(
        description="A json field of regions along it (type, start, end, name, strand), or null."
    )
    tracks: list[TrackOut] = Field(description="Values along its residues, each a track.")


class StructureViewerOut(Schema):
    viewer: Literal["structure"] = Field(description="`structure`: a 3D structure, in Mol*.")
    label: str = Field(description="What its tab is called.")
    structure: str = Field(
        description="The file field holding the structure: mmCIF (chemical/x-mmcif) or PDB "
        "(chemical/x-pdb)."
    )


class AlignmentViewerOut(Schema):
    viewer: Literal["alignment"] = Field(description="`alignment`: aligned sequences, a row each.")
    label: str = Field(description="What its tab is called.")
    alignment: str = Field(description="The file field holding the alignment, as FASTA.")
    rows: str | None = Field(
        description="A list field of references to the sequences in Iki it aligns, or null. A "
        "row whose FASTA header is one's HRID links to it; others, such as reads, are named by "
        "their headers."
    )
    reference: str | None = Field(
        description="A reference field naming the sequence the others were aligned to, which "
        "is shown first, or null."
    )


ViewerOut = Annotated[
    SequenceViewerOut | StructureViewerOut | AlignmentViewerOut, Field(discriminator="viewer")
]


class FileFormatOut(Schema):
    format: Literal["genbank", "fasta"] = Field(description="What `/entities/{ref}/export` takes.")
    label: str = Field(description="Its name, such as GenBank.")
    extension: str = Field(description="Its file name's, such as gb.")
    media_type: str = Field(description="Its media type, such as chemical/seq-na-genbank.")
    description: str = Field(description="What of a record it holds.")


class FileExportOut(Schema):
    export: Literal["sequence"] = Field(
        description="What it writes. `sequence`: a DNA or RNA sequence, with its features."
    )
    formats: list[FileFormatOut] = Field(description="The files it's written as.")
    sequence: str = Field(
        description="The field it's written from: a string field with a DNA or RNA format, "
        "or a reference to a record of a type that exports a sequence."
    )
    features: str | None = Field(
        description="A json field of features (type, start, end, name, strand) it adds "
        "along the sequence, or null. A record page draws them as a map."
    )


class TypeNotificationOut(Schema):
    """A notification a type declares (ADR 0129), as its page previews it."""

    name: str
    kind: str = Field(
        description="Its kind, which people's notification settings choose by: "
        "plugin.Type.name, of the type that declares it."
    )
    label: str
    description: str | None
    declared_by: str = Field(description="The type that declares it: this one or a parent.")
    when: str = Field(description="When it's sent, in words.")
    only_some: bool = Field(description="Whether only records its conditions find send it.")
    tells: list[str] = Field(description="Whom it tells, in words.")
    email: bool = Field(description="Whether it's emailed unless someone chooses otherwise.")
    required: bool = Field(description="Whether nobody can turn it off in Iki.")


class RequestTypeOut(Schema):
    """What makes a type's records requests between projects (ADR 0132)."""

    service_fields: list[str] = Field(
        description="The fields the service it's sent to changes, and the state fields it "
        "sets, with the lab roles its people hold in its project. The rest are the asking "
        "project's."
    )
    sends: list[str] = Field(
        description="Its references whose records go to the service with it, such as its "
        "samples: what the service can read of the asking project."
    )


class ResultTypeOut(Schema):
    """What makes a type's records a request's results (ADR 0132)."""

    of: str = Field(description="Its reference to the request it's a result of.")
    released_field: str = Field(description="The service's state field that releases it.")
    released_states: list[str] = Field(
        description="The states that send it back to the asking project, where it stays."
    )


class TypeAmountOut(Schema):
    unit: str = Field(description="The UCUM unit it's kept in, such as uL, mg or {vial}.")
    label: str = Field(description="What it's called, such as Volume.")
    description: str | None
    display_unit: str | None = Field(description="The unit it's shown in, if not `unit`.")
    concentration: str | None = Field(
        description="The quantity field giving how much of it each volume holds: an amount "
        "can then be drawn as a mass of a volume, or a volume of a mass."
    )
    aliquots: str | None = Field(
        description="The type its aliquots are: its own, or another such as an Aliquot."
    )
    aliquots_of: str | None = Field(
        description="For aliquots of another type, its reference naming the record each was "
        "split from."
    )
    material: str | None = Field(
        description="Its reference naming what its amount is of, such as a lot's molecule: "
        "a transfer into one of its records takes only that entity's, and its aliquots are "
        "of it too."
    )


class EntityTypeOut(Schema):
    name: str = Field(description="Qualified: plugin.Type.")
    plugin: str
    label: str
    label_plural: str
    description: str | None
    extends: str | None
    abstract: bool = Field(description="Abstract types have no entities of their own.")
    sample: bool = Field(
        description="Its records are samples (ADR 0177): a project's files are found by them."
    )
    detached: bool = Field(
        description="Its plugin is detached (ADR 0091): its entities can be read, not changed, "
        "and no new ones can be made."
    )
    subtypes: list[str] = Field(description="The types that extend it, at any depth.")
    hrid_prefix: str | None
    identity: list[str] = Field(
        description="The fields that tell two entities apart. With one, its entities are "
        "projects' records of registry entries (ADR 0115): one entry per identity in a "
        "registry, one record of it per project."
    )
    fields: list[FieldOut]
    values_schema: dict[str, Any] = Field(
        description="A JSON Schema (2020-12) of the entity's values."
    )
    viewers: list[ViewerOut] = Field(
        description="Iki's own viewers on its entities' pages, each a tab: its own, or its "
        "nearest parent's."
    )
    exports: list[FileExportOut] = Field(
        default_factory=list,
        description="The files Iki writes of its records (ADR 0171), from "
        "`/entities/{ref}/export`: its own, or its nearest parent's.",
    )
    due: str | None = Field(
        None,
        description="The date or datetime field saying when its records are due (ADR 0122), "
        "which My work sorts by.",
    )
    rules: list[RuleOut] = Field(
        default_factory=list,
        description="The rules its records keep at every save (ADR 0127): its own, its "
        "parents' and those extensions add.",
    )
    rule_variables: dict[str, Any] | None = Field(
        None,
        description="What its rules' `check` reads, as the schemas iki-wasm evaluates them "
        "against: so a form checks them as you type, as the server will.",
    )
    notifications: list[TypeNotificationOut] = Field(
        description="Whom its records tell, and when (ADR 0129): its own notifications and "
        "its parents'.",
    )
    request: RequestTypeOut | None = Field(
        None,
        description="Its records are requests one project sends to another's service (ADR "
        "0132): which fields the service writes, and which send records with it.",
    )
    result: ResultTypeOut | None = Field(
        None,
        description="Its records are a request's results (ADR 0132), which the service owns "
        "and releases back to the asking project.",
    )
    amount: TypeAmountOut | None = Field(
        None,
        description="How much of each of its records there is (ADR 0139), if it keeps that: "
        "drawn, transferred, split into aliquots and adjusted through "
        "`/entities/{ref}/amount`, never below zero.",
    )


class StateCan(Schema):
    """A state a record's state field could be set to (ADR 0125), and whether you may."""

    name: str
    label: str
    locks: bool
    reason: Literal["required", "optional"] | None = Field(
        description="Whether it asks why: required, optional, or not at all (null)."
    )
    allowed: bool = Field(description="You may set it now.")
    why_not: str | None = Field(description="Why you may not, when you may not.")
    guard: str | None = Field(
        None, description="The guard refusing it (ADR 0128), when one is why you may not."
    )
    signed: bool = Field(
        description="It's set by signing for it (ADR 0126), never directly: see `can.sign`."
    )


class GivenSignatureOut(Schema):
    """A signature given for setting a state, on the record's current revision."""

    id: UUID
    signer: PersonRef
    signed_at: datetime
    manifestation: str = Field(
        description="As people read it: who, when (UTC), and what it means (21 CFR 11.50)."
    )


class SignatureCan(Schema):
    """One of the signatures setting a record's state needs (ADR 0126): given, or whether
    you may give it."""

    index: int = Field(description="Which, from 0: send it to sign.")
    meaning: Meaning
    meaning_label: str
    label: str
    statement: str = Field(description="What you attest to by giving it.")
    roles: list[str]
    role_labels: list[str]
    given: GivenSignatureOut | None = Field(description="The signature that counts for it.")
    allowed: bool = Field(description="You may give it now.")
    why_not: str | None = Field(description="Why you may not, when you may not.")
    lapsed: str | None = Field(
        description="A signature given for it that no longer counts, and why: its signer no "
        "longer holds the role."
    )


class SignCan(Schema):
    """Setting one of the record's state fields to a state that needs signatures (ADR
    0123): each of them, given or not. Give one with POST /entities/{ref}/signatures, in
    the web app."""

    field: str = Field(description="The state field.")
    field_label: str
    to: str = Field(description="The state it sets.")
    to_label: str
    reason: Literal["required", "optional"] | None = Field(
        description="Whether each signer says why: required, optional, or not at all (null)."
    )
    allowed: bool = Field(description="You may give one of its signatures now.")
    why_not: str | None = Field(description="Why you may not, when you may not.")
    signatures: list[SignatureCan]


class EntityCan(Schema):
    edit: bool = Field(
        description="Change it, archive or restore it: false for everyone when its plugin is "
        "detached, or while a state locks it."
    )
    correct: bool = Field(
        False,
        description="You may attempt a reasoned correction. Each locking field declares an "
        "`on_edit` destination you have permission to enter, and the first return's guards "
        "allow it. Later guards see each preceding return in the same save (ADR 0179).",
    )
    edit_amount: bool = Field(
        description="Change its amount, including while a state locks the record's values."
    )
    draw_purposes: list[DrawPurpose] = Field(
        default_factory=list,
        description="The purposes you may draw for. State holds and draw guards still apply.",
    )
    states: dict[str, list[StateCan]] = Field(
        description="For each of its state fields (ADR 0125), the states it could be set to: "
        "every one but the one it's in, each with whether you may set it and why not. Empty "
        "for a type without state fields.",
    )
    sign: list[SignCan] = Field(
        description="The states its state fields may be set to now that need electronic "
        "signatures (ADR 0126), each with its signatures, given or not.",
    )
    take: list[str] = Field(
        default_factory=list,
        description="The assignment fields you may take it in (ADR 0122), with `POST "
        "/entities/{ref}/take`: each names nobody who can be named now, and you can be.",
    )


class PersonOut(Schema):
    """Someone a record's values name (ADR 0122)."""

    id: UUID = Field(description="Theirs for good: what a value names them by.")
    name: str
    email: str | None = Field(
        description="Their email address, when you could see it on the record's project's "
        "Members page: when you're both in it. Otherwise null."
    )
    active: bool = Field(description="False once they're deactivated.")


class ReassignOut(Schema):
    """Someone an assignment field names who can't be named there now (ADR 0122)."""

    field: str
    person: UUID
    why: str = Field(description="Why: deactivated, no longer in the project, or the role.")


class RefOut(Schema):
    hrid: str
    title: str = Field(description="What it's called: its type's display title, or its HRID.")
    type: str
    archived: bool
    purged: bool = Field(
        description="Its plugin's data was purged (ADR 0092): only its HRID and type are left."
    )
    formerly: list[str] = Field(
        default_factory=list,
        description="The HRIDs it had where it was imported from, when it got a new one here "
        "(ADR 0096): files made there, such as an alignment's, name it by them.",
    )


class FileRef(Schema):
    name: str
    media_type: str
    size: int = Field(description="Bytes.")


class SupersededOut(Schema):
    by: RefOut = Field(description="The entry that replaced it.")
    by_id: UUID
    at: datetime
    reason: str


class EntryRefOut(Schema):
    """A record's registry entry (ADR 0115): its identity, ID and registry values."""

    id: UUID
    hrid: str
    registry: RegistryRef
    revision: int = Field(description="The entry's latest revision.")
    superseded: SupersededOut | None = Field(
        description="A registrar replaced it with another entry: move the record there."
    )


class HoldOut(Schema):
    """A rule holding the record for review (ADR 0127): a save broke it, and was kept
    with a reason."""

    id: UUID
    rule: str
    label: str
    message: str = Field(description="What the save that held it was told.")
    reviewed_by: list[str] = Field(description="The lab roles whose holders may review it.")
    held_by: UserOut | None = Field(description="Who saved it; null for a deploy's check.")
    agent: str | None = Field(None, description="The app or agent that saved it for them.")
    held_at: datetime
    reason: str = Field(description="Why it was saved anyway.")
    revision: int = Field(description="The revision that held it.")
    last_revision: int = Field(description="The latest revision that broke the rule again.")
    can_review: bool = Field(description="You may review it now.")
    why_not: str | None = Field(None, description="Why you may not, when you may not.")


class HeldOut(Schema):
    """A hold a revision opened (ADR 0127), as the record's history keeps it: why the save
    was kept, and its review, once someone has reviewed it."""

    id: UUID
    rule: str
    label: str
    message: str = Field(description="What the save that held it was told.")
    held_by: UserOut | None = Field(description="Who saved it; null for a deploy's check.")
    agent: str | None = Field(None, description="The app or agent that saved it for them.")
    held_at: datetime
    reason: str = Field(description="Why it was saved anyway.")
    revision: int = Field(description="The revision that held it.")
    last_revision: int = Field(description="The latest revision that broke the rule again.")
    reviewer: UserOut | None = Field(description="Who reviewed it; null while it's held.")
    reviewed_at: datetime | None
    review_reason: str = Field(description="Why the reviewer let it stand.")


class BreachOut(Schema):
    """A rule the record breaks, found after it was saved (ADR 0127): its plugin added or
    changed the rule, or a bundle brought it. The next save that checks it clears it."""

    rule: str
    label: str
    message: str
    found_at: datetime


class RequestSidesOut(Schema):
    """A request's two sides (ADR 0132): the project that asked, which owns it, and the
    service it was sent to."""

    asking_project: ProjectRef | None = Field(description="The project that owns and sent it.")
    service: ProjectRef | None = Field(
        description="The project serving it, once it's sent; null before."
    )
    submitted: bool
    submitted_at: datetime | None
    submitted_by: UserOut | None
    service_fields: list[str] = Field(
        description="The fields the service changes, and the state fields it sets. The rest "
        "are the asking project's."
    )
    sends: list[str] = Field(description="The fields whose records go to the service with it.")
    yours: list[Literal["asker", "service"]] = Field(
        description="The sides you may change it as: `asker`, as an Editor in its project, "
        "and `service`, as an Editor in the service's."
    )


class AmountIn(Schema):
    value: Decimal = Field(description="How much: a number, or a string for an exact decimal.")
    unit: str = Field(max_length=50, description="Its UCUM unit, such as uL, mg or umol.")


class AmountOut(Schema):
    value: str = Field(
        pattern=r"^\d+(\.\d+)?$", description="How much there is, exactly, in `unit`."
    )
    unit: str = Field(description="The unit its type keeps it in.")
    label: str = Field(description="What its type calls it, such as Volume.")
    display: str | None = Field(
        description="As its type shows it, in its `display_unit`, such as 0.25 mL. None if "
        "it's shown in `unit`."
    )
    held_by: str | None = Field(
        description="The state field whose state holds it, such as quarantine: none of it is "
        "drawn, transferred or split off until that changes. None if nothing holds it."
    )
    updated_at: datetime


class EntityOut(Schema):
    id: UUID
    hrid: str
    title: str = Field(description="What it's called: its type's display title, or its HRID.")
    type: str
    revision: int = Field(description="Its latest revision. Send it back as If-Match to change it.")
    owner_project: ProjectRef | None = Field(
        description="The project that owns it, and decides its changes. Null if you can't see it."
    )
    projects: list[ProjectRef] = Field(
        default_factory=list,
        description="The projects it belongs to that you can read (ADR 0176): its owner "
        "project first, then those it's linked into, by name. Projects you can't read aren't "
        "named.",
    )
    created_by: UserOut | None
    created_at: datetime
    updated_at: datetime
    archived: bool
    archived_at: datetime | None
    locked: bool = Field(
        description="A state locks it (ADR 0125). Archiving, restoring and moving need every "
        "lock lifted first. A reasoned value edit may return it for review (see `can.correct`)."
    )
    locked_by: list[str] = Field(
        description="The state fields whose states lock it, in its type's order."
    )
    values: dict[str, Any] = Field(description="Its fields' values, as its type's schema says.")
    refs: dict[str, RefOut] = Field(
        description="The entities its values refer to that you can read, by ID."
    )
    files: dict[str, FileRef] = Field(
        description="The files its values refer to that you can read, by ID."
    )
    people: dict[str, PersonOut] = Field(
        default_factory=dict, description="The people its values name, by ID (ADR 0122)."
    )
    needs_reassigning: list[ReassignOut] = Field(
        default_factory=list,
        description="Who its assignment fields counting in its state name that can't be named "
        "there any more (ADR 0122): for those who can read its project, who reassign it.",
    )
    entry: EntryRefOut | None = Field(
        None,
        description="For a record of a type with an identity, its registry entry (ADR 0115): "
        "the values marked `registry` are the entry's.",
    )
    holds: list[HoldOut] = Field(
        default_factory=list,
        description="The rules holding it for review (ADR 0127), oldest first. While any "
        "does, it can't be set to a state that locks it.",
    )
    breaches: list[BreachOut] = Field(
        default_factory=list,
        description="Rules it breaks that it was saved before (ADR 0127).",
    )
    request: RequestSidesOut | None = Field(
        None,
        description="For a request between projects (ADR 0132): the project that asks and the "
        "service it's sent to, and whose each field is. Null for a record of another type.",
    )
    amount: AmountOut | None = Field(
        None,
        description="For a type with an amount (ADR 0139), how much there is of it: changed "
        "only through `/entities/{ref}/amount`. None if none is recorded.",
    )
    draw_refusals: dict[DrawPurpose, str] = Field(
        default_factory=dict,
        description="The first draw guard refusing each purpose, including on a record "
        "without a balance. A state holding its amount also refuses all withdrawals.",
    )
    can: EntityCan


class HoldReviewIn(Schema):
    reason: str = Field(
        min_length=1, max_length=2000, description="Why the record may stand as it is."
    )


class CreatedOut(EntityOut):
    reused: bool = Field(
        description="The project had a record of this identity already: it's returned as it "
        "is, unchanged."
    )
    registered: bool = Field(
        description="A new registry entry was made for it. False when the identity was "
        "registered already, by any project: then the project gets a record of that entry."
    )


class EntityIn(Schema):
    type: str = Field(description="Qualified, such as iki/dna.DNASequence.")
    project: UUID = Field(description="The project it belongs to. It needs you as an Editor.")
    values: dict[str, Any] = Field(default_factory=dict)
    amount: AmountIn | None = Field(
        None,
        description="For a type with an amount (ADR 0139): how much there is of it to start "
        "with, in its unit or one that converts to it.",
    )
    reason: str = Field("", max_length=2000, description="Why, for the record.")
    hold_reason: str | None = Field(
        None,
        max_length=2000,
        description="Why to save it though it breaks rules that hold records for review, "
        "such as a result outside its specification (ADR 0127): it's saved, and held. "
        "Without it, such a save is refused with `hold_reason_required`.",
    )


class EntityPatch(Schema):
    values: dict[str, Any] | None = Field(None, description="Only the fields to change.")
    archived: bool | None = Field(
        None, description="Archived entities can't change: restore one to change it."
    )
    reason: str = Field(
        "",
        max_length=2000,
        description="Why the values changed. Required for an edit that returns a state "
        "field to its declared `on_edit` destination.",
    )
    hold_reason: str | None = Field(
        None,
        max_length=2000,
        description="As for creating one: why to save values that break rules that hold.",
    )


class StateIn(Schema):
    state: str = Field(description="The state to set it to, by name.", max_length=50)
    reason: str = Field(
        "", max_length=2000, description="Why: a state may require it, and its history keeps it."
    )


class NamedOut(Schema):
    name: str
    label: str


class CarriedSignatureOut(Schema):
    id: UUID
    manifestation: str = Field(
        description="Who signed, when (UTC), and what it means (21 CFR 11.50); empty if the "
        "signature is gone."
    )
    present: bool = Field(description="The signature is there: false only if it was deleted.")


class RevisionStateOut(Schema):
    field: NamedOut = Field(description="The state field set.")
    from_: NamedOut | None = Field(alias="from", description="The state it was in.")
    to: NamedOut = Field(description="The state it was set to.")
    signatures: list[CarriedSignatureOut] = Field(
        description="The electronic signatures that set it (ADR 0126), in order: each as "
        "people read it. Empty for a state set directly. GET /entities/{ref}/signatures has "
        "them in full."
    )


class RunRefOut(Schema):
    id: UUID
    hrid: str
    action: str


class RevisionOut(Schema):
    number: int
    kind: Literal[
        "created",
        "edited",
        "archived",
        "restored",
        "curated",
        "superseded",
        "moved",
        "state_changed",
    ]
    entry: bool = Field(
        False,
        description="It's a revision of the record's registry entry (ADR 0115), not of the "
        "record. Who made it, how and when are told only to the project it was made through, "
        "and for a registrar's change, to every holder: for others they're empty.",
    )
    changed_fields: list[str]
    user: UserOut | None = Field(description="Who, or for a run, who launched it.")
    via: str = Field(description="How: web, api, mcp, run, …")
    client: str = Field(
        description="The OAuth app it came through, such as an AI agent's MCP client, if any."
    )
    agent: AgentOut | None = Field(
        description="The agent that made it, acting for user, if one did (ADR 0072)."
    )
    run: RunRefOut | None = Field(
        None,
        description="The run that made it, when `via` is `run` and you can read the run's "
        "project. For a registry entry's revision, the run that made it through a record.",
    )
    run_withheld: bool = Field(
        False,
        description="A run made it, in a project you can't read, so `run` is null. When `via` "
        "is `run` and both are empty, no run is recorded for it: it was imported, say.",
    )
    reason: str
    state: RevisionStateOut | None = Field(
        description="For a state change (ADR 0125): which state field, and the states it "
        "went from and to. One a plugin's migration made, moving records out of a state it "
        "removed, is by no one, via `system`."
    )
    holds: list[HeldOut] = Field(
        default_factory=list,
        description="The holds this revision opened (ADR 0127), by breaking a rule that "
        "holds, each with its review once it's reviewed. Kept after the review.",
    )
    submitted: dict[str, Any] | None = Field(
        None,
        description="A record's first revision, of a registry entry its identity found "
        "(ADR 0138): its identity's values as they were given, where they're spelled other "
        "than the entry's, such as a plasmid from another origin or on its other strand. "
        "Features given along it were placed along the entry's.",
    )
    created_at: datetime | None


class ChangeOut(Schema):
    before: Any = None
    after: Any = None


class RevisionDetailOut(RevisionOut):
    values: dict[str, Any] = Field(description="Every field's value as it was at this revision.")
    changes: dict[str, ChangeOut] = Field(description="What this revision changed.")
    refs: dict[str, RefOut] = Field(
        description="The entities its values and changes refer to that you can read, by ID."
    )
    files: dict[str, FileRef] = Field(
        description="The files its values and changes refer to that you can read, by ID."
    )
    people: dict[str, PersonOut] = Field(
        default_factory=dict,
        description="The people its values and changes name, by ID (ADR 0122).",
    )


class EntityProjectOut(Schema):
    project: ProjectRef
    owner: bool
    linked_by: UserOut | None
    linked_at: datetime


class EntityFilters(PageParams):
    type: str | None = Field(None, description="This type and its subtypes.")
    types: list[str] | None = Field(
        None,
        max_length=20,
        description="Any one of these types, and their subtypes, as a reference to several "
        "types names them (ADR 0136). Not with `type`.",
    )
    exact_type: bool = Field(False, description="Only the type itself, not its subtypes.")
    project: UUID | None = Field(None, description="Entities that belong to this project.")
    archived: bool | None = None
    q: str | None = Field(
        None,
        max_length=200,
        description="Entities whose HRID, or a text field in their title, contains this.",
    )
    collection: str | None = Field(
        None,
        max_length=100,
        description="Members of this collection (its ID or HRID): those it lists, or those its "
        "search finds, as you can see them.",
    )
    run: str | None = Field(
        None,
        max_length=100,
        description="What this run (its ID or HRID) wrote, or found already written: a "
        "spreadsheet import's records, say.",
    )


OPERATOR = Literal[
    "is_set",
    "eq",
    "ne",
    "in",
    "lt",
    "lte",
    "gt",
    "gte",
    "contains",
    "starts_with",
    "matches",
    "has",
    "of",
    "motif",
]


class Condition(Schema):
    """A condition on a field, or a group of them: exactly one of ``field`` (with ``op`` and
    ``value``), ``all``, ``any`` or ``not``."""

    model_config = ConfigDict(extra="forbid")

    field: str | None = Field(None, description="A field of the type, or of every entity.")
    op: OPERATOR | None = None
    value: Any = Field(None, description="What to compare with, as the field takes it.")
    all: list["Condition"] | None = Field(None, description="Every one of these.")
    any: list["Condition"] | None = Field(None, description="At least one of these.")
    not_: "Condition | None" = Field(None, alias="not", description="Not this.")


class SortIn(Schema):
    field: str = Field(description="A field to sort by: text, numbers, dates or yes/no.")
    descending: bool = False


class SearchIn(Schema):
    type: str | None = Field(None, description="This type and its subtypes.")
    exact_type: bool = Field(False, description="Only the type itself, not its subtypes.")
    project: UUID | None = Field(None, description="Entities that belong to this project.")
    archived: bool | None = None
    q: str | None = Field(
        None, max_length=200, description="Text in their HRID, or a text field in their title."
    )
    collection: str | None = Field(
        None,
        max_length=100,
        description="Members of this collection (its ID or HRID): those it lists, or those its "
        "search finds, as you can see them.",
    )
    run: str | None = Field(
        None,
        max_length=100,
        description="What this run (its ID or HRID) wrote, or found already written: a "
        "spreadsheet import's records, say.",
    )
    where: Condition | None = None
    sort: SortIn | None = None
    cursor: str | None = Field(None, description="From the previous page's next_cursor.")
    limit: int = Field(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Items per page.")


# --- Entity types --------------------------------------------------------------------------------


def _types() -> dict[str, EntityType]:
    return {n: t for n, t in registry.types().items() if n != "iki/core.Entity"}


def field_out(name: str, f: SpecField, plugin: str) -> dict[str, Any]:
    """A field as `FieldOut` describes it: an entity type's, or an action's param."""
    return {
        "name": name,
        "label": f.label,
        "description": f.description,
        "type": f.type,
        "format": f.format,
        "plugin": plugin,
        "required": f.required,
        "immutable": f.immutable,
        "unique": f.unique,
        "detached": plugin in registry.detached(),
        "link": unique.link(f, plugin),
        "computed": f.computed,
        "unit": f.unit,
        "unit_symbol": units.symbol(f.unit) if f.unit else None,
        "display_unit": f.display_unit,
        "display_unit_symbol": units.symbol(f.display_unit) if f.display_unit else None,
        **_display(f.unit, f.display_unit),
        "input_units": _input_units(f.unit, f.display_unit),
        "to": list(f.items.to if f.type == "list" and f.items is not None else f.to),
        # A list of choices offers its items' (`items: {type: enum, values: [...]}`).
        "values": list(f.items.values if f.type == "list" and f.items is not None else f.values),
        "items": f.items.type if f.items is not None else None,
        "role": person_role(f),
        "role_label": lab_roles.label(role) if (role := person_role(f)) is not None else None,
        "assignment": f.assignment and names_people(f),
        "assignment_field": f.assignment_field,
        "assignment_states": list(f.assignment_states),
        "initial": f.initial if f.type == "state" else None,
        "states": [_state_out(st) for st in f.states.values() if not st.removed]
        if f.type == "state"
        else [],
        "row": _row_out(f.items, plugin) if f.is_rows and f.items is not None else None,
    }


def _row_out(row: SpecField, plugin: str) -> dict[str, Any]:
    return {
        "fields": [field_out(n, cell, plugin) for n, cell in row.fields.items()],
        "identity": list(row.identity),
        "by": row.by,
        # Whether a row's role is one of `roles` and nothing else: a subtype's (ADR 0164),
        # or one given by a string field. Otherwise roles only count what they name.
        "closed": row.closed,
        "roles": [
            {
                "name": r.name,
                "label": r.label,
                "min": r.min,
                "max": r.max,
                "to": {c: list(t) for c, t in r.to.items()},
            }
            for r in row.roles.values()
        ],
    }


#: What a sequence viewer's field is a sequence of, by its format.
ALPHABETS = {"dna_sequence": "dna", "rna_sequence": "rna", "protein_sequence": "protein"}


def _viewer_out(form: Shape, viewer: Viewer) -> dict[str, Any]:
    if viewer.viewer == "structure":
        return {"viewer": "structure", "label": viewer.label, "structure": viewer.structure}
    if viewer.viewer == "alignment":
        return {
            "viewer": "alignment",
            "label": viewer.label,
            "alignment": viewer.alignment,
            "rows": viewer.rows or None,
            "reference": viewer.reference or None,
        }
    spec = form.fields().get(viewer.sequence)
    return {
        "viewer": "sequence",
        "label": viewer.label,
        "sequence": viewer.sequence,
        "alphabet": ALPHABETS.get((spec.format or "") if spec is not None else "", "dna"),
        "strands": (spec.strands if spec is not None else None) or None,
        "topology": (spec.topology if spec is not None else None) or None,
        "features": viewer.features or None,
        "tracks": [_track_out(form, t) for t in viewer.tracks],
    }


def _track_out(form: Shape, track: Track) -> dict[str, Any]:
    spec = form.fields().get(track.field)
    items = ((spec.schema if spec else None) or {}).get("items") or {}
    low, high = items.get("minimum"), items.get("maximum")
    return {
        "field": track.field,
        "label": track.label or (spec.label if spec and spec.label else track.field),
        "min": low if isinstance(low, int | float) else None,
        "max": high if isinstance(high, int | float) else None,
    }


def _type_out(entity_type: EntityType) -> dict[str, Any]:
    form = shape(entity_type.qualified_name)
    owner = registry.identity_owner(entity_type)
    fields = []
    identity = set(owner.identity) if owner is not None else set()
    for part in form.parts:
        for name, f in part.fields.items():
            if f.removed:
                continue
            out = field_out(name, f, part.plugin)
            if part.entry:
                out["registry"] = "identity" if name in identity else f.registry
            fields.append(out)
    name = entity_type.qualified_name
    return {
        "name": name,
        "plugin": entity_type.plugin,
        "label": entity_type.label or entity_type.name,
        "label_plural": entity_type.label_plural or f"{entity_type.label or entity_type.name}s",
        "description": entity_type.description,
        "extends": entity_type.extends if entity_type.extends != "iki/core.Entity" else None,
        "abstract": entity_type.abstract,
        "sample": name in registry.sample_types(),
        "detached": services.frozen(form) is not None,
        "subtypes": sorted(subtypes(name) - {name}),
        "hrid_prefix": entity_type.hrid_prefix,
        "identity": list(owner.identity) if owner is not None else [],
        "fields": fields,
        "values_schema": schema.for_type(name),
        "viewers": [_viewer_out(form, v) for v in registry.viewers(entity_type)],
        "exports": [
            {
                "export": e.export,
                "formats": [asdict(f) for f in exports.FORMATS[e.export]],
                "sequence": e.sequence,
                "features": e.features or None,
            }
            for e in registry.exports(entity_type)
        ],
        "due": assignments.due_field_name(name) if assignments.due_field(name) else None,
        "rules": [_rule_out(r) for r in checking.rules_of(name)],
        "rule_variables": checking.record_variables(name) if checking.checks_of(name) else None,
        "notifications": declared.describe(entity_type),
        "request": _request_type_out(name),
        "result": _result_type_out(name),
        "amount": _type_amount(name),
    }


def _rule_out(rule: Rule) -> dict[str, Any]:
    return {
        "name": rule.key,
        "plugin": rule.plugin,
        "label": rule.label,
        "description": rule.description,
        "kind": rule.kind,
        "check": rule.check,
        "when": rule.when,
        "message": rule.message,
        "fields": list(rule.fields or rule.reads),
        "reads": list(rule.reads),
        "hold": {"reviewed_by": list(rule.hold)} if rule.hold else None,
        "unique": list(rule.unique),
        "no_overlap": {
            "from": rule.no_overlap[0],
            "to": rule.no_overlap[1],
            "per": list(rule.per),
        }
        if rule.no_overlap
        else None,
        "while": {name: list(values) for name, values in rule.only_while},
    }


def _request_type_out(name: str) -> dict[str, Any] | None:
    spec = requests.spec_of(name)
    if spec is None:
        return None
    return {"service_fields": list(spec.service_fields), "sends": list(spec.sends)}


def _result_type_out(name: str) -> dict[str, Any] | None:
    spec = requests.result_spec(name)
    if spec is None:
        return None
    return {
        "of": spec.of,
        "released_field": spec.released_field,
        "released_states": list(spec.released_states),
    }


def _type_amount(type_name: str) -> dict[str, Any] | None:
    from iki_server.amounts import services as amounts

    wanted = amounts.spec(type_name)
    if wanted is None:
        return None
    return {
        "unit": wanted.unit,
        "label": wanted.label,
        "description": wanted.description,
        "display_unit": wanted.display_unit,
        "concentration": wanted.concentration,
        "aliquots": wanted.aliquots or type_name,
        "aliquots_of": wanted.aliquots_of,
        "material": wanted.material,
    }


def _state_out(state: State) -> dict[str, Any]:
    return {
        "name": state.name,
        "label": state.label,
        "description": state.description,
        "set_by": list(state.set_by),
        "locks": state.locks,
        "on_edit": state.on_edit,
        "holds_amount": state.holds_amount,
        "reason": state.reason,
        "guards": [
            {
                "name": g.name,
                "description": g.description,
                "check": g.check,
                "when": g.when,
                "message": g.message,
            }
            for g in state.guards.values()
        ],
        "signatures": [_wanted_out(state, w) for w in state.signatures],
    }


def _wanted_out(state: State, wanted: Any) -> dict[str, Any]:  # noqa: ANN401 - a spec Signature
    meaning = SIGNATURE_MEANINGS[wanted.meaning]
    return {
        "meaning": wanted.meaning,
        "meaning_label": meaning.label,
        "verb": meaning.verb,
        "label": wanted.label,
        "statement": wanted.statement,
        "roles": [wanted.role] if wanted.role else list(state.set_by),
    }


def _input_units(unit: str | None, display_unit: str | None) -> list[dict[str, str]]:
    if unit is None:
        return []
    """The display unit first (what people read values in), then the field's own."""
    codes = list(units.common_alternatives(unit))
    if display_unit:
        codes = [display_unit, *(c for c in codes if c != display_unit)]
    return [{"code": c, "symbol": units.symbol(c)} for c in codes]


def _display(unit: str | None, display_unit: str | None) -> dict[str, float | None]:
    """How to show a quantity in its display unit, when that's a scale and an offset.

    Pages convert without a units table of their own. Units that don't convert by a scale
    and an offset (decibels, a concentration as pH, degrees of slope) are shown in the
    field's own unit: which do is the units' definitions' to say (`units.scale_and_offset`),
    not a few sampled values', which a logarithm can lie close to a line at.
    """
    none: dict[str, float | None] = {"display_scale": None, "display_offset": None}
    if unit is None or display_unit is None or display_unit == unit:
        return none
    found = units.scale_and_offset(unit, display_unit)
    if found is None:
        return none
    return {"display_scale": found[0], "display_offset": found[1]}


@router.get(
    "/entity-types",
    response=list[EntityTypeOut],
    summary="The installed entity types",
    by_alias=True,
)
def list_types(request: HttpRequest) -> list[dict[str, Any]]:
    """Every type the deployment's plugins define, abstract ones included, by name."""
    caller(request)
    return [_type_out(t) for _, t in sorted(_types().items())]


@router.get(
    "/entity-types/{qualified:name}",
    response=EntityTypeOut,
    summary="An entity type",
    by_alias=True,
)
def get_type(request: HttpRequest, name: str) -> dict[str, Any]:
    caller(request)
    found = _types().get(name)
    if found is None:
        raise Problem(404, f"There’s no entity type {name}.")
    return _type_out(found)


# --- Reading -------------------------------------------------------------------------------------


def _find(user: User, ref: str, project_id: UUID | None = None) -> Entity:
    """The entity, by ID or HRID, if the user can read it. Otherwise 404.

    A registry entry's ID or HRID (ADR 0115) names a project's record of it: the one the
    user can read, or ``project_id``'s. When they can read several, it answers 409."""
    readable = access.visible(user).select_related("created_by")
    try:
        key = UUID(ref)
    except ValueError:
        key = None
    if key is not None:
        found = readable.filter(pk=key).first()
        if found is not None:
            return found
        candidates = readable.filter(entry_id=key)
    else:
        candidates = readable.filter(hrid=ref.upper())
    if project_id is not None:
        candidates = candidates.filter(links__project_id=project_id)
    records = list(candidates.select_related("owner_project").order_by("created_at", "pk")[:21])
    if project_id is not None:
        own = [r for r in records if r.owner_project_id == project_id]
        records = own or records
    if len(records) == 1:
        return records[0]
    if not records:
        _gone(user, ref)
        raise Problem(404, "No such entity, or you can’t read it.")
    raise _choose_project(user, ref, records)


def _choose_project(user: User, ref: str, records: list[Entity]) -> Problem:
    """Several of the user's projects have a record of the entry: which is meant."""
    readable = {p.pk: p for p in access.readable_projects(user)}
    choices = []
    for record in records:
        owner = readable.get(record.owner_project_id) if record.owner_project_id else None
        if owner is not None:
            choices.append(
                {
                    "entity": str(record.pk),
                    "project": {"id": str(owner.pk), "name": owner.name, "slug": owner.slug},
                }
            )
    return Problem(
        409,
        f"{records[0].hrid} is in several projects you can read: say which, with ?project=, "
        "or name the record by its ID.",
        code="choose_project",
        extensions={"records": choices},
    )


def _gone(user: User, ref: str) -> None:
    """Answers 410 for a purged entity the user could read (ADR 0092): its tombstone."""
    gone = access.tombstones(user).select_related("tombstone__purge")
    try:
        found = gone.filter(pk=UUID(ref)).first()
    except ValueError:
        found = gone.filter(hrid=ref.upper()).first()
    if found is None:
        return
    stone = getattr(found, "tombstone", None)
    plugin = stone.purge.plugin if stone is not None else found.type.partition(".")[0]
    assert found.purged_at is not None  # noqa: S101 - a tombstone has it
    raise Problem(
        410,
        f"{found.hrid} was purged on {found.purged_at:%Y-%m-%d}, with the rest of the "
        f"{plugin} plugin’s data. Its ID, HRID and type are kept, so what refers to it "
        "still does.",
        code="purged",
        extensions={
            "entity": {
                "id": str(found.pk),
                "hrid": found.hrid,
                "type": found.type,
                "purged_at": found.purged_at.isoformat(),
                "content_sha256": stone.content_sha256 if stone is not None else None,
            }
        },
    )


def _out(user: User, entities: list[Entity]) -> list[dict[str, Any]]:
    values = services.current_values_of(entities)
    projects = {
        p.pk: p
        for p in permissions.visible_projects(user).filter(
            pk__in={e.owner_project_id for e in entities}
        )
    }
    editable = {p.pk for p in projects.values() if permissions.can(user, Action.PROJECT_EDIT, p)}
    asked = requests.summaries(user, entities)
    refs, files = _referenced(user, entities, values)
    frozen = {t for t in {e.type for e in entities} if services.frozen(shape(t)) is not None}
    entries = _entries_of(user, entities)
    named, stale, take = _people_of(user, entities, values)
    readers = {p.pk for p in access.readable_projects(user).filter(pk__in=projects)}
    signing = _Signing(user, entities)
    held, breaches = _holds_of(user, entities)
    belongs = _projects_of(user, entities)
    from iki_server.amounts import services as amounts

    amounts_held = amounts.amounts_of(entities)
    restricted_projects = {
        e.owner_project_id for e in entities if eligibility.purpose_roles(e.type)
    }
    permissions.remember_roles(user, [p for p in projects.values() if p.pk in restricted_projects])
    out = []
    for entity in entities:
        fields = shape(entity.type).fields()
        owner = (
            projects.get(entity.owner_project_id) if entity.owner_project_id is not None else None
        )
        editable_record = (
            bool(asked[entity.pk]["yours"])
            if entity.pk in asked
            else entity.owner_project_id in editable
        ) and entity.type not in frozen
        edit_amount = (
            editable_record and entity.archived_at is None and amounts.spec(entity.type) is not None
        )
        transitions = states.options(user, entity, values[entity.pk])
        mine = _ids_in(fields, values[entity.pk])
        shown = {name: to_json(fields[name], value) for name, value in values[entity.pk].items()}
        out.append(
            {
                "id": entity.pk,
                "hrid": entity.hrid,
                "title": _title(entity, shown),
                "type": entity.type,
                "revision": entity.revision,
                "owner_project": owner,
                "projects": belongs.get(entity.pk, []),
                "created_by": entity.created_by,
                "created_at": entity.created_at,
                "updated_at": entity.updated_at,
                "archived": entity.archived_at is not None,
                "archived_at": entity.archived_at,
                "locked": entity.locked,
                "locked_by": [n for n in fields if n in entity.locked_by],
                "values": shown,
                "refs": refs_in(refs, mine["ref"], entity.owner_project_id),
                "files": {str(i): files[i] for i in mine["file"] if i in files},
                "people": {str(i): named[i] for i in mine["person"] if i in named},
                "needs_reassigning": [
                    {"field": s.field, "person": s.person, "why": s.why}
                    for s in stale.get(entity.pk, [])
                ]
                if entity.owner_project_id in readers
                else [],
                "entry": entries.get(entity.entry_id) if entity.entry_id is not None else None,
                "holds": held.get(entity.pk, []),
                "breaches": breaches.get(entity.pk, []),
                "request": asked.get(entity.pk),
                "amount": _amount(entity, amounts_held.get(entity.pk), values[entity.pk]),
                "draw_refusals": eligibility.refusals(
                    entity, DrawPurpose.values, values[entity.pk]
                ),
                "can": {
                    "edit": editable_record and not entity.locked,
                    "correct": editable_record
                    and _can_correct(entity, values[entity.pk], transitions),
                    "edit_amount": edit_amount,
                    "draw_purposes": [
                        purpose
                        for purpose in DrawPurpose
                        if eligibility.purpose_refusal(user, entity.type, owner, purpose) is None
                    ]
                    if edit_amount
                    else [],
                    "states": _states_can(transitions),
                    "sign": _sign_can(user, entity, values[entity.pk], signing),
                    "take": take.get(entity.pk, []) if entity.type not in frozen else [],
                },
            }
        )
    return out


def _projects_of(user: User, entities: list[Entity]) -> dict[Any, list[Any]]:
    """The projects each record belongs to that the user can read, as ``ProjectRef``: its
    owner first, then by name. Two queries, however many records."""
    ids = [e.pk for e in entities if e.registry_id is None]
    if not ids:
        return {}
    links = ProjectLink.objects.filter(entity_id__in=ids)
    readable = {
        p.pk: p for p in access.readable_projects(user).filter(pk__in=links.values("project_id"))
    }
    owners = {e.pk: e.owner_project_id for e in entities}
    out: dict[Any, list[Any]] = defaultdict(list)
    for entity_id, project_id in links.filter(project_id__in=list(readable)).values_list(
        "entity_id", "project_id"
    ):
        out[entity_id].append(readable[project_id])
    for entity_id, found in out.items():
        found.sort(key=lambda p: (p.pk != owners.get(entity_id), p.name.lower(), str(p.pk)))
    return out


def _holds_of(
    user: User, entities: list[Entity]
) -> tuple[dict[Any, list[dict[str, Any]]], dict[Any, list[dict[str, Any]]]]:
    """The open holds and the breaches of these records (ADR 0127), as ``HoldOut`` and
    ``BreachOut``, by record."""
    ids = [e.pk for e in entities if e.registry_id is None]
    by_id = {e.pk: e for e in entities}
    held: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    breaches: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    if not ids:
        return held, breaches
    rows = (
        RuleHold.objects.filter(entity_id__in=ids, reviewed_at__isnull=True)
        .select_related("held_by", "held_agent")
        .order_by("held_at")
    )
    for row in rows:
        entity = by_id[row.entity_id]
        refusal = holds.why_not_review(user, entity, row)
        held[row.entity_id].append(
            {
                "id": row.pk,
                "rule": row.rule,
                "label": row.label,
                "message": row.message,
                "reviewed_by": list(row.roles),
                "held_by": row.held_by,
                "agent": row.held_agent.client_name if row.held_agent is not None else None,
                "held_at": row.held_at,
                "reason": row.reason,
                "revision": row.revision,
                "last_revision": row.last_revision,
                "can_review": refusal is None,
                "why_not": refusal[0] if refusal is not None else None,
            }
        )
    for breach in RuleBreach.objects.filter(entity_id__in=ids).order_by("found_at", "rule"):
        breaches[breach.entity_id].append(
            {
                "rule": breach.rule,
                "label": breach.label,
                "message": breach.message,
                "found_at": breach.found_at,
            }
        )
    return held, breaches


def _can_correct(
    entity: Entity, values: dict[str, Any], options: dict[str, list[states.Option]]
) -> bool:
    if not entity.locked_by or entity.released_review_revision is not None:
        return False
    targets = states.on_edit(states.state_fields(entity.type), values)
    if set(entity.locked_by) != targets.keys():
        return False
    for index, (field, target) in enumerate(targets.items()):
        option = next((o for o in options[field] if o.state.name == target), None)
        # A later guard can become true when the preceding state returns. The save
        # checks it then. Role and access refusals still apply to every return.
        if option is None or (option.why_not is not None and (index == 0 or option.guard is None)):
            return False
    return True


def _states_can(options: dict[str, list[states.Option]]) -> dict[str, Any]:
    return {
        name: [
            {
                "name": o.state.name,
                "label": o.state.label,
                "locks": o.state.locks,
                "reason": o.state.reason,
                "allowed": o.why_not is None,
                "why_not": o.why_not,
                "signed": o.signed,
                "guard": o.guard,
            }
            for o in found
        ]
        for name, found in options.items()
    }


class _Signing:
    """What the records' signed states need, read once for a page of records: their
    signatures given, and why the caller can't sign at all, if they can't."""

    def __init__(self, user: User, entities: list[Entity]) -> None:
        self.user = user
        self.entities = entities
        self._given: dict[Any, list[Any]] | None = None
        self._cant: tuple[str | None] | None = None

    def given(self, entity: Entity) -> list[Any]:
        if self._given is None:
            self._given = sig_services.pending(self.entities)
        return self._given.get(entity.pk, [])

    def cant(self) -> str | None:
        if self._cant is None:
            self._cant = (sig_services.why_cant_sign(self.user),)
        return self._cant[0]


def _sign_can(
    user: User, entity: Entity, values: dict[str, Any], signing: _Signing
) -> list[dict[str, Any]]:
    out = []
    for change in sig_changes.signed(entity, values):
        slots = sig_services.slots(
            user, entity, change, given=signing.given(entity), cant=signing.cant(), values=values
        )
        allowed = any(s.why_not is None for s in slots)
        open_slots = [s for s in slots if s.signature is None]
        out.append(
            {
                "field": change.field,
                "field_label": change.field_label,
                "to": change.to,
                "to_label": change.to_label,
                "reason": change.reason,
                "allowed": allowed,
                "why_not": None
                if allowed
                else (open_slots[0].why_not if open_slots else "Its signatures are all given."),
                "signatures": [_slot_out(s) for s in slots],
            }
        )
    return out


def _slot_out(slot: Any) -> dict[str, Any]:  # noqa: ANN401 - a signatures.services.Slot
    meaning = SIGNATURE_MEANINGS[slot.wanted.meaning]
    given = slot.signature
    return {
        "index": slot.index,
        "meaning": slot.wanted.meaning,
        "meaning_label": meaning.label,
        "label": slot.wanted.label,
        "statement": slot.wanted.statement,
        "roles": list(slot.roles),
        "role_labels": [lab_roles.label(r) for r in slot.roles],
        "given": {
            "id": given.pk,
            "signer": given.signer,
            "signed_at": given.signed_at,
            "manifestation": sig_services.manifestation(sig_services.parsed(given)),
        }
        if given is not None
        else None,
        "allowed": slot.why_not is None,
        "why_not": slot.why_not if given is None else None,
        "lapsed": slot.lapsed,
    }


def _entries_of(user: User, entities: list[Entity]) -> dict[Any, dict[str, Any]]:
    """The registry entries these records are of (ADR 0115), as ``EntryRefOut``."""
    ids = {e.entry_id for e in entities if e.entry_id is not None}
    if not ids:
        return {}
    rows = list(Entity.objects.filter(pk__in=ids).select_related("registry"))
    replacements = {r.superseded_by_id for r in rows if r.superseded_by_id is not None}
    refs, _ = _lookup(user, {"ref": replacements, "file": set()}) if replacements else ({}, {})
    out = {}
    for row in rows:
        superseded = None
        if row.superseded_by_id is not None:
            from iki_server.entities.entries import supersession

            found = supersession(row)
            assert found is not None  # noqa: S101 - it was superseded
            superseded = {
                "by": refs.get(row.superseded_by_id)
                or {
                    "hrid": found.replacement.hrid,
                    "title": found.replacement.hrid,
                    "type": found.replacement.type,
                    "archived": False,
                    "purged": found.replacement.purged_at is not None,
                },
                "by_id": row.superseded_by_id,
                "at": found.at,
                "reason": found.reason,
            }
        out[row.pk] = {
            "id": row.pk,
            "hrid": row.hrid,
            "registry": row.registry,
            "revision": row.revision,
            "superseded": superseded,
        }
    return out


def _people_of(
    user: User, entities: list[Entity], values: dict[Any, dict[str, Any]]
) -> tuple[dict[Any, dict[str, Any]], dict[Any, list[assignments.Stale]], dict[Any, list[str]]]:
    """The people a page of records names, as ``people`` has them; who of them needs
    reassigning; and the fields ``user`` may take each record in (ADR 0122)."""
    named = _named(user, entities, values)
    stale = assignments.stale_of(entities, values)
    return named, stale, assignments.takeable(user, entities, values, stale)


def _named(
    user: User, entities: list[Entity], values: dict[Any, dict[str, Any]]
) -> dict[Any, dict[str, Any]]:
    """The people a page of records names, as ``people`` has them."""
    where: dict[Any, set[Any]] = {}
    for entity in entities:
        for person in _ids_in(shape(entity.type).fields(), values[entity.pk])["person"]:
            where.setdefault(person, set()).add(entity.owner_project_id)
    found = people.shown(user, where, where)
    return {
        pk: {"id": p.id, "name": p.name, "email": p.email, "active": p.active}
        for pk, p in found.items()
    }


def _ids_in(fields: dict[str, Any], values: dict[str, Any]) -> dict[str, set[Any]]:
    """The entity, file and person IDs these values refer to."""
    found: dict[str, set[Any]] = {"ref": set(), "file": set(), "person": set()}
    for name, value in values.items():
        f = fields[name]
        if f.is_rows and f.items is not None and value:
            # A list of rows' references, files and people (ADR 0137).
            for cell_name, cell in f.items.fields.items():
                if cell.type in found:
                    found[cell.type].update(
                        r[cell_name] for r in value if r.get(cell_name) is not None
                    )
            continue
        kind = f.items.type if f.type == "list" and f.items is not None else f.type
        if kind in found and value is not None:
            found[kind].update(value if isinstance(value, list) else [value])
    return found


def _referenced(
    user: User, entities: list[Entity], values: dict[Any, dict[str, Any]]
) -> tuple[dict[Any, dict[str, Any]], dict[Any, dict[str, Any]]]:
    """What a page of entities refers to that the user can read, in a few queries. A
    reference to a registry entry is also titled in each of their projects: see
    :func:`refs_in`."""
    wanted: dict[str, set[Any]] = {"ref": set(), "file": set()}
    for entity in entities:
        for kind, ids in _ids_in(shape(entity.type).fields(), values[entity.pk]).items():
            if kind in wanted:
                wanted[kind] |= ids
    return _lookup(user, wanted, projects={e.owner_project_id for e in entities})


def refs_in(
    refs: dict[Any, dict[str, Any]],
    ids: Iterable[Any],
    project: Any,  # noqa: ANN401 - a UUID
) -> dict[str, dict[str, Any]]:
    """A referring record's ``refs``, for its ``project``: an entry it refers to is titled
    by that project's record of it, where it has one, as its link opens that record."""
    return {str(i): refs.get((i, project), refs[i]) for i in ids if i in refs}


def _lookup(
    user: User, wanted: dict[str, set[Any]], *, projects: Iterable[Any] = ()
) -> tuple[dict[Any, dict[str, Any]], dict[Any, dict[str, Any]]]:
    """The entities and files with these IDs that the user can read, as ``refs`` and
    ``files``. A reference to an identity names its registry entry (ADR 0115): it's titled
    by a record of it the user can read, the oldest; and for each of ``projects`` that has
    a record of it, by that one too, keyed by the entry's ID and the project's, for
    :func:`refs_in`."""
    found = list(access.visible(user).filter(pk__in=wanted["ref"])) if wanted["ref"] else []
    entries = (
        list(access.visible_entries(user).filter(pk__in=wanted["ref"] - {e.pk for e in found}))
        if wanted["ref"]
        else []
    )
    preferred = {p for p in projects if p is not None}
    record_of: dict[Any, Entity] = {}
    record_in: dict[tuple[Any, Any], Entity] = {}
    if entries:
        for record in (
            access.visible(user)
            .filter(entry__in=[e.pk for e in entries])
            .order_by("-created_at", "-pk")
        ):
            record_of[record.entry_id] = record
            if record.owner_project_id in preferred:
                record_in[(record.entry_id, record.owner_project_id)] = record
    titled = [
        e
        for e in {r.pk: r for r in [*found, *record_of.values(), *record_in.values()]}.values()
        if _template(e.type) is not None
    ]
    titled_values = services.current_values_of(titled) if titled else {}

    def ref(e: Entity, shown_by: Entity) -> dict[str, Any]:
        fields = shape(e.type).fields()
        shown = {n: to_json(fields[n], v) for n, v in titled_values.get(shown_by.pk, {}).items()}
        return {
            "hrid": e.hrid,
            "title": _title(shown_by, shown),
            "type": e.type,
            "archived": shown_by.archived_at is not None and shown_by is not e,
            "purged": False,
        }

    refs: dict[Any, dict[str, Any]] = {}
    for e in [*found, *entries]:
        refs[e.pk] = ref(e, record_of.get(e.pk, e))
    by_pk = {e.pk: e for e in entries}
    for (entry, holder), record in record_in.items():
        if record is not record_of.get(entry):
            refs[(entry, holder)] = ref(by_pk[entry], record)
    formerly = _formerly({k for k in refs if not isinstance(k, tuple)})
    for key, r in refs.items():
        hrids = formerly.get(key[0] if isinstance(key, tuple) else key)
        if hrids:
            r["formerly"] = hrids
    missing = wanted["ref"] - set(refs)
    if missing:
        # What a purge left (ADR 0092): the reference still names it, as purged.
        for e in access.tombstones(user).filter(pk__in=missing):
            refs[e.pk] = {
                "hrid": e.hrid,
                "title": e.hrid,
                "type": e.type,
                "archived": e.archived_at is not None,
                "purged": True,
            }
    files = (
        {
            pk: {"name": name, "media_type": media_type, "size": size}
            for pk, name, media_type, size in access.readable_files(user)
            .filter(pk__in=wanted["file"])
            .values_list("pk", "name", "media_type", "blob__size")
        }
        if wanted["file"]
        else {}
    )
    return refs, files


def _formerly(ids: set[Any]) -> dict[Any, list[str]]:
    """The HRIDs each had where it was imported from, when it got a new one here."""
    if not ids:
        return {}
    from iki_server.bundles.models import ImportEntity, ImportState

    out: dict[Any, set[str]] = {}
    applied = ImportEntity.objects.filter(bundle_import__state=ImportState.APPLIED).exclude(
        hrid=F("hrid_here")
    )
    for pk, hrid in applied.filter(entity_id__in=ids).values_list("entity_id", "hrid"):
        out.setdefault(pk, set()).add(hrid)
    # A registry entry (ADR 0115) had the HRID its imported records had.
    for entry, hrid in applied.filter(entry_id__in=ids).values_list("entry_id", "hrid"):
        out.setdefault(entry, set()).add(hrid)
    return {pk: sorted(hrids) for pk, hrids in out.items()}


def _template(type_name: str) -> str | None:
    entity_type = registry.types().get(type_name)
    return registry.display_title(entity_type) if entity_type is not None else None


def _title(entity: Entity, values: dict[str, Any]) -> str:
    """Its display title, from its values as JSON."""
    return titles.render(_template(entity.type), values, entity.hrid, shape(entity.type).fields())


def _etag(entity: Entity) -> str:
    return f'"{entity.revision}"'


def _plain(value: Decimal) -> str:
    from iki_server.amounts.services import shown

    return shown(value)


def _amount(entity: Entity, amount: Any, values: dict[str, Any]) -> dict[str, Any] | None:  # noqa: ANN401 - an Amount
    """A record's amount (ADR 0139), as ``AmountOut``."""
    from iki_server.amounts import services as amounts

    wanted = amounts.spec(entity.type)
    if wanted is None or amount is None:
        return None
    return {
        "value": _plain(amount.value),
        "unit": amount.unit,
        "label": wanted.label,
        "display": amounts.displayed(wanted, amount.value),
        "held_by": amounts.held(entity, values),
        "updated_at": amount.updated_at,
    }


def _one(user: User, entity: Entity, response: HttpResponse) -> dict[str, Any]:
    response["ETag"] = _etag(entity)
    return _out(user, [entity])[0]


@router.get("/entities", response=Page[EntityOut], summary="Entities you can read")
def list_entities(request: HttpRequest, filters: Query[EntityFilters]) -> dict[str, Any]:
    """Oldest first. Filter by type to get one type and its subtypes. For conditions on
    fields, and sorting, use ``POST /entities/search``."""
    me = caller(request)
    if filters.project:
        project(me, filters.project)
    wanted = search.Search(
        type=filters.type,
        types=tuple(filters.types or ()),
        exact_type=filters.exact_type,
        project=filters.project,
        archived=filters.archived,
        q=filters.q,
        collection=filters.collection,
        run=filters.run,
    )
    with _searching(parameters=True):
        result = page(search.queryset(me, wanted), filters)
        result["items"] = _out(me, result["items"])
    return result


@router.post("/entities/search", response=Page[EntityOut], summary="Search entities")
def search_entities(request: HttpRequest, payload: SearchIn) -> dict[str, Any]:
    """Entities you can read that match every filter given: a type (with its subtypes), a
    project, archived or not, text in their HRID or title (``q``), membership of a
    ``collection``, and ``where``, conditions on the type's fields. Sorted by ``sort``
    (then by ID), oldest first without one.

    Each kind of field takes its own operators, and every field takes ``is_set``:

    - string: ``eq``, ``ne``, ``in``, ``contains``, ``starts_with`` (any case); a sequence
      (a ``dna_sequence``, ``rna_sequence`` or ``protein_sequence``, its field's ``format``)
      also ``motif`` (ADR 0175): its bases or residues in IUPAC codes, as ``GAATTY``, each
      matching a residue it certainly is (Y matches C, T and Y; a record's N only N), found
      on a duplex's other strand as its reverse complement, and across a circle's origin, as
      the record's strands and topology say
    - text: ``contains``, ``matches`` (its words: ``"kinase -fragment"``, ``"\"active site\""``)
    - enum, reference: ``eq``, ``ne``, ``in`` (references by ID or HRID)
    - int, float, quantity, date, datetime: ``eq``, ``ne``, ``in``, ``lt``, ``lte``, ``gt``,
      ``gte`` (quantities in the field's unit, or in another by its UCUM code, as
      ``{"value": 50, "unit": "ku"}`` for 50 kDa)
    - bool: ``eq``; file: ``eq``, ``ne``; list: ``has``

    ``ne`` matches entities without a value too. Every entity also has ``hrid``, ``type``,
    ``owner_project``, ``created_by``, ``created_at``, ``updated_at`` and ``archived_at``,
    and ``made``, how it was made (ADR 0106): ``has`` an object of ``software``,
    ``version``, ``kind`` (``experimental``, ``computational``) and ``by`` (``iki``, Iki's
    record of its run; ``source``, an imported record's word), all matching one making,
    as ``{"software": "AlphaFold", "by": "iki"}``; ``is_set``, made in any way it records.
    A state field (ADR 0125) is searched as an enum of its states, by name.
    ``upstream`` and ``downstream`` (ADR 0123) take ``of``: what an entity (by ID or
    HRID) was derived from, or what was derived from it, through references and runs, as
    ``{"field": "downstream", "op": "of", "value": "DNR-000001"}``, or with a ``depth``
    and ``edges``: ``{"entity": "DNR-000001", "depth": 5, "edges": ["reference"]}``; and
    ``is_set``, whether anything you can read is that way.
    ``held`` and ``breaks`` (ADR 0127) find what plugins' rules caught: records a rule holds
    for review now, and records that break a rule they were saved before (one its plugin
    added or changed since, or one a bundle brought). They take a rule's name with ``eq``,
    ``ne`` and ``in``, as ``{"field": "held", "op": "eq", "value": "purity_in_specification"}``,
    and ``is_set``, held or breaking any rule. Each result's ``holds`` and ``breaches``
    say which, and why.
    A search may run for 15 seconds: conditions on indexed fields keep it fast.
    """
    me = caller(request)
    if payload.project:
        project(me, payload.project)
    wanted = search.Search(
        type=payload.type,
        exact_type=payload.exact_type,
        project=payload.project,
        archived=payload.archived,
        q=payload.q,
        collection=payload.collection,
        run=payload.run,
        where=payload.where.model_dump(by_alias=True, exclude_unset=True)
        if payload.where
        else None,
        sort=payload.sort.field if payload.sort else None,
        descending=payload.sort.descending if payload.sort else False,
    )
    with _searching():
        rows, cursor = search.page(
            search.queryset(me, wanted), wanted, payload.cursor, payload.limit
        )
        return {"items": _out(me, rows), "next_cursor": cursor}


#: How many records a search's download reads at a time: one keyset page (ADR 0175).
EXPORT_PAGE = 500


class SearchCsvParams(Schema):
    type: str | None = Field(None, description="This type and its subtypes.")
    exact_type: bool = Field(False, description="Only the type itself, not its subtypes.")
    project: UUID | None = Field(None, description="Entities that belong to this project.")
    archived: bool | None = None
    q: str | None = Field(
        None, max_length=200, description="Text in their HRID, or a text field in their title."
    )
    collection: str | None = Field(
        None, max_length=100, description="Members of this collection: its ID or HRID."
    )
    run: str | None = Field(
        None, max_length=100, description="What this run wrote: its ID or HRID."
    )
    where: str | None = Field(
        None,
        max_length=20_000,
        description="Conditions on the type's fields, as JSON: what `POST /entities/search` "
        "takes as `where`.",
    )
    sort: str | None = Field(None, max_length=100, description="A field to sort by.")
    descending: bool = False
    columns: list[str] = Field(
        default_factory=list,
        max_length=200,
        description="The columns, in order, by name: the type's fields, and `hrid`, `type`, "
        "`title`, `project`, `updated_at`, `revision` and `id` (and a request's `asked_by` "
        "and `sent_to`). The grid's, without them.",
    )


_CSV_OUT = {"responses": {200: {"description": "As CSV", "content": {"text/csv": {}}}}}


@router.get("/entities/search.csv", summary="Download a search's results", openapi_extra=_CSV_OUT)
def search_csv(request: HttpRequest, params: Query[SearchCsvParams]) -> StreamingHttpResponse:
    """Every entity you can read that the search finds, as CSV: what ``POST
    /entities/search`` finds with the same filters, in its order, with the grid's columns
    (ADR 0175). Values are as the grid shows them, as you can read them: a reference by
    its HRID, a person by name, a state by its label; numbers in full, a quantity's unit
    in its column's heading, times in ISO 8601 (UTC). Each row ends with when the record
    last changed, its revision and its UUID.

    It's written a page at a time as it's read, so a large result starts at once. Each
    page may run for 15 seconds; one that runs out ends the file with a row saying so. No
    cell starts as a formula would (OWASP's CSV injection guidance): one that would starts
    with a quote. Each download is in the audit log (``search.exported``) before its
    first row is sent."""
    me = caller(request)
    place = project(me, params.project) if params.project else None
    where = _where_of(params.where)
    wanted = search.Search(
        type=params.type,
        exact_type=params.exact_type,
        project=params.project,
        archived=params.archived,
        q=params.q,
        collection=params.collection,
        run=params.run,
        where=where,
        sort=params.sort,
        descending=params.descending,
    )
    entity_type = registry.types().get(params.type) if params.type else None
    typed = entity_type if entity_type is not None and params.type != "iki/core.Entity" else None
    with _searching(parameters=True):
        found = search.queryset(me, wanted)
        try:
            table = search_exports.columns(
                _type_out(typed) if typed is not None else None,
                params.columns,
                projects=place is None,
            )
        except search_exports.NoColumn as error:
            raise search.SearchError([(f"#/columns/{error.index}", str(error))]) from error
        rows, cursor = search.page(found, wanted, None, EXPORT_PAGE)
        first = _table_rows(me, rows)
    name = _download_name(place, typed)
    with transaction.atomic():
        audit.record(
            "search.exported",
            actor=me,
            project=place,
            data={
                "format": "csv",
                "file": name,
                "search": {
                    "type": wanted.type,
                    "exact_type": wanted.exact_type,
                    "project": str(wanted.project) if wanted.project else None,
                    "archived": wanted.archived,
                    "q": wanted.q,
                    "collection": wanted.collection,
                    "where": where,
                    "sort": wanted.sort,
                    "descending": wanted.descending,
                },
                "columns": [c.key for c in table],
            },
        )

    def chunks() -> Iterator[str]:
        nonlocal cursor
        yield from csvs.lines([[c.heading for c in table]])
        yield "".join(csvs.lines([c.cell(r) for c in table] for r in first))
        while cursor is not None:
            try:
                with search.time_limit():
                    more, cursor = search.page(found, wanted, cursor, EXPORT_PAGE)
                    got = _table_rows(me, more)
            except search.TooSlow as error:
                stopped = error.detail or "a page of it took too long to read."
                yield from csvs.lines([[f"The download stopped here: {stopped}"]])
                return
            yield "".join(csvs.lines([c.cell(r) for c in table] for r in got))

    return csvs.streamed(request, chunks(), name)


def _where_of(text: str | None) -> dict[str, Any] | None:
    """A download's ``where``, as JSON, as a search takes it."""
    if text is None or not text.strip():
        return None
    try:
        data = json.loads(text)
        condition = Condition.model_validate(data)
    except ValueError as error:
        errors = (
            [
                {"parameter": "/".join(["where", *map(str, e["loc"])]), "detail": e["msg"]}
                for e in error.errors()
            ]
            if isinstance(error, ValidationError)
            else [{"parameter": "where", "detail": "must be a condition, as JSON."}]
        )
        raise Problem(
            422, "Some fields are invalid.", code="validation_failed", errors=errors
        ) from error
    dumped: dict[str, Any] = condition.model_dump(by_alias=True, exclude_unset=True)
    return dumped


#: What a download's name may not hold: path separators, and what some systems refuse.
_NOT_IN_A_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _download_name(place: Any, entity_type: EntityType | None) -> str:  # noqa: ANN401 - a Project
    """``Kinase Atlas Proteins.csv``: the project's name, then what it lists."""
    what = (
        (entity_type.label_plural or f"{entity_type.label or entity_type.name}s")
        if entity_type is not None
        else "Entities"
    )
    words = f"{place.name} {what}" if place is not None else what
    return " ".join(_NOT_IN_A_NAME.sub("_", words).split())[:150] + ".csv"


def _table_rows(user: User, entities: list[Entity]) -> list[dict[str, Any]]:
    """A page of records as a search's download reads them (``search_exports.Row``): their
    values, what those refer to as the reader can read it, and their own columns."""
    values = services.current_values_of(entities)
    refs, files = _referenced(user, entities, values)
    named = _named(user, entities, values)
    asked = requests.summaries(user, entities)
    belongs = _projects_of(user, entities)
    types = registry.types()
    out = []
    for entity in entities:
        fields = shape(entity.type).fields()
        mine = _ids_in(fields, values[entity.pk])
        shown = {n: to_json(fields[n], v) for n, v in values[entity.pk].items()}
        kind = types.get(entity.type)
        out.append(
            {
                "id": entity.pk,
                "hrid": entity.hrid,
                "type_label": (kind.label or kind.name) if kind is not None else entity.type,
                "title": _title(entity, shown),
                # Each project it's in that the reader can read, as the grid's column (ADR 0176).
                "project": "; ".join(p.name for p in belongs.get(entity.pk, [])),
                "updated_at": entity.updated_at,
                "revision": entity.revision,
                "values": shown,
                "refs": refs_in(refs, mine["ref"], entity.owner_project_id),
                "files": {str(i): files[i] for i in mine["file"] if i in files},
                "people": {str(i): named[i] for i in mine["person"] if i in named},
                "request": asked.get(entity.pk),
            }
        )
    return out


@contextmanager
def _searching(*, parameters: bool = False) -> Iterator[None]:
    """Runs a search with its time limit, answering for its mistakes."""
    try:
        with search.time_limit():
            yield
    except search.SearchError as error:
        raise Problem(
            422,
            "Some fields are invalid." if parameters else "This search is invalid.",
            code="validation_failed",
            errors=[
                {"parameter": pointer.removeprefix("#/"), "detail": detail}
                if parameters
                else {"pointer": pointer, "detail": detail}
                for pointer, detail in error.errors
            ],
        ) from error
    except search.TooSlow as error:
        raise Problem(
            422,
            error.detail
            or "This search took too long. Narrow it with conditions on indexed fields, or "
            "sort by an indexed field.",
            code="search_too_slow",
        ) from error


#: More entities than one value of a unique field has across the projects a person reads.
LOOKUP_LIMIT = 100


class LookupParams(Schema):
    type: str = Field(description="The entity type, such as `iki/protein.Protein`; subtypes count.")
    field: str = Field(description="One of the type's unique fields, such as `lims_id`.")
    value: str = Field(min_length=1, max_length=600)
    project: UUID | None = Field(None, description="Only entities that belong to this project.")


@router.get("/entities/lookup", response=list[EntityOut], summary="Entities by an external ID")
def lookup_entities(request: HttpRequest, params: Query[LookupParams]) -> list[dict[str, Any]]:
    """The entities you can read of ``type`` (or a subtype) whose unique ``field`` is
    ``value``, such as the record's ID in a LIMS (ADR 0082). Each project has at most one of
    its own, so with ``project`` you get its own and any linked into it. Oldest first."""
    me = caller(request)
    type_name, field, project_id = params.type, params.field, params.project
    if type_name not in _types():
        raise Problem(404, f"There’s no entity type {type_name}.")
    form = shape(type_name)
    spec = form.fields().get(field)
    if spec is None or not spec.unique:
        raise Problem(
            422,
            f"`{field}` isn’t a unique field of {type_name}.",
            code="validation_failed",
            errors=[{"parameter": "field", "detail": "isn't a unique field of the type."}],
        )
    if project_id is not None:
        project(me, project_id)
    rows = unique.holders(unique.key(form.part_of(field).model, field), params.value)
    # A unique registry fact is its entries' (ADR 0115): their records are what's found.
    entities = access.visible(me).filter(
        Q(pk__in=rows) | Q(entry__in=rows), type__in=subtypes(type_name)
    )
    if project_id is not None:
        entities = entities.filter(links__project_id=project_id)
    picked = list(entities.select_related("created_by").order_by("created_at", "pk")[:LOOKUP_LIMIT])
    return _out(me, picked)


class TypeCountOut(Schema):
    type: str = Field(description="The type, qualified, such as `iki/dna.Plasmid`.")
    label: str = Field(description="What the type calls one of its records, such as Plasmid.")
    count: int | None = Field(
        description="How many current records of it you can read. Null when counting would "
        "be slow: then every type that can have records is listed, uncounted."
    )


class CountsOut(Schema):
    types: list[TypeCountOut] = Field(
        description="Each type you can read current records of, by label: the type itself, "
        "not its parents, which count nothing of their own."
    )
    total: int | None = Field(description="Every current record you can read. Null as `count`.")


@router.get("/entities/counts", response=CountsOut, summary="How many records you can read")
def count_entities(request: HttpRequest, project: UUID | None = None) -> dict[str, Any]:
    """How many current records of each type you can read, by the rules that read one: a
    record in a project you aren't in counts for nothing (ADR 0176). With ``project``, only
    those in it, its own and those linked into it.

    It's one grouped query. When Postgres expects it to read more than a million records,
    nothing is counted, and every type that can have records is listed with a null count:
    a count is left out rather than made slow, or estimated."""
    me = caller(request)
    if project is not None:
        common.project(me, project)
    found = counts.by_type(me, project)
    types = _types()
    if found is None:
        listed = sorted(
            ((n, t) for n, t in types.items() if not t.abstract),
            key=lambda pair: ((pair[1].label or pair[1].name).lower(), pair[0]),
        )
        return {
            "types": [{"type": n, "label": t.label or t.name, "count": None} for n, t in listed],
            "total": None,
        }
    rows = [
        {
            "type": name,
            "label": (types[name].label or types[name].name) if name in types else name,
            "count": n,
        }
        for name, n in found.items()
    ]
    return {
        "types": sorted(rows, key=lambda r: (r["label"].lower(), r["type"])),
        "total": sum(found.values()),
    }


@router.get(
    "/entities/{ref}",
    response={200: EntityOut, 304: None},
    summary="An entity, by ID or HRID",
)
def get_entity(
    request: HttpRequest,
    ref: str,
    response: HttpResponse,
    project: UUID | None = None,
    if_none_match: HeaderEx[str | None, P(alias="If-None-Match")] = None,
) -> Any:  # noqa: ANN401 - the entity, or 304
    """Send ``If-None-Match`` with the ETag you have to get 304 if it hasn't changed.

    A registry entry's HRID or ID names a project's record of it (ADR 0115): give
    ``project`` to say whose, when you read more than one."""
    me = caller(request)
    entity = _find(me, ref, project)
    # The clock or a role grant can change draw or correction eligibility without a revision.
    if (
        if_none_match is not None
        and _etag(entity) in _etags(if_none_match)
        and not entity.locked
        and not eligibility.draw_guards(entity.type)
        and not eligibility.purpose_roles(entity.type)
    ):
        unchanged = HttpResponse(status=304)  # no body, as RFC 9110 says
        unchanged["ETag"] = _etag(entity)
        return unchanged
    return _one(me, entity, response)


def _etags(header: str) -> set[str]:
    return {tag.strip().removeprefix("W/") for tag in header.split(",")}


# --- Changing ------------------------------------------------------------------------------------


def _invalid(error: Invalid) -> Problem:
    """A 422 for invalid values: each field's problems, and each broken rule's (ADR 0127)
    on its own, naming the rule and whether a reason would save it anyway."""
    from iki_server.amounts.services import Wrong

    if isinstance(error, Wrong):
        # Where in the request (ADR 0139): its amount, or an aliquot's values.
        return Problem(
            422,
            "The amount can't change so."
            if "amount" in error.errors
            else "Some values are invalid.",
            errors=[
                {"pointer": f"#/{name}" if name else "#", "detail": " ".join(messages)}
                for name, messages in error.errors.items()
            ],
        )
    from_rules: dict[str, list[str]] = defaultdict(list)
    errors: list[dict[str, Any]] = []
    for b in getattr(error, "broken", ()):
        for name in b.fields or ("",):
            from_rules[name].append(b.message)
            errors.append(
                {
                    "pointer": f"#/values/{name}" if name else "#/values",
                    "detail": b.message,
                    "rule": b.rule.key,
                    "hold": b.held,
                }
            )
    plain = []
    for name, messages in error.errors.items():
        rest = list(messages)
        for message in from_rules.get(name, ()):
            if message in rest:
                rest.remove(message)
        if not rest:
            continue
        pointer = "#/type" if name == "type" else f"#/values/{name}" if name else "#/values"
        plain.append({"pointer": pointer, "detail": " ".join(rest)})
    if isinstance(error, checking.Held):
        return Problem(
            422,
            "These values break rules that hold records for review. To save them anyway, say "
            "why in `hold_reason`: the record is saved, and held until it's reviewed.",
            code="hold_reason_required",
            errors=plain + errors,
        )
    return Problem(422, "Some values are invalid.", errors=plain + errors)


@router.post(
    "/entities",
    response={201: CreatedOut, 200: CreatedOut},
    summary="Create an entity",
)
def create_entity(request: HttpRequest, payload: EntityIn, response: HttpResponse) -> Any:  # noqa: ANN401
    """For a type with an identity, this registers it in the project's registry (ADR
    0115): a new registry entry (201, ``registered``), or the one there is, whoever
    registered it (201), of which the project gets its own record. If the project has a
    record of it already, that's returned as it is (200, ``reused``). Values of the entry's
    that it has already are kept; a fact given with another value is refused."""
    me = caller(request)
    target = project(me, payload.project, Action.PROJECT_EDIT)
    try:
        saved = services.create(
            me,
            payload.type,
            target,
            payload.values,
            reason=payload.reason,
            hold_reason=payload.hold_reason or "",
            amount=payload.amount.model_dump(mode="json") if payload.amount else None,
        )
    except Invalid as error:
        raise _invalid(error) from None
    return _created(me, saved, response)


def _created(me: User, saved: services.Saved, response: HttpResponse) -> Any:  # noqa: ANN401
    entity = Entity.objects.select_related("created_by").get(pk=saved.entity.pk)
    out = {**_one(me, entity, response), "reused": saved.reused, "registered": saved.registered}
    response["Location"] = f"/api/v1/entities/{entity.pk}"
    return Status(200 if saved.reused else 201, out)


def _expected(if_match: str | None) -> int:
    if if_match is None:
        raise Problem(
            428,
            "Send If-Match with the ETag you read, so you can’t overwrite someone else’s change.",
            code="precondition_required",
        )
    tags = _etags(if_match)
    numbers = [int(t.strip('"')) for t in tags if t.strip('"').isdigit() and t.startswith('"')]
    if len(numbers) != 1 or len(tags) != 1:
        raise Problem(
            412,
            'If-Match must be the one ETag you read, such as "3".',
            code="precondition_failed",
        )
    return numbers[0]


@router.patch("/entities/{ref}", response=EntityOut, summary="Change, archive or restore")
def patch_entity(
    request: HttpRequest,
    ref: str,
    payload: EntityPatch,
    response: HttpResponse,
    if_match: HeaderEx[str | None, P(alias="If-Match")] = None,
) -> dict[str, Any]:
    """Send only the values to change and the ETag you read. If `can.correct` is true,
    changing a value requires a reason. Each locking state returns to its declared `on_edit`
    destination, and the return and edit revisions commit together (ADR 0179).
    To restore an archived entity and change it, send both: it's restored first."""
    me = caller(request)
    entity = _find(me, ref)
    owner = services.owner_of(entity)
    if payload.archived is not None or not requests.can_change(me, entity):
        # A request's service changes only its own fields (ADR 0132): archiving is the
        # asking project's.
        require(me, Action.PROJECT_EDIT, owner)
    expected = _expected(if_match)
    try:
        with transaction.atomic():
            # Locked for the whole request, so its steps can't interleave with another's.
            locked = Entity.objects.select_for_update().get(pk=entity.pk)
            if locked.revision != expected:
                raise services.Stale(locked.revision)
            if payload.archived is False:
                services.set_archived(me, locked, False, reason=payload.reason)
            if payload.values:
                services.update(
                    me,
                    locked,
                    payload.values,
                    expected_revision=_current(locked),
                    allow_correction=True,
                    reason=payload.reason,
                    hold_reason=payload.hold_reason or "",
                )
            if payload.archived is True:
                services.set_archived(me, locked, True, reason=payload.reason)
    except states.CorrectionReason as error:
        raise Problem(
            422,
            "Correcting this record needs a reason.",
            errors=[{"pointer": "#/reason", "detail": " ".join(error.errors["reason"])}],
        ) from None
    except states.GuardRefused as refused:
        raise Problem(
            409, str(refused), code="guard_refused", extensions={"guard": refused.guard}
        ) from None
    except PermissionDenied as denied:
        raise Problem(403, str(denied)) from None
    except services.Stale as stale:
        raise Problem(
            412,
            f"Someone changed it since you read it: it’s at revision {stale.current} now. "
            "Read it again, then make your change.",
            code="stale",
            headers={"ETag": f'"{stale.current}"'},
        ) from None
    except Invalid as error:
        raise _invalid(error) from None
    return _one(me, _find(me, str(entity.pk)), response)


def _current(entity: Entity) -> int:
    return Entity.objects.values_list("revision", flat=True).get(pk=entity.pk)


@router.post(
    "/entities/{ref}/states/{field}",
    response=EntityOut,
    summary="Set one of a record's state fields",
)
def set_entity_state(
    request: HttpRequest,
    ref: str,
    field: str,
    payload: StateIn,
    response: HttpResponse,
    project: UUID | None = None,
    if_match: HeaderEx[str | None, P(alias="If-Match")] = None,
) -> dict[str, Any]:
    """Sets one of the record's state fields (ADR 0125) to one of its states, from any other,
    as a revision of its own, with your reason. It needs one of the state's lab roles in the
    record's project (ADR 0116): `can.states` lists each state with whether you may and why
    not. A state that locks the record may declare a return on a reasoned edit (ADR 0179).
    State fields stay settable. A state that needs electronic signatures
    (ADR 0126) is set only by them, in the web app: this answers 409 `signatures_required`
    for it. Send If-Match with the ETag you read."""
    me = caller(request)
    entity = _find(me, ref, project)
    expected = _expected(if_match)
    try:
        states.set_state(
            me, entity, field, payload.state, expected_revision=expected, reason=payload.reason
        )
    except states.NeedsSignatures as signed:
        raise Problem(409, str(signed), code="signatures_required") from None
    except PermissionDenied as denied:
        raise Problem(403, str(denied)) from None
    except states.GuardRefused as refused:
        raise Problem(
            409, str(refused), code="guard_refused", extensions={"guard": refused.guard}
        ) from None
    except services.Stale as stale:
        raise Problem(
            412,
            f"Someone changed it since you read it: it’s at revision {stale.current} now. "
            "Read it again, then make your change.",
            code="stale",
            headers={"ETag": f'"{stale.current}"'},
        ) from None
    except Invalid as error:
        raise Problem(
            422,
            "Setting that state needs more.",
            errors=[
                {"pointer": f"#/{key}", "detail": " ".join(messages)}
                for key, messages in error.errors.items()
            ],
        ) from None
    return _one(me, _find(me, str(entity.pk)), response)


@router.post(
    "/entities/{ref}/holds/{hold_id}/review",
    response=EntityOut,
    summary="Review a hold",
)
def review_hold(
    request: HttpRequest,
    ref: str,
    hold_id: UUID,
    payload: HoldReviewIn,
    response: HttpResponse,
    project: UUID | None = None,
) -> dict[str, Any]:
    """Reviews a rule's hold on the record (ADR 0127), with why it may stand: the hold is
    cleared, and the audit log keeps who reviewed it, when and why. It takes a person, not
    an app or agent's token, with one of the rule's lab roles in the record's project, and
    not whoever saved what held it: `holds[].can_review` says whether you may. Editing the
    record back within the rule doesn't clear a hold; only a review does."""
    me = caller(request)
    entity = _find(me, ref, project)
    try:
        holds.review(me, entity, hold_id, payload.reason)
    except RuleHold.DoesNotExist:
        raise Problem(404, "It has no such hold.") from None
    except PermissionDenied as denied:
        raise Problem(403, str(denied)) from None
    except Invalid as error:
        raise Problem(
            422,
            "Reviewing it needs a reason.",
            errors=[
                {"pointer": f"#/{key}", "detail": " ".join(messages)}
                for key, messages in error.errors.items()
            ],
        ) from None
    return _one(me, _find(me, str(entity.pk)), response)


# --- History -------------------------------------------------------------------------------------


class TrailFilter(Schema):
    """What a record's audit trail is narrowed to (ADR 0176): the revisions a person made,
    or that set a field. Both together find those that did both."""

    person: UUID | None = Field(None, description="Only the revisions this person made.")
    field: str | None = Field(
        None, max_length=63, description="Only the revisions that set this field, by its name."
    )

    def applied(
        self, revisions: QuerySet[Revision], *, disclosed: Q | None = None
    ) -> QuerySet[Revision]:
        """``revisions``, narrowed. A person is matched only among those ``disclosed``
        says who made, so narrowing by them tells no more than reading each would."""
        if self.field:
            revisions = revisions.filter(changed_fields__contains=[self.field])
        if self.person:
            revisions = revisions.filter(user_id=self.person)
            if disclosed is not None:
                revisions = revisions.filter(disclosed)
        return revisions


@router.get(
    "/entities/{ref}/revisions",
    response=Page[RevisionOut],
    summary="An entity's revisions",
    by_alias=True,
)
def list_revisions(
    request: HttpRequest,
    ref: str,
    params: Query[PageParams],
    trail: Query[TrailFilter],
    project: UUID | None = None,
) -> dict[str, Any]:
    """Newest first: who changed what, how, when and why. ``project`` says whose record of a
    registry entry, as for reading it. ``person`` and ``field`` narrow it to the revisions
    a person made, or that set a field, as its audit trail's filters do (ADR 0176)."""
    me = caller(request)
    entity = _find(me, ref, project)
    revisions = trail.applied(Revision.objects.filter(entity=entity)).select_related(
        "user", "agent"
    )
    result = page(revisions, params, key="number", newest_first=True)
    made = _runs_of(me, result["items"])
    states = revision_states(result["items"])
    held = _holds_opened(entity, result["items"])
    result["items"] = [
        {**_revision(r, made, states=states), "holds": held.get(r.number, [])}
        for r in result["items"]
    ]
    return result


class ChangedOut(Schema):
    id: UUID
    hrid: str
    title: str = Field(description="What it's called: its type's display title, or its HRID.")
    type: str
    archived: bool


class ProjectChangeOut(RevisionOut):
    entity: ChangedOut = Field(description="The record it changed.")


class ChangesParams(Schema):
    limit: int = Field(10, ge=1, le=50, description="How many, newest first.")


@router.get(
    "/projects/{project_id}/changes",
    response=list[ProjectChangeOut],
    summary="A project's latest changes",
    by_alias=True,
)
def project_changes(
    request: HttpRequest,
    project_id: UUID,
    params: Query[ChangesParams],
) -> list[dict[str, Any]]:
    """Newest first: the latest revisions of the records in the project (its own, and those
    linked into it) that you can read, as its Entities tab lists them, each with the record
    it changed. Its records' own revisions: a registry entry's curation is on the entry's
    history (ADR 0115). Takes a role in the project. The full trail of each is the record's
    ``/revisions``."""
    me = caller(request)
    found = project(me, project_id, Action.PROJECT_READ)
    records = search.queryset(me, search.Search(project=found.pk)).values("pk")
    revisions = list(
        Revision.objects.filter(entity__in=records)
        .select_related("user", "agent", "entity")
        # Revisions' IDs are UUIDv7, in the order they were made, imported ones' included:
        # newest first by the primary key's index, without sorting the project's history.
        .order_by("-id")[: params.limit]
    )
    entities = list({r.entity_id: r.entity for r in revisions}.values())
    values = services.current_values_of(entities) if entities else {}
    made = _runs_of(me, revisions)
    states = revision_states(revisions)
    out = []
    for r in revisions:
        fields = shape(r.entity.type).fields()
        shown = {n: to_json(fields[n], v) for n, v in values.get(r.entity_id, {}).items()}
        out.append(
            {
                **_revision(r, made, states=states),
                "entity": {
                    "id": r.entity.pk,
                    "hrid": r.entity.hrid,
                    "title": _title(r.entity, shown),
                    "type": r.entity.type,
                    "archived": r.entity.archived_at is not None,
                },
            }
        )
    return out


def _holds_opened(entity: Entity, revisions: list[Revision]) -> dict[int, list[dict[str, Any]]]:
    """The holds each of a record's revisions opened (ADR 0127), as ``HeldOut``, by
    revision number: reviewed or not, as its history keeps them."""
    numbers = [r.number for r in revisions if r.entity_id == entity.pk]
    if not numbers or entity.registry_id is not None:
        return {}
    out: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in (
        RuleHold.objects.filter(entity_id=entity.pk, revision__in=numbers)
        .select_related("held_by", "held_agent", "reviewed_by")
        .order_by("held_at", "pk")
    ):
        out[row.revision].append(
            {
                "id": row.pk,
                "rule": row.rule,
                "label": row.label,
                "message": row.message,
                "held_by": row.held_by,
                "agent": row.held_agent.client_name if row.held_agent is not None else None,
                "held_at": row.held_at,
                "reason": row.reason,
                "revision": row.revision,
                "last_revision": row.last_revision,
                "reviewer": row.reviewed_by,
                "reviewed_at": row.reviewed_at,
                "review_reason": row.review_reason,
            }
        )
    return out


@router.get(
    "/entities/{ref}/revisions/{number}",
    response=RevisionDetailOut,
    summary="An entity as it was at a revision",
    by_alias=True,
)
def get_revision(
    request: HttpRequest, ref: str, number: int, project: UUID | None = None
) -> dict[str, Any]:
    """``project`` says whose record of a registry entry, as for reading it: a view pins
    its own project's record (ADR 0115)."""
    me = caller(request)
    entity = _find(me, ref, project)
    revision = (
        Revision.objects.filter(entity=entity, number=number)
        .select_related("user", "agent")
        .first()
    )
    if revision is None:
        raise Problem(404, f"{entity.hrid} has no revision {number}.")
    # Fields since removed are read back too, as history keeps them (ADR 0035).
    fields = shape(entity.type).fields(removed=True)
    values = services.revision_values(entity, number, fields)
    changes = {n: c for n, c in services.changes(entity, revision).items() if n in fields}
    wanted = _ids_in(fields, values)
    for name, (before, after) in changes.items():
        for kind, ids in _ids_in(fields, {name: before}).items():
            wanted[kind] |= ids
        for kind, ids in _ids_in(fields, {name: after}).items():
            wanted[kind] |= ids
    refs, files = _lookup(me, wanted, projects={entity.owner_project_id})
    return {
        **_revision(revision, _runs_of(me, [revision])),
        "holds": _holds_opened(entity, [revision]).get(revision.number, []),
        "values": {n: to_json(fields[n], v) for n, v in values.items()},
        "changes": {
            name: {"before": to_json(fields[name], before), "after": to_json(fields[name], after)}
            for name, (before, after) in changes.items()
        },
        "refs": refs_in(refs, wanted["ref"], entity.owner_project_id),
        "files": {str(i): f for i, f in files.items()},
        "people": _people_out(me, wanted["person"], entity),
    }


class ExportParams(Schema):
    format: Literal["genbank", "fasta"] = Field(
        description="Which file: `genbank` (with its features) or `fasta`, as its type's "
        "`exports` list them."
    )
    revision: int | None = Field(
        None, ge=1, description="Which revision, as it was then. Its latest if left out."
    )
    project: UUID | None = Field(
        None, description="Whose record of a registry entry, as for reading it (ADR 0115)."
    )


@router.get(
    "/entities/{ref}/export",
    response={200: None},
    summary="Download it as a file, such as GenBank",
    openapi_extra={
        "responses": {
            200: {
                "description": "The file, as an attachment named for the record.",
                "content": {"chemical/seq-na-genbank": {}, "chemical/seq-na-fasta": {}},
            }
        }
    },
)
def export_entity(request: HttpRequest, ref: str, params: Query[ExportParams]) -> HttpResponse:
    """The record as one of the files its type exports (ADR 0171), at its latest revision or
    the one asked for, written from its values then, so a revision is always the same file.
    Anyone who can read it may; each download is in the audit log (``entity.exported``),
    with the file's SHA-256. What the file can't say, such as a feature across a linear
    molecule's ends, is listed in it (GenBank's COMMENT), and ``Iki-Left-Out`` says how
    many such notes there are."""
    me = caller(request)
    entity = _find(me, ref, params.project)
    try:
        made = exports.export(me, entity, params.format, params.revision)
    except exports.Unexportable as why:
        raise Problem(404, str(why), code=why.code) from None
    response = HttpResponse(made.content, content_type=made.media_type)
    response["Content-Disposition"] = f'attachment; filename="{made.name}"'
    response["Cache-Control"] = "no-store"
    response["Iki-Left-Out"] = str(len(made.left_out))
    return response


def _people_out(me: User, ids: set[Any], entity: Entity) -> dict[str, dict[str, Any]]:
    """The people these IDs name, as a record's ``people`` has them (ADR 0122)."""
    found = people.shown(me, ids, {i: {entity.owner_project_id} for i in ids})
    return {
        str(pk): {"id": p.id, "name": p.name, "email": p.email, "active": p.active}
        for pk, p in found.items()
    }


#: A revision's run, as :func:`_runs_of` finds it: one the reader can't read.
WITHHELD: dict[str, Any] = {}


def _runs_of(me: User, revisions: Iterable[Revision]) -> dict[tuple[Any, int], dict[str, Any]]:
    """The runs that made these revisions, by their entity and number: each as ``RunRefOut``
    if the caller can read it, or :data:`WITHHELD`. A revision a run made with no run
    recorded, as one imported from elsewhere can be, isn't there. A record's revisions are
    its own effects'; a registry entry's (ADR 0115), those of the records it was made
    through."""
    made = [r for r in revisions if r.via == "run"]
    if not made:
        return {}
    ids = {r.entity_id for r in made}
    numbers = {r.number for r in made}
    effects = list(
        RunEffect.objects.filter(
            Q(entity_id__in=ids, revision__in=numbers)
            | Q(entry_id__in=ids, entry_revisions__overlap=list(numbers))
        ).select_related("run")
    )
    readable = set(
        visible_runs(me).filter(pk__in={e.run_id for e in effects}).values_list("pk", flat=True)
    )
    out: dict[tuple[Any, int], dict[str, Any]] = {}
    for e in effects:
        shown = (
            {"id": e.run.pk, "hrid": e.run.hrid, "action": e.run.action}
            if e.run_id in readable
            else WITHHELD
        )
        keys = [(e.entity_id, e.revision), *((e.entry_id, n) for n in e.entry_revisions)]
        for entity, number in keys:
            if number is not None and entity in ids and number in numbers:
                out[(entity, number)] = shown
    return out


def _revision(
    revision: Revision,
    runs: dict[tuple[Any, int], dict[str, Any]],
    *,
    entity: Entity | None = None,
    projects: set[Any] | None = None,
    states: dict[Any, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A revision as ``RevisionOut``. One of ``entity``'s registry entry's, for a reader of
    ``projects``, says who made it and when only if :func:`services.discloses` it. A page's
    revisions pass their ``states``, from :func:`revision_states`, found for all at once."""
    of_entry = entity is not None and revision.entity_id != entity.pk
    if of_entry and not services.discloses(revision, projects or set()):
        return undisclosed(revision)
    return {
        "number": revision.number,
        "kind": revision.kind,
        "entry": of_entry,
        "changed_fields": revision.changed_fields,
        "user": revision.user,
        "via": revision.via,
        "client": revision.client,
        "agent": agent_out(revision.agent, revision.client),
        "run": (run := runs.get((revision.entity_id, revision.number))) or None,
        "run_withheld": run is WITHHELD,
        "reason": revision.reason,
        "state": (states if states is not None else revision_states([revision])).get(revision.pk),
        "submitted": revision.submitted,
        "created_at": revision.created_at,
    }


def revision_states(revisions: list[Revision]) -> dict[Any, dict[str, Any]]:
    """What each state change among the revisions did, by revision ID: its field, its
    states, and the signatures that set it (ADR 0126). Their entities' types and their
    signatures are found for all of them at once, in two queries however many there are."""
    changed = [r for r in revisions if r.kind == RevisionKind.STATE_CHANGED]
    if not changed:
        return {}
    types = dict(
        Entity.objects.filter(pk__in={r.entity_id for r in changed}).values_list("pk", "type")
    )
    wanted = {i for r in changed for i in r.signatures or ()}
    carried = (
        {s.pk: s for s in signing_models.Signature.objects.filter(pk__in=wanted)} if wanted else {}
    )
    return {
        r.pk: {
            **services.state_change(types[r.entity_id], r),
            "signatures": [
                {
                    "id": i,
                    "manifestation": sig_services.manifestation(sig_services.parsed(carried[i]))
                    if i in carried
                    else "",
                    "present": i in carried,
                }
                for i in r.signatures or ()
            ],
        }
        for r in changed
    }


def undisclosed(revision: Revision) -> dict[str, Any]:
    """A registry entry's revision as told to a reader it isn't disclosed to: what it set,
    not who, how or when (ADR 0115)."""
    return {
        "number": revision.number,
        "kind": revision.kind,
        "entry": True,
        "changed_fields": revision.changed_fields,
        "user": None,
        "via": "",
        "client": "",
        "agent": None,
        "run": None,
        "run_withheld": False,
        "reason": "",
        "state": None,
        "submitted": None,
        "created_at": None,
    }


@router.get(
    "/entities/{ref}/provenance",
    response=dict[str, RevisionOut],
    summary="Where each value came from",
    by_alias=True,
)
def get_provenance(request: HttpRequest, ref: str) -> dict[str, dict[str, Any]]:
    """For each field, the revision that set its current value: who or what, how and when."""
    me = caller(request)
    entity = _find(me, ref)
    fields = shape(entity.type).fields()
    found = {n: r for n, r in services.provenance(entity).items() if n in fields}
    made = _runs_of(me, found.values())
    readable = set(access.readable_projects(me).values_list("pk", flat=True))
    states = revision_states(list(found.values()))
    return {
        name: _revision(revision, made, entity=entity, projects=readable, states=states)
        for name, revision in found.items()
    }


class OriginFileOut(Schema):
    id: UUID
    name: str


class SoftwareOut(Schema):
    name: str
    version: str = Field(description="Empty when the source doesn't say.")


class MadeOut(Schema):
    kind: Literal["experimental", "computational"]
    method: str = Field(description="As the source names it, such as X-RAY DIFFRACTION.")
    software: list[SoftwareOut]
    details: dict[str, str | float | bool] = Field(
        description="What else the source states, such as a model's mean pLDDT."
    )


class OriginOut(Schema):
    source: str = Field(description="The database or format, such as NCBI Nucleotide.")
    id: str = Field(description="Its identifier there: an accession, or a record's name.")
    version: str = Field(description="The record's version or the database's release.")
    url: str = Field(description="Where people can see the record (https), if the run said.")
    retrieved_at: datetime | None = Field(description="When the run fetched it.")
    sha256: str = Field(description="The SHA-256 of the record as the run read it.")
    file: OriginFileOut | None = Field(description="The file it was read from, if you can read it.")
    run: RunRefOut | None = Field(description="The run that imported it, if you can read it.")
    revision: int | None = Field(
        description="The revision the run made; none if the entity was already there."
    )
    made: MadeOut | None = Field(
        description="How the record says its source made it (ADR 0106): its source's word."
    )
    recorded_at: datetime


@router.get(
    "/entities/{ref}/origins",
    response=list[OriginOut],
    summary="Where it was imported from",
)
def get_origins(request: HttpRequest, ref: str) -> list[dict[str, Any]]:
    """The records runs imported it from, newest first: a public database's, or one in a
    file. An entity found again by a later import lists each."""
    me = caller(request)
    entity = _find(me, ref)
    rows = list(
        ExternalOrigin.objects.filter(entity=entity).select_related("run", "file").order_by("-id")
    )
    runs = set(
        visible_runs(me).filter(pk__in={r.run_id for r in rows}).values_list("pk", flat=True)
    )
    files = set(
        access.readable_files(me)
        .filter(pk__in={r.file_id for r in rows if r.file_id})
        .values_list("pk", flat=True)
    )
    return [
        {
            "source": row.source,
            "id": row.identifier,
            "version": row.version,
            "url": row.url,
            "retrieved_at": row.retrieved_at,
            "sha256": row.sha256,
            "file": (
                {"id": row.file.pk, "name": row.file.name}
                if row.file is not None and row.file.pk in files
                else None
            ),
            "run": (
                {"id": row.run.pk, "hrid": row.run.hrid, "action": row.run.action}
                if row.run_id in runs
                else None
            ),
            "revision": row.revision,
            "made": _made(row.made),
            "recorded_at": row.created_at,
        }
        for row in rows
    ]


def _made(made: dict[str, Any] | None) -> dict[str, Any] | None:
    if not made:
        return None
    return {
        "kind": made["kind"],
        "method": made.get("method", ""),
        "software": [
            {"name": t["name"], "version": t.get("version", "")} for t in made.get("software", [])
        ],
        "details": made.get("details", {}),
    }


class MakingSourceOut(Schema):
    source: str = Field(description="The database or format, such as AlphaFold DB.")
    id: str = Field(description="The record there.")
    version: str
    url: str


class MakingOut(Schema):
    by: Literal["iki", "source"] = Field(
        description="`iki`: Iki's record, made by a run of an action that runs the software. "
        "`source`: what an imported record says about how its source made it."
    )
    kind: Literal["experimental", "computational"]
    method: str = Field(description="As the source names it; empty for Iki's own record.")
    software: str = Field(description="Empty when a source says how, but not with what.")
    version: str
    run: RunRefOut | None = Field(
        description="The run that made it, or imported the record, if you can read it."
    )
    source: MakingSourceOut | None = Field(description="The record that says so, for `source`.")
    recorded_at: datetime


@router.get(
    "/entities/{ref}/making",
    response=list[MakingOut],
    summary="How it was made",
)
def get_making(request: HttpRequest, ref: str) -> list[dict[str, Any]]:
    """How it was made, and who says so (ADR 0106), one tool at a time, Iki's own records
    first: a run of an action that lists its software, or what a record it was imported
    from says. Empty when it was made by hand, or its source doesn't say."""
    me = caller(request)
    entity = _find(me, ref)
    rows = list(
        EntityMaking.objects.filter(entity=entity).select_related("run", "origin").order_by("id")
    )
    runs = set(
        visible_runs(me).filter(pk__in={r.run_id for r in rows}).values_list("pk", flat=True)
    )
    rows.sort(key=lambda r: (r.by != MadeBy.IKI, r.id))
    return [
        {
            "by": row.by,
            "kind": row.kind,
            "method": row.method,
            "software": row.software,
            "version": row.version,
            "run": (
                {"id": row.run.pk, "hrid": row.run.hrid, "action": row.run.action}
                if row.run_id in runs
                else None
            ),
            "source": (
                {
                    "source": row.origin.source,
                    "id": row.origin.identifier,
                    "version": row.origin.version,
                    "url": row.origin.url,
                }
                if row.origin is not None
                else None
            ),
            "recorded_at": row.created_at,
        }
        for row in rows
    ]


class BundleOriginOut(Schema):
    deployment: str = Field(description="The trusted deployment the bundle was from.")
    project: str = Field(description="Its project there.")
    hrid: str = Field(description="Its HRID there.")
    action: Literal["new", "update"] = Field(
        description="Whether the import made it, or added revisions it didn't have."
    )
    revisions: int = Field(description="How many revisions the import added.")
    applied_by: PersonRef | None
    applied_at: datetime | None
    reason: str = Field(description="Why it was imported.")
    bundle_import: UUID | None = Field(
        description="The import, if you may see it: an Org Admin who is the project's Admin."
    )
    project_slug: str = Field(description="The project it was imported into, for its link.")


@router.get(
    "/entities/{ref}/bundles",
    response=list[BundleOriginOut],
    summary="Which bundles it was imported from",
)
def get_bundle_origins(request: HttpRequest, ref: str) -> list[dict[str, Any]]:
    """The imports from other deployments that made it here, or added to its history,
    newest first (ADR 0096)."""
    from iki_server.bundles.models import EntityPlan, ImportEntity, ImportState

    me = caller(request)
    entity = _find(me, ref)
    rows = (
        ImportEntity.objects.filter(
            entity_id=entity.pk,
            bundle_import__state=ImportState.APPLIED,
            action__in=[EntityPlan.NEW, EntityPlan.UPDATE],
        )
        .select_related("bundle_import__project__team", "bundle_import__applied_by")
        .order_by("-bundle_import__applied_at")
    )
    out = []
    for row in rows:
        made = row.bundle_import
        out.append(
            {
                "deployment": made.trusted_as,
                "project": made.source.get("project", {}).get("name", ""),
                "hrid": row.hrid,
                "action": row.action,
                "revisions": row.revisions if row.action == EntityPlan.NEW else row.adds,
                "applied_by": made.applied_by,
                "applied_at": made.applied_at,
                "reason": made.reason,
                "bundle_import": made.pk
                if permissions.can(me, Action.PROJECT_IMPORT, made.project)
                else None,
                "project_slug": made.project.slug,
            }
        )
    return out


# --- Projects ------------------------------------------------------------------------------------


def _link_out(link: ProjectLink, entity: Entity) -> dict[str, Any]:
    return {
        "project": link.project,
        "owner": link.project_id == entity.owner_project_id,
        "linked_by": link.linked_by,
        "linked_at": link.linked_at,
    }


@router.get(
    "/entities/{ref}/projects",
    response=list[EntityProjectOut],
    summary="The projects an entity belongs to",
)
def list_entity_projects(request: HttpRequest, ref: str) -> list[dict[str, Any]]:
    """Only the projects you can see."""
    me = caller(request)
    entity = _find(me, ref)
    links = (
        ProjectLink.objects.filter(
            entity=entity, project__in=permissions.visible_projects(me).values("pk")
        )
        .select_related("project", "linked_by")
        .order_by("linked_at", "pk")
    )
    return [_link_out(link, entity) for link in links]


@router.put(
    "/entities/{ref}/projects/{project_id}",
    response={200: EntityProjectOut, 201: EntityProjectOut},
    summary="Add an entity to a project",
)
def link_entity(request: HttpRequest, ref: str, project_id: UUID) -> Any:  # noqa: ANN401
    """Its members can then read it. Only its owner project's Editors can change it.
    You need to be able to read the entity, and to be an Editor in the project."""
    me = caller(request)
    entity = _find(me, ref)
    target = project(me, project_id, Action.PROJECT_EDIT)
    created = services.link(me, entity, target)
    link = ProjectLink.objects.select_related("project", "linked_by").get(
        entity=entity, project=target
    )
    return Status(201 if created else 200, _link_out(link, entity))


@router.delete(
    "/entities/{ref}/projects/{project_id}",
    response={204: None},
    summary="Take an entity out of a project",
)
def unlink_entity(request: HttpRequest, ref: str, project_id: UUID) -> Status[None]:
    """An Editor in that project, or in the entity's owner project, can. The owner project
    itself can't be removed."""
    me = caller(request)
    entity = _find(me, ref)
    target = project(me, project_id)
    owner = services.owner_of(entity)
    if not (
        permissions.can(me, Action.PROJECT_EDIT, target)
        or permissions.can(me, Action.PROJECT_EDIT, owner)
    ):
        require(me, Action.PROJECT_EDIT, target)
    if not services.unlink(me, entity, target):
        raise Problem(404, f"{entity.hrid} isn’t in that project.")
    return Status(204, None)
