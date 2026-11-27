"""Iki's action helper: what an action's command reads, and what it writes (ADR 0287).

A command reads its run from ``$IKI_INPUT`` (run-input/v1) and writes what it changes to
``$IKI_OUTPUT`` (run-output/v1). This module does both, and says why a run failed the way
Iki shows it::

    import iki_action as iki


    def store(run: iki.Run) -> iki.Output:
        sample = run.input("sample")
        out = run.output()
        if not sample["values"].get("storage"):
            out.update("sample", sample, {"storage": run.params["location"]})
        return out


    if __name__ == "__main__":
        iki.main(store)

Keep it a vendored copy, unchanged, beside your scripts: ``iki plugin vendor .`` writes the
version your Iki's CLI has, and ``iki plugin validate`` refuses a copy that isn't a
published version. It uses only Python's standard library (3.8 and later), so it runs in
any image, Iki's or one a lab brings (CRISPResso2, Cell Ranger).

What a run gives, a :class:`Run` and its :class:`Entity` objects, are still the JSON's
dicts: ``run["params"]`` and ``entity["values"]`` read as they always did, with names for
what most commands read beside them. None of the names hides a dict's own method, so code
that walks the JSON as dicts walks these too.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    NoReturn,
    Optional,
    Tuple,
    Type,
    Union,
)

#: This helper's version. A new one is published with Iki when what it does changes; the
#: run input and output it reads and writes are v1.
VERSION = "1.0.0"
API_VERSION = "iki/v1"
#: The most output.json may hold, as the worker reads it; larger results go in files.
MAX_OUTPUT_BYTES = 2_500_000
#: The most messages and metrics a run keeps.
MAX_MESSAGES = 100
MAX_METRICS = 100
#: The most of a message, or of the reason a run failed, that a run keeps.
MAX_TEXT = 2000

__all__ = [
    "API_VERSION",
    "MAX_MESSAGES",
    "MAX_METRICS",
    "MAX_OUTPUT_BYTES",
    "MAX_TEXT",
    "VERSION",
    "Entity",
    "Output",
    "Refused",
    "Run",
    "fail",
    "is_a",
    "main",
    "progress",
    "read",
    "ref",
    "write",
]


class Refused(Exception):  # noqa: N818 - reads as "the run is refused"
    """What keeps a run from doing what it was asked, said as the run's page shows it.

    Raise it anywhere under :func:`main`: it prints the reason as the run's ``::error``
    line and fails the attempt, writing nothing."""


class Entity(Dict[str, Any]):
    """An input entity, or one an input includes, as the run input gives it: the dict,
    with names for what commands read most."""

    @property
    def id(self) -> str:
        """Its project record's ID."""
        return str(self["id"])

    @property
    def hrid(self) -> str:
        return str(self["hrid"])

    @property
    def type(self) -> str:
        """Its own type, qualified, such as ``iki/protein.Protein``."""
        return str(self["type"])

    @property
    def types(self) -> List[str]:
        """Its type and every type it extends, nearest first, ending ``iki/core.Entity``."""
        return list(self["types"])

    @property
    def revision(self) -> int:
        return int(self["revision"])

    @property
    def ref(self) -> str:
        """What a reference field takes to name it: its registry entry if it's
        registered, or else its record."""
        return ref(self)

    def is_a(self, type: str) -> bool:
        """Whether it's of ``type``, or of a type that extends it."""
        return is_a(self, type)

    def included(self, field: str) -> List[Entity]:
        """The entities its reference ``field`` names, when the input includes them: one
        for a reference, a list's in order, none when it's empty."""
        return [_entity(e) for e in (self.get("included") or {}).get(field, [])]


def _entity(given: Mapping[str, Any]) -> Entity:
    return given if isinstance(given, Entity) else Entity(given)


def is_a(entity: Mapping[str, Any], type: str) -> bool:
    """Whether ``entity`` (an input's, or one it includes) is of ``type``, or of a type
    that extends it: a lab's own subtype of a protein is a protein. Compare types this way,
    never by ``entity["type"] ==``."""
    return type in entity["types"]


def ref(entity: Mapping[str, Any]) -> str:
    """What a reference field takes to name ``entity``: its registry entry's ID if it's
    registered, or else its record's."""
    return str(entity.get("entry_id") or entity["id"])


class Run(Dict[str, Any]):
    """The run input: the run, its params and its inputs at the revisions it was launched
    on. Read it with :func:`read`."""

    def __init__(self, given: Mapping[str, Any]) -> None:
        super().__init__(given)
        inputs = self.get("inputs") or {}
        self["inputs"] = {name: [_entity(e) for e in items] for name, items in inputs.items()}

    @property
    def id(self) -> str:
        return str(self["run"]["id"])

    @property
    def hrid(self) -> str:
        return str(self["run"]["hrid"])

    @property
    def attempt(self) -> int:
        return int(self["run"]["attempt"])

    @property
    def action(self) -> str:
        """The action's qualified name, such as ``iki/protein.compute_properties``."""
        return str(self["run"]["action"])

    @property
    def plugin(self) -> str:
        """The plugin's name, such as ``iki/protein``."""
        return str(self["run"]["plugin"]["name"])

    @property
    def project(self) -> str:
        return str(self["run"]["project"])

    @property
    def seed(self) -> Optional[int]:
        seed = self["run"].get("seed")
        return None if seed is None else int(seed)

    @property
    def params(self) -> Dict[str, Any]:
        params: Dict[str, Any] = self.get("params") or {}
        return params

    @property
    def settings(self) -> Dict[str, Any]:
        """The plugin's settings, as the deployment set them when the run was launched."""
        settings: Dict[str, Any] = self.get("settings") or {}
        return settings

    def inputs(self, name: str) -> List[Entity]:
        """The entities given as input ``name``: none if it was given none."""
        items: List[Entity] = self["inputs"].get(name, [])
        return items

    def input(self, name: str) -> Entity:
        """The one entity given as input ``name``. Refuses the run if there isn't
        exactly one."""
        items = self.inputs(name)
        if len(items) != 1:
            raise Refused(f"The run needs one {name}; it was given {len(items)}.")
        return items[0]

    def file(self, id: str) -> Path:
        """Where the worker put the file with this ID, which the inputs or a param name.
        Refuses the run if it has no such file."""
        described = (self.get("files") or {}).get(id)
        if not described or "path" not in described:
            raise Refused(f"The run has no file {id}.")
        return Path(described["path"])

    @property
    def out_dir(self) -> Path:
        """The directory a result file is written to, to name in ``{"$file": name}``."""
        return Path(os.environ["IKI_OUT_DIR"])

    def qualify(self, type: str) -> str:
        """``type`` qualified: a name of this plugin's own, such as ``Protein``, is
        ``<plugin>.Protein``; a qualified one is itself."""
        return type if "." in type else f"{self.plugin}.{type}"

    def output(self) -> Output:
        """An empty output for this run, to fill and return."""
        return Output(self)


def read(path: Union[str, os.PathLike[str], None] = None) -> Run:
    """The run input, from ``path`` or ``$IKI_INPUT``. One that names a version or kind
    this helper doesn't read is refused; a test's input may leave them out."""
    source = Path(path if path is not None else os.environ["IKI_INPUT"])
    given = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(given, dict):
        raise Refused(f"{source} isn't a run input.")
    if given.get("apiVersion", API_VERSION) != API_VERSION:
        raise Refused(
            f"The run input is {given['apiVersion']}, which this action helper "
            f"({VERSION}) doesn't read: vendor a newer one with `iki plugin vendor`."
        )
    if given.get("kind", "RunInput") != "RunInput":
        raise Refused(f"{source} is a {given['kind']}, not a RunInput.")
    return Run(given)


def _id(entity: Union[Mapping[str, Any], str]) -> str:
    return entity if isinstance(entity, str) else str(entity["id"])


class Output:
    """What a run writes: the entities it updates and creates, its messages and its
    metrics. Iki writes all of it in one transaction, or none of it."""

    def __init__(self, run: Optional[Run] = None) -> None:
        self._run = run
        self.updates: Dict[str, List[Dict[str, Any]]] = {}
        self.creates: List[Dict[str, Any]] = []
        self.messages: List[Dict[str, Any]] = []
        self.metrics: Dict[str, float] = {}
        #: Anything else run-output/v1 takes, by its key: `creates_file`, `park`.
        self.extra: Dict[str, Any] = {}

    def _qualify(self, type: str) -> str:
        if "." in type or self._run is None:
            return type
        return self._run.qualify(type)

    def update(
        self,
        input: str,
        entity: Union[Mapping[str, Any], str],
        values: Mapping[str, Any],
        **fields: Any,
    ) -> None:
        """New values of fields of an entity given as ``input``. ``fields`` are the
        update's others: ``reason``, ``require_unchanged``."""
        self.updates.setdefault(input, []).append(
            {"entity": _id(entity), "values": dict(values), **fields}
        )

    def create(
        self,
        type: str,
        values: Mapping[str, Any],
        key: Optional[str] = None,
        **fields: Any,
    ) -> Optional[Dict[str, str]]:
        """A new entity of ``type``, which is qualified with this plugin's name if it
        isn't already. ``fields`` are the create's others (``receipt``, ``fills``,
        ``derived_from``, ``origin``, ``made_with``). Given a ``key``, returns what a later
        create's ref field gives to name it, ``{"$create": key}``."""
        create: Dict[str, Any] = {"type": self._qualify(type), "values": dict(values), **fields}
        if key is not None:
            create["key"] = key
        self.creates.append(create)
        return None if key is None else {"$create": key}

    def message(
        self, text: str, level: str = "info", entity: Union[Mapping[str, Any], str, None] = None
    ) -> None:
        """A note for whoever reads the run: ``info``, ``warning`` or ``error``. An
        ``error`` about an entity fails its run. Past the 100 a run keeps, the last says
        how many more there were."""
        if level not in ("info", "warning", "error"):
            raise ValueError(f"A message's level is info, warning or error, not {level!r}.")
        message: Dict[str, Any] = {"level": level, "text": text[:MAX_TEXT]}
        if entity is not None:
            message["entity"] = _id(entity)
        self.messages.append(message)

    def info(self, text: str, entity: Union[Mapping[str, Any], str, None] = None) -> None:
        self.message(text, "info", entity)

    def warning(self, text: str, entity: Union[Mapping[str, Any], str, None] = None) -> None:
        self.message(text, "warning", entity)

    def error(self, text: str, entity: Union[Mapping[str, Any], str, None] = None) -> None:
        self.message(text, "error", entity)

    def metric(self, name: str, value: float) -> None:
        """A number about the run, kept on it, such as a score or a count."""
        self.metrics[name] = value

    def document(self) -> Dict[str, Any]:
        """The run output, as output.json holds it."""
        out: Dict[str, Any] = {"apiVersion": API_VERSION, "kind": "RunOutput"}
        if self.updates:
            out["updates"] = self.updates
        if self.creates:
            out["creates"] = self.creates
        out.update(self.extra)
        if self.messages:
            messages = self.messages
            if len(messages) > MAX_MESSAGES:
                # An error fails the run of the entity it's about, so none is dropped: past
                # the most a run keeps, the run fails with the first.
                errors = [m for m in messages if m["level"] == "error"]
                if len(errors) >= MAX_MESSAGES:
                    raise Refused(f"{len(errors)} problems, the first: {errors[0]['text']}")
                others = [m for m in messages if m["level"] != "error"]
                kept = errors + others[: MAX_MESSAGES - 1 - len(errors)]
                more = len(messages) - len(kept)
                messages = [*kept, {"level": "warning", "text": f"And {more} more."}]
            out["messages"] = messages
        if self.metrics:
            if len(self.metrics) > MAX_METRICS:
                raise Refused(f"A run keeps at most {MAX_METRICS} metrics.")
            out["metrics"] = self.metrics
        return out

    def write(self, path: Union[str, os.PathLike[str], None] = None) -> None:
        """Writes output.json to ``path`` or ``$IKI_OUTPUT``. Refuses the run if it's over
        what the worker reads."""
        write(self.document(), path)


def write(
    output: Union[Output, Mapping[str, Any]], path: Union[str, os.PathLike[str], None] = None
) -> None:
    """Writes ``output`` (an :class:`Output`, or run-output's fields as a dict) as
    output.json, to ``path`` or ``$IKI_OUTPUT``."""
    if isinstance(output, Output):
        document = output.document()
    else:
        document = {"apiVersion": API_VERSION, "kind": "RunOutput", **output}
    try:
        text = json.dumps(document, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except ValueError:
        raise Refused("A number the run computed isn't one: it's NaN or infinite.") from None
    if len(text.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise Refused(
            "What the run writes is over the 2.5 MB a run's output holds: give it fewer, "
            "or write its creates to a file in IKI_OUT_DIR."
        )
    Path(path if path is not None else os.environ["IKI_OUTPUT"]).write_text(text, encoding="utf-8")


def progress(fraction: float, text: str = "") -> None:
    """Says how far the run has got, from 0 to 1, as the run's page shows it."""
    print(f"::progress {max(0.0, min(1.0, fraction)):.3g} {text}".rstrip(), flush=True)


def fail(reason: str) -> NoReturn:
    """Fails the attempt, with ``reason`` as the run's page shows it."""
    line = " ".join(str(reason).split())[:MAX_TEXT]
    print(f"::error {line}", flush=True)
    sys.exit(1)


Action = Callable[[Run], Union[Output, Mapping[str, Any], None]]


def main(
    action: Union[Action, Mapping[str, Action]],
    argv: Optional[Iterable[str]] = None,
    refuse: Union[Type[Exception], Tuple[Type[Exception], ...]] = (),
) -> None:
    """Runs ``action`` on the run input and writes what it returns: an :class:`Output`,
    run-output's fields as a dict, or nothing for an empty output. Given commands by name,
    runs the one the command line's first argument names. A :class:`Refused` fails the
    attempt with its reason, and so does an exception of a class in ``refuse``, such as
    the ``ValueError`` a parser raises for a file it can't read."""
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if isinstance(action, Mapping):
            if not args or args[0] not in action:
                raise Refused(f"Name one of these commands: {', '.join(sorted(action))}.")
            chosen = action[args[0]]
        else:
            chosen = action
        run = read()
        try:
            result = chosen(run)
        except refuse as why:
            raise Refused(str(why)) from why
        write(result if result is not None else run.output())
    except Refused as why:
        fail(str(why))
