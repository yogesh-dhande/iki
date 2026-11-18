"""Outside APIs (ADR 0130): reading them from a plugin's manifests, and the rules beyond the
schema.

A plugin names the third parties' APIs its actions call under ``apis`` in plugin.yaml: its
host, what it's for, the credential the egress proxy adds to each call, what Iki keeps of
each answer, and, for an API whose work is a job to submit and poll, how to poll it. Each
action that calls one says what of a run it sends, which people see before they launch.

Also here, for the server and the worker's input: reading a *locator* (a JSON pointer in an
answer, or ``header:<name>``) and filling a job's ID into a path.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from iki_spec import schemas
from iki_spec import validate as _validate
from iki_spec.model import (
    Action,
    ApiCredential,
    ApiJobs,
    ApiUse,
    ApiWebhook,
    OutsideApi,
    Plugin,
    parse_duration,
)
from iki_spec.problems import Path, Severity

if TYPE_CHECKING:
    from iki_spec.yamlsource import Document

__all__ = [
    "JOB_ID",
    "MAX_JOB_TIMEOUT",
    "MIN_POLL",
    "check",
    "job_path",
    "locate",
    "pointer",
    "read_apis",
    "read_uses",
    "resolve",
]

#: How often a parked run may be polled, at most, and the longest it may wait.
MIN_POLL = 5
MAX_JOB_TIMEOUT = 7 * 86400
#: What a provider's job ID may look like: it goes into a path.
JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:~-]{0,199}$")
#: Headers a credential can't go in: the proxy sets them, or they say how to read the body.
_NOT_CREDENTIAL_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "content-type",
        "content-encoding",
        "transfer-encoding",
        "connection",
        "upgrade",
        "te",
        "trailer",
        "keep-alive",
        "proxy-authorization",
        "proxy-connection",
        "accept-encoding",
    }
)


# --- Reading -----------------------------------------------------------------------------------


def _credential(given: dict[str, Any] | None) -> ApiCredential | None:
    if not given:
        return None
    header = given.get("header") or "Authorization"
    default = "Bearer " if header.lower() == "authorization" else ""
    return ApiCredential(
        secret=given["secret"],
        header=header,
        prefix=given.get("prefix", default),
        description=str(given.get("description", "")).strip(),
    )


def _jobs(given: dict[str, Any] | None) -> ApiJobs | None:
    if not given:
        return None
    hook = given.get("webhook")
    return ApiJobs(
        poll=given["poll"],
        status=given["status"],
        done=tuple(given["done"]),
        failed=tuple(given["failed"]),
        message=given.get("message", ""),
        cancel=given.get("cancel", ""),
        every=parse_duration(given["every"]) if "every" in given else 30,
        timeout=parse_duration(given["timeout"]) if "timeout" in given else 86400,
        usage=dict(given.get("usage") or {}),
        webhook=ApiWebhook(
            signature=hook["signature"], secret=hook["secret"], header=hook.get("header", "")
        )
        if hook
        else None,
    )


def read_apis(plugin: str, data: dict[str, Any]) -> dict[str, OutsideApi]:
    """The outside APIs plugin.yaml names under ``apis``."""
    out: dict[str, OutsideApi] = {}
    for name, spec in (data.get("apis") or {}).items():
        reports = spec.get("reports") or {}
        out[name] = OutsideApi(
            plugin=plugin,
            name=name,
            label=spec["label"].strip() or name,
            host=spec["host"],
            purpose=" ".join(spec["purpose"].split()),
            credential=_credential(spec.get("credential")),
            model=reports.get("model", ""),
            version=reports.get("version", ""),
            parameters=tuple(reports.get("parameters") or ()),
            usage=dict(reports.get("usage") or {}),
            jobs=_jobs(spec.get("jobs")),
        )
    return out


def read_uses(body: dict[str, Any]) -> tuple[ApiUse, ...]:
    """The outside APIs an action calls, and what it sends each, in the order it names them."""
    return tuple(
        ApiUse(
            name=name,
            inputs={i: tuple(f) for i, f in ((spec["sends"].get("inputs") or {}).items())},
            params=tuple(spec["sends"].get("params") or ()),
        )
        for name, spec in (body.get("apis") or {}).items()
    )


def resolve(plugin: Plugin) -> Plugin:
    """`plugin` with each of its actions' uses naming the API they call, as the plugin
    declares it, so a run's copy of its action says everything about where it sends data."""
    actions = {
        name: dataclasses.replace(
            action,
            apis=tuple(dataclasses.replace(u, api=plugin.apis.get(u.name)) for u in action.apis),
        )
        for name, action in plugin.actions.items()
        if action.apis
    }
    if not actions:
        return plugin
    return dataclasses.replace(plugin, actions={**plugin.actions, **actions})


# --- Reading answers ---------------------------------------------------------------------------


def pointer(document: Any, at: str) -> Any:  # noqa: ANN401 - JSON
    """The value at JSON pointer `at` (RFC 6901) in `document`; None where there's none."""
    if at == "":
        return document
    if not at.startswith("/"):
        return None
    found = document
    for raw in at[1:].split("/"):
        part = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(found, dict):
            if part not in found:
                return None
            found = found[part]
        elif isinstance(found, list):
            if not part.isdigit() or int(part) >= len(found) or (part != "0" and part[0] == "0"):
                return None
            found = found[int(part)]
        else:
            return None
    return found


def locate(locator: str, documents: Iterable[Any], headers: Mapping[str, str] | None = None) -> Any:  # noqa: ANN401 - JSON
    """What `locator` finds: a header's value, or the value at a JSON pointer in the last of
    `documents` that has one (a JSON answer is one document; a streamed answer's events are
    several). None where nothing has it."""
    if not locator:
        return None
    if locator.startswith("header:"):
        wanted = locator.removeprefix("header:").lower()
        for name, value in (headers or {}).items():
            if name.lower() == wanted:
                return value
        return None
    found = None
    for document in documents:
        value = pointer(document, locator)
        if value is not None:
            found = value
    return found


def job_path(template: str, job_id: str) -> str:
    """`template` with the job's ID, escaped as a path segment, in place of ``{id}``. Raises
    ValueError for an ID a provider wouldn't give."""
    if not JOB_ID.fullmatch(job_id):
        raise ValueError(f"`{job_id[:50]}` isn't a job ID Iki can poll.")
    return template.replace("{id}", quote(job_id, safe=""), 1)


def short(value: Any, limit: int = 1000) -> Any:  # noqa: ANN401 - JSON
    """A value kept from a request or answer: scalars as they are, anything else as JSON,
    and nothing longer than `limit` characters."""
    if value is None or isinstance(value, bool | int | float):
        return value
    text = value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --- Rules -------------------------------------------------------------------------------------


def check(semantics: _validate._Semantics) -> None:
    """Check the plugin's outside APIs, and its actions' uses of them."""
    _Rules(semantics).run()


class _Rules:
    def __init__(self, semantics: _validate._Semantics) -> None:
        self.s = semantics
        self.add = semantics.add
        self.plugin = semantics.plugin
        self.loaded = semantics.loaded
        self.source = semantics.source

    def run(self) -> None:
        for name, api in self.plugin.apis.items():
            self.api(api, ("apis", name))
        used: set[str] = set()
        for name, action in self.plugin.actions.items():
            source = self.loaded.action_sources[name]
            for use in action.apis:
                used.add(use.name)
                self.use(action, use, source)
            if action.apis:
                self.action(action, source)
        for name in sorted(self.plugin.apis.keys() - used):
            self.add(
                "unused-api",
                f"No action of this plugin calls `{name}`.",
                self.source,
                ("apis", name),
                key=True,
                severity=Severity.WARNING,
                hint="Delete it, or name it under an action's `apis`.",
            )

    def api(self, api: OutsideApi, path: Path) -> None:
        if api.host.startswith("{settings."):
            self.setting(api.host.removeprefix("{settings.")[:-1], (*path, "host"))
        credential = api.credential
        if credential is not None and credential.header.lower() in _NOT_CREDENTIAL_HEADERS:
            self.add(
                "api-credential-header",
                f"A credential can't go in `{credential.header}`: the egress proxy sets it, or "
                "it says how to read the call.",
                self.source,
                (*path, "credential", "header"),
                hint="Use the header the API takes its key in, such as `Authorization` or "
                "`X-Api-Key`.",
            )
        jobs = api.jobs
        if jobs is None:
            return
        at: Path = (*path, "jobs")
        if jobs.every < MIN_POLL:
            self.add(
                "api-polls-too-often",
                f"A parked run is polled at most every {MIN_POLL} seconds.",
                self.source,
                (*at, "every"),
                hint="Poll less often, and say how the API calls back (`webhook`) to hear sooner.",
            )
        if jobs.timeout > MAX_JOB_TIMEOUT:
            self.add(
                "api-job-timeout",
                "A run waits on a job for at most 7 days (`7d`).",
                self.source,
                (*at, "timeout"),
            )
        overlap = sorted(set(jobs.done) & set(jobs.failed))
        if overlap:
            self.add(
                "api-status-twice",
                f"{', '.join(f'`{s}`' for s in overlap)} can't say both that a job is done and "
                "that it failed.",
                self.source,
                (*at, "failed"),
            )
        hook = jobs.webhook
        if hook is not None:
            if hook.signature == "hmac-sha256" and not hook.header:
                self.add(
                    "webhook-needs-header",
                    "An `hmac-sha256` signature comes in a header: say which.",
                    self.source,
                    (*at, "webhook"),
                    hint="Such as `header: X-Signature`.",
                )
            if hook.signature == "standard-webhooks" and hook.header:
                self.add(
                    "webhook-header",
                    "Standard Webhooks signatures always come in `webhook-signature`.",
                    self.source,
                    (*at, "webhook", "header"),
                    key=True,
                    hint="Drop `header`.",
                )
            if credential is not None and hook.secret == credential.secret:
                self.add(
                    "webhook-secret-is-credential",
                    "The webhook's secret must be its own: the API's key never leaves the proxy.",
                    self.source,
                    (*at, "webhook", "secret"),
                )

    def setting(self, name: str, path: Path) -> None:
        """A host a deployment gives: a plugin setting that holds a hostname (ADR 0083)."""
        setting = self.plugin.settings.get(name)
        if setting is None:
            self.add(
                "network-unknown-setting",
                f"The plugin has no setting `{name}`.",
                self.source,
                path,
                hint=schemas._did_you_mean(name, list(self.plugin.settings))
                or "Declare it under `settings:` in plugin.yaml, with `format: hostname`.",
            )
        elif setting.type != "string" or setting.format != "hostname":
            self.add(
                "network-setting-not-hostname",
                f"`{name}` isn't a host name, so a run can't reach it.",
                self.source,
                path,
                hint=f"Give `{name}` `type: string` and `format: hostname`.",
            )

    def use(self, action: Action, use: ApiUse, source: Document) -> None:
        path: Path = ("apis", use.name)
        api = self.plugin.apis.get(use.name)
        if api is None:
            self.add(
                "unknown-api",
                f"The plugin has no outside API `{use.name}`.",
                source,
                path,
                key=True,
                hint=schemas._did_you_mean(use.name, list(self.plugin.apis))
                or "Declare it under `apis:` in plugin.yaml.",
            )
            return
        if api.host in action.network:
            self.add(
                "api-in-network",
                f"`{api.host}` is {api.label}'s host, so runs reach it only through `apis`: "
                "that's how the proxy adds its key and records each call.",
                source,
                ("network", action.network.index(api.host)),
                hint="Drop it from `network`.",
            )
        credential = api.credential
        secrets = {s.name: n for n, s in enumerate(action.secrets)}
        for secret in (credential.secret if credential else None, _hook_secret(api)):
            if secret is not None and secret in secrets:
                self.add(
                    "api-credential-is-secret",
                    f"`{secret}` is {api.label}'s, which the egress proxy uses: a run's command "
                    "never gets it.",
                    source,
                    ("secrets", secrets[secret]),
                    hint="Drop it from `secrets`.",
                )
        sends: Path = (*path, "sends")
        for name, wanted in use.inputs.items():
            put = action.inputs.get(name)
            if put is None:
                self.add(
                    "unknown-input",
                    f"This action has no input `{name}`.",
                    source,
                    (*sends, "inputs", name),
                    key=True,
                    hint=schemas._did_you_mean(name, list(action.inputs))
                    or ("It has no inputs." if not action.inputs else None),
                )
                continue
            target = self.s.types.get(put.type)
            if target is None:
                continue  # reported on the input
            fields = {n: f for n, (f, _) in self.s.all_fields(target).items() if not f.removed}
            chain = {put.type, *(t.qualified_name for t in self.s.ancestors(target))}
            for extension in self.plugin.extensions:
                if extension.extends_type in chain:
                    fields.update({n: f for n, f in extension.fields.items() if not f.removed})
            for index, field_name in enumerate(wanted):
                if field_name not in fields:
                    self.add(
                        "unknown-field",
                        f"`{put.type}` has no field `{field_name}`.",
                        source,
                        (*sends, "inputs", name, index),
                        hint=schemas._did_you_mean(field_name, list(fields)),
                    )
        for index, param in enumerate(use.params):
            if param not in action.params:
                self.add(
                    "unknown-param",
                    f"This action has no param `{param}`.",
                    source,
                    (*sends, "params", index),
                    hint=schemas._did_you_mean(param, list(action.params)),
                )
        if api.jobs is not None and action.batchable > 1:
            self.add(
                "batch-parks",
                f"A batchable action can't call {api.label}, whose work is a job a run waits "
                "on: one job runs it on several entities, so it couldn't park for any one run.",
                source,
                ("batchable",),
                hint="Don't batch it: a parked run holds no worker anyway.",
            )

    def action(self, action: Action, source: Document) -> None:
        if action.tier == "self_hosted":
            self.add(
                "self-hosted-apis",
                "Self-hosted actions run on machines their teams look after, so Iki can't keep "
                "an outside API's key from whoever runs them.",
                source,
                ("apis",),
                key=True,
                hint="Run it on the backend tier.",
            )
        if action.deterministic:
            self.add(
                "deterministic-with-api",
                "This action calls an outside API, whose model can change under it, so the "
                "same inputs may not give the same results, and Iki would reuse a run that's "
                "out of date.",
                source,
                ("deterministic",),
                key=True,
                severity=Severity.WARNING,
                hint="Drop `deterministic`.",
            )


def _hook_secret(api: OutsideApi) -> str | None:
    return api.jobs.webhook.secret if api.jobs and api.jobs.webhook else None
