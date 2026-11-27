"""Every problem ``code`` the API answers with, its status and the members it carries
(ADR 0290).

A problem's ``code`` is stable and meant for programs (ADR 0027). This is the one place
that says which codes there are: :class:`~iki_server.api.problems.Problem` refuses a code,
a status or a member that isn't declared here when the tests run, and logs one in
production. The OpenAPI document is built from it, one schema per code that carries
members, so a client generated from it reads each problem by its ``code``.
"""

from dataclasses import dataclass, field
from typing import Any

#: JSON Schema for ``errors``: what was wrong, and where.
_ERRORS = {"type": "array", "items": {"$ref": "#/components/schemas/ProblemError"}}
_ID = {"type": "string", "format": "uuid"}
_TEXT = {"type": "string"}
_NUMBER = {"type": "integer"}
_RECORD = {
    "type": "object",
    "required": ["id", "hrid", "type"],
    "properties": {"id": _ID, "hrid": _TEXT, "type": _TEXT},
}


def _ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/components/schemas/{name}"}


def _list(items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": items}


@dataclass(frozen=True)
class Code:
    """One problem code."""

    #: The HTTP status it's answered with.
    status: int
    #: What it means, and what to do about it, for the OpenAPI document.
    summary: str
    #: The members it carries beyond every problem's, as JSON Schema properties.
    members: dict[str, Any] = field(default_factory=dict)
    #: Of those, the ones it always carries.
    required: tuple[str, ...] = ()
    #: Answered only by the worker protocol (``/api/worker``), never by ``/api/v1``.
    worker: bool = False


#: Members each schema may hold, beside the ones a code declares.
SHARED_SCHEMAS: dict[str, dict[str, Any]] = {
    "StaleRecord": {
        "type": "object",
        "description": "A record a change named that changed since it was read.",
        "required": ["id", "hrid", "expected_revision", "current_revision"],
        "properties": {
            "id": _ID,
            "hrid": _TEXT,
            "expected_revision": _NUMBER,
            "current_revision": _NUMBER,
        },
    },
    "StaleOption": {
        "type": "object",
        "description": "A lab list's option a reorder named that changed, came or went "
        "since it was read: a revision is null where it wasn't, or isn't, in the list.",
        "required": ["code", "expected_revision", "current_revision"],
        "properties": {
            "code": _TEXT,
            "expected_revision": {"anyOf": [_NUMBER, {"type": "null"}]},
            "current_revision": {"anyOf": [_NUMBER, {"type": "null"}]},
        },
    },
    "StaleLine": {
        "type": "object",
        "description": "A receipt's or count's line that changed, at its revision now.",
        "required": ["line_id", "revision"],
        "properties": {"line_id": _ID, "revision": _NUMBER},
    },
    "StaleRow": {
        "type": "object",
        "description": "A sheet's row whose record changed since the sheet was read.",
        "required": ["key", "entity", "hrid", "revision"],
        "properties": {"key": _TEXT, "entity": _ID, "hrid": _TEXT, "revision": _NUMBER},
    },
}

CODES: dict[str, Code] = {
    # --- Any request -------------------------------------------------------------------------
    "bad_request": Code(400, "The request couldn't be read."),
    "invalid_json": Code(400, "The request body isn't valid JSON."),
    "not_authenticated": Code(401, "Sign in, or send a token."),
    "invalid_token": Code(401, "The token is unknown, expired or revoked."),
    "forbidden": Code(403, "You don't have permission to do that."),
    "insufficient_scope": Code(403, "The token's scopes don't allow it."),
    "token_limited_to_projects": Code(
        403, "The token is limited to some projects, and this reaches beyond them."
    ),
    "csrf_failed": Code(403, "A request signed in with the session lacked its CSRF token."),
    "web_only": Code(403, "Only a person in Iki's web app can do this; tokens can't."),
    "not_found": Code(404, "There's nothing here, or you can't see it."),
    "method_not_allowed": Code(405, "This method isn't supported here."),
    "not_acceptable": Code(406, "Iki can't answer in the format the request accepts."),
    "too_large": Code(413, "The request is larger than Iki takes."),
    "unsupported_media_type": Code(415, "The body's content type isn't one Iki reads."),
    "validation_failed": Code(
        422,
        "Some fields or parameters are invalid: `errors` says which, and why.",
        {"errors": _ERRORS},
        ("errors",),
    ),
    "rate_limited": Code(429, "Too many requests: wait for `Retry-After` seconds."),
    "internal_error": Code(500, "Something went wrong on Iki's side: the request ID finds it."),
    "unavailable": Code(503, "Iki can't do this right now: try again after `Retry-After`."),
    # --- Changes and their preconditions (ADR 0289) ------------------------------------------
    "precondition_required": Code(
        428, "Send `If-Match` with the one ETag you read, or `If-None-Match: *`."
    ),
    "stale": Code(
        412,
        "What the change named changed since it was read, and nothing changed. The current "
        "`ETag` comes with it when it names one thing; a change of many says which changed.",
        {
            "stale": {
                "type": "array",
                "description": "Each record or list option that changed since it was read.",
                "items": {"anyOf": [_ref("StaleRecord"), _ref("StaleOption")]},
            },
            "affected_lines": {
                "type": "array",
                "description": "A receipt's or count's lines that changed.",
                "items": _ref("StaleLine"),
            },
            "rows": {
                "type": "array",
                "description": "A sheet's rows whose records changed.",
                "items": _ref("StaleRow"),
            },
        },
    ),
    "conflict": Code(409, "It conflicts with what's recorded now."),
    "refused": Code(
        409,
        "The change would break one of Iki's rules: `detail` says which. `errors` names the "
        "field it's about, when there's one.",
        {"errors": _ERRORS},
    ),
    "too_many": Code(422, "It would reach more than Iki does at once: narrow it."),
    # --- Records ---------------------------------------------------------------------------
    "choose_project": Code(
        409,
        "The HRID names a record in several projects you can read: say which, with "
        "`?project=`, or name it by its ID.",
        {
            "records": _list(
                {
                    "type": "object",
                    "required": ["entity", "project"],
                    "properties": {
                        "entity": _ID,
                        "project": {
                            "type": "object",
                            "required": ["id", "name", "slug"],
                            "properties": {"id": _ID, "name": _TEXT, "slug": _TEXT},
                        },
                    },
                }
            )
        },
        ("records",),
    ),
    "purged": Code(
        410,
        "The record was purged with its plugin's data (ADR 0092): its tombstone comes with it.",
        {
            "entity": {
                "type": "object",
                "required": ["id", "hrid", "type", "purged_at", "content_sha256"],
                "properties": {
                    "id": _ID,
                    "hrid": _TEXT,
                    "type": _TEXT,
                    "purged_at": {"type": "string", "format": "date-time"},
                    "content_sha256": {"anyOf": [_TEXT, {"type": "null"}]},
                },
            }
        },
        ("entity",),
    ),
    "hold_reason_required": Code(
        422,
        "A rule holds the record for review: save again with a `hold_reason` to keep it.",
        {"errors": _ERRORS},
        ("errors",),
    ),
    "clear_reason_required": Code(
        422, "Clearing this flag needs a reason.", {"errors": _ERRORS}, ("errors",)
    ),
    "guard_refused": Code(
        409,
        "A plugin's guard (ADR 0128) refused the state.",
        {"guard": {"type": "string", "description": "The guard that refused it."}},
        ("guard",),
    ),
    "signatures_required": Code(409, "The state needs signatures it doesn't have yet."),
    "cannot_sign": Code(403, "You can't sign this, as `detail` says."),
    "search_too_slow": Code(422, "The search took too long: narrow it."),
    "sheet_problems": Code(
        422,
        "Some of a sheet's rows can't be saved: `errors` says which, and why. Nothing saved.",
        {"errors": _ERRORS},
        ("errors",),
    ),
    # --- Exports (ADRs 0229, 0230) -----------------------------------------------------------
    "no_export": Code(404, "No installed plugin writes that format for this record."),
    "no_data": Code(404, "The format holds none of this record's fields as data."),
    "no_field": Code(404, "The type has no such field."),
    "no_sequence": Code(404, "The record has no sequence to write."),
    "no_structure": Code(404, "The record has no structure to write."),
    "sequence_unreadable": Code(404, "The record's sequence can't be read for this format."),
    "report_not_issued": Code(404, "The report hasn't been issued, so it isn't exported."),
    "writer_refused": Code(422, "The plugin's writer refused the record: `detail` says why."),
    "writer_failed": Code(422, "The plugin's writer failed: tell whoever looks after it."),
    "sandbox_unavailable": Code(503, "The export sandbox is busy or away: try again shortly."),
    "helm_unavailable": Code(503, "HELM can't be read right now: try again shortly."),
    # --- Runs ------------------------------------------------------------------------------
    "inputs_outside_project": Code(
        409,
        "Some inputs aren't in the run's project.",
        {"entities": _list(_RECORD)},
        ("entities",),
    ),
    "outside_api_not_approved": Code(
        409,
        "The action calls outside APIs this project hasn't approved (ADR 0130).",
        {
            "apis": _list({"type": "string", "description": "Each API's qualified name."}),
            "approvers": _list({"type": "string", "description": "Org Admins who approve."}),
        },
        ("apis", "approvers"),
    ),
    "consent_required": Code(
        409,
        "The launch sends data to outside APIs: launch again with `consent` naming each "
        "`api` to agree (ADR 0130).",
        {"consent": _list(_ref("ConsentStatementOut"))},
        ("consent",),
    ),
    "artifact_deleted": Code(410, "The artifact was deleted once its retention was over."),
    "artifact_not_kept": Code(409, "The artifact didn't finish uploading."),
    # --- Files -----------------------------------------------------------------------------
    "file_scanning": Code(409, "The file is being scanned: try again after `Retry-After`."),
    "file_quarantined": Code(409, "The scanner found malware in the file, so it's held."),
    "file_not_scanned": Code(409, "The scanner couldn't inspect the file, so it's held."),
    "storage_not_configured": Code(503, "This deployment has no file storage set up."),
    "storage_unavailable": Code(503, "Iki can't reach its file storage: try again shortly."),
    # --- Storage ---------------------------------------------------------------------------
    "scan_ambiguous": Code(
        409,
        "The scanned code names more than one thing: choose one of `candidates`.",
        {
            "candidates": _list(_ref("ScanCandidateOut")),
            "index": {
                "type": "integer",
                "description": "Which of a rack's scans needs the choice, from 0.",
            },
        },
        ("candidates",),
    ),
    "scan_unavailable": Code(409, "The scan can't be used here, as `detail` says."),
    "moves_plan_changed": Code(
        409,
        "What the moves do changed since they were previewed: review `moves`, then confirm.",
        {"moves": _ref("MovesOut")},
        ("moves",),
    ),
    "count_unregistered_choice": Code(
        409,
        "An unregistered tube may be one seen earlier in the count: choose one of "
        "`observations`, or say it's new.",
        {
            "index": _NUMBER,
            "position": _TEXT,
            "observations": _list(
                {
                    "type": "object",
                    "required": ["line_id", "revision", "position"],
                    "properties": {"line_id": _ID, "revision": _NUMBER, "position": _TEXT},
                }
            ),
        },
        ("index", "position", "observations"),
    ),
    # --- Setup and plugins -----------------------------------------------------------------
    "starter_plan_changed": Code(
        409,
        "What applying the starter does changed since it was previewed: review `plan`.",
        {"plan": _ref("StarterSetupOut")},
        ("plan",),
    ),
    "stale_plan": Code(409, "The bundle's import plan changed since it was made: plan again."),
    "invalid_manifest": Code(422, "The plugin's manifest is invalid, as `detail` says."),
    "preflight_unavailable": Code(409, "The upgrade's preflight can't run now."),
    "preflight_needs_every_project": Code(
        403, "The preflight reads every project, so it needs an Org Admin."
    ),
    "component_misdeclared": Code(
        422, "The component's declaration doesn't work here: tell its plugin's maintainer."
    ),
    "not_shown_here": Code(422, "The component isn't shown in that place."),
    # --- The worker protocol (ADR 0029) ------------------------------------------------------
    "stale_lease": Code(409, "The worker no longer holds the job's lease.", worker=True),
    "wrong_state": Code(409, "The job isn't in a state that allows it.", worker=True),
    "template_gone": Code(409, "The worker's compute template is gone.", worker=True),
    "worker_outdated": Code(
        409,
        "The worker is older than this deployment takes: update it.",
        {"update": {"type": "object", "description": "Where the update is, and its digest."}},
        ("update",),
        worker=True,
    ),
    "log_gap": Code(
        409,
        "The log append skipped bytes: send from `next_offset`.",
        {"errors": _ERRORS, "next_offset": _NUMBER},
        ("errors", "next_offset"),
        worker=True,
    ),
}

#: The code a problem gets when it names none: its status's.
DEFAULT_CODES = {
    400: "bad_request",
    401: "not_authenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    406: "not_acceptable",
    409: "conflict",
    413: "too_large",
    415: "unsupported_media_type",
    422: "validation_failed",
    429: "rate_limited",
    500: "internal_error",
    503: "unavailable",
}
