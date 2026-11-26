"""What a file says about itself, besides when its data was acquired (ADR 0224).

An instrument writes into its files which instrument it is and who ran it: an FCS file's
``$CYT`` and ``$OP``, an mzML run's instrument configuration, an AB1 trace's ``HCFG`` tags.
Iki reads those when the file is added (see :mod:`iki_server.files.acquired`), keeps them
with the file, shows them on its page and finds the file by them in the Files list.

Each **fact** is one of :data:`FACTS`, with the value as the file gives it and where in the
file it's stated (``stated_in``), so whoever reads it can check it against the file. A
fact the file doesn't state, states twice differently, or states in a way the format
doesn't define is unknown: never inferred from anything else. A SAM or BAM file's
**read groups** are kept whole, each ``@RG`` line's standard tags, or not at all.

This module checks what's kept, whether a reader found it or a bundle from another
deployment brought it (ADR 0177's bundles carry it), and writes the text it's found by.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import cast
from xml.parsers import expat

#: What a file can say about where its data came from, as people would name each.
FACTS: dict[str, str] = {
    "instrument": "Instrument",
    "manufacturer": "Manufacturer",
    "serial": "Serial number",
    "instrument_name": "Instrument name",
    "operator": "Operator",
    "software": "Software",
    "protocol": "Protocol",
}
#: The longest value kept: longer is no instrument's name, so it's left out.
MAX_VALUE = 255
MAX_STATED_IN = 200
#: The SAM/BAM specification's standard @RG tags (section 1.3), in its order.
READ_GROUP_TAGS = (
    "ID",
    "BC",
    "CN",
    "DS",
    "DT",
    "FO",
    "KS",
    "LB",
    "PG",
    "PI",
    "PL",
    "PM",
    "PU",
    "SM",
)
#: The read groups' tags the Files list finds them by: not dates, flow orders or sizes.
_SEARCHED_TAGS = ("ID", "SM", "LB", "PU", "PM", "PL", "CN", "BC", "DS", "PG")
MAX_READ_GROUPS = 512
#: A description (DS) can be a sentence or two.
MAX_READ_GROUP_VALUE = 1024
#: What the Files list searches of one file's facts and read groups, at most.
MAX_SEARCH = 64 * 1024


@dataclass(frozen=True)
class Fact:
    """A value the file states, and where in the file it states it."""

    value: str
    stated_in: str


def clean_value(value: str, limit: int = MAX_VALUE) -> str | None:
    """The value without surrounding space, or None when it's no value a person could
    read: empty, too long, or with control characters or undecodable bytes in it."""
    value = value.strip()
    if not value or len(value) > limit or not value.isprintable() or "�" in value:
        return None
    return value


class Facts:
    """Facts as a reader finds them. The first statement of each is kept, so a reader adds
    them in the order the format says to prefer them."""

    def __init__(self) -> None:
        self._found: dict[str, Fact] = {}

    def add(self, key: str, value: str | None, stated_in: str) -> None:
        if key not in FACTS:
            raise KeyError(key)
        if key in self._found or value is None:
            return
        cleaned = clean_value(value)
        if cleaned is not None:
            self._found[key] = Fact(cleaned, stated_in)

    def found(self) -> dict[str, Fact]:
        return dict(self._found)


def stored(facts: Mapping[str, Fact]) -> dict[str, dict[str, str]]:
    """Facts as the file keeps them, in :data:`FACTS` order."""
    return {
        key: {"value": facts[key].value, "stated_in": facts[key].stated_in}
        for key in FACTS
        if key in facts
    }


def clean_stored(data: object) -> dict[str, dict[str, str]]:
    """Kept facts from elsewhere (a bundle's), checked as a reader's are. Raises
    ValueError for anything else."""
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("A file's facts are an object.")
    facts: dict[str, Fact] = {}
    for key, fact in cast("dict[object, object]", data).items():
        if key not in FACTS or not isinstance(fact, dict) or set(fact) != {"value", "stated_in"}:
            raise ValueError(f"A file's fact isn't one Iki keeps: {key!r}.")
        value, stated_in = fact["value"], fact["stated_in"]
        if not isinstance(value, str) or not isinstance(stated_in, str):
            raise ValueError(f"A file's {key} fact is text.")
        cleaned = clean_value(value)
        where = clean_value(stated_in, MAX_STATED_IN)
        if cleaned is None or where is None or cleaned != value or where != stated_in:
            raise ValueError(f"A file's {key} fact isn't a value Iki keeps.")
        facts[str(key)] = Fact(cleaned, where)
    return stored(facts)


def read_group(tags: Iterable[tuple[str, str]]) -> dict[str, str] | None:
    """One @RG line's standard tags, or None when it can't be kept as the file gives it: no
    ID, a tag twice, or a value too long, blank or with control characters. Tags the
    specification doesn't define (lowercase ones are anyone's) are left out."""
    group: dict[str, str] = {}
    for tag, value in tags:
        if tag not in READ_GROUP_TAGS:
            continue
        if tag in group or clean_value(value, MAX_READ_GROUP_VALUE) != value:
            return None
        group[tag] = value
    if "ID" not in group:
        return None
    return {tag: group[tag] for tag in READ_GROUP_TAGS if tag in group}


def read_groups(groups: Iterable[dict[str, str] | None]) -> list[dict[str, str]]:
    """All the read groups, or none: an empty list when any can't be kept, two share an ID
    (the specification says IDs are unique) or there are more than :data:`MAX_READ_GROUPS`."""
    kept: list[dict[str, str]] = []
    ids: set[str] = set()
    for group in groups:
        if group is None or group["ID"] in ids or len(kept) == MAX_READ_GROUPS:
            return []
        ids.add(group["ID"])
        kept.append(group)
    return kept


def clean_read_groups(data: object) -> list[dict[str, str]]:
    """Read groups from elsewhere (a bundle's), checked as a reader's are."""
    if data is None:
        return []
    if not isinstance(data, list):
        raise ValueError("A file's read groups are a list.")
    groups: list[dict[str, str] | None] = []
    for group in cast("list[object]", data):
        if not isinstance(group, dict) or not all(
            isinstance(k, str) and isinstance(v, str)
            for k, v in cast("dict[object, object]", group).items()
        ):
            raise ValueError("A read group is an object of text.")
        tags = cast("dict[str, str]", group)
        if set(tags) - set(READ_GROUP_TAGS):
            raise ValueError("A read group has only the SAM specification's @RG tags.")
        groups.append(read_group(tags.items()))
    kept = read_groups(groups)
    if len(kept) != len(groups):
        raise ValueError("The read groups aren't ones Iki keeps.")
    return kept


def search_text(facts: Mapping[str, Mapping[str, str]], groups: Iterable[Mapping[str, str]]) -> str:
    """What the Files list finds the file by: each fact's value, and the read groups'
    names (each once), up to :data:`MAX_SEARCH` characters."""
    values = [fact["value"] for fact in facts.values()]
    values += [group[tag] for group in groups for tag in _SEARCHED_TAGS if tag in group]
    text: list[str] = []
    size = 0
    for value in dict.fromkeys(values):
        if size + len(value) + 1 > MAX_SEARCH:
            break
        text.append(value)
        size += len(value) + 1
    return "\n".join(text)


# XML, as instruments write it: no DTD, read in a bounded number of bytes.


class XmlError(ValueError):
    """The XML isn't what a format defines, or it tries what an instrument's never does."""


@dataclass
class Element:
    """An element kept from a document, by its local name: its attributes (by local name
    too), its kept children and its text, up to a few kilobytes."""

    name: str
    namespace: str
    attributes: dict[str, str]
    children: list[Element] = field(default_factory=list)
    text: str = ""

    def all(self, name: str) -> list[Element]:
        return [child for child in self.children if child.name == name]

    def one(self, name: str) -> Element | None:
        """The one child of that name, or None when there's none or more than one."""
        found = self.all(name)
        return found[0] if len(found) == 1 else None

    def text_of(self, name: str) -> str | None:
        child = self.one(name)
        return child.text if child is not None else None


#: Text past this in one element is no value Iki keeps; it's cut so it can't fill memory.
_MAX_TEXT = 4 * 1024
_MAX_DEPTH = 256
_MAX_KEPT = 20_000


class _Stopped(Exception):  # noqa: N818 - control flow, not an error
    pass


def parse_xml(
    chunks: Iterable[bytes],
    keep: Callable[[tuple[str, ...]], bool],
    *,
    stop: Callable[[tuple[str, ...], bool], bool] | None = None,
) -> tuple[Element | None, bool]:
    """Parse ``chunks`` of a document, keeping the elements whose path of local names from
    the root ``keep`` accepts (an element is kept only under a kept parent). ``stop`` is
    asked at each element's start (True) and end (False); parsing ends there when it says
    so. Returns the kept root and whether it stopped.

    A DTD is refused outright, so no entity can expand or reach outside the document;
    so is nesting deeper than any format's, and keeping more than a format needs."""
    parser = expat.ParserCreate(namespace_separator=" ")
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    path: list[str] = []
    stack: list[Element | None] = []
    root: list[Element] = []
    kept = 0

    def refuse(*_: object) -> None:
        raise XmlError("The document declares a DTD.")

    def local(name: str) -> tuple[str, str]:
        namespace, _, rest = name.rpartition(" ")
        return namespace, rest

    def start(name: str, attributes: dict[str, str]) -> None:
        nonlocal kept
        namespace, tag = local(name)
        path.append(tag)
        if len(path) > _MAX_DEPTH:
            raise XmlError("The document nests deeper than any format does.")
        here = tuple(path)
        halt = stop is not None and stop(here, True)
        parent = stack[-1] if stack else None
        element: Element | None = None
        if (parent is not None or not stack) and (halt or keep(here)):
            kept += 1
            if kept > _MAX_KEPT:
                raise XmlError("The document has more of what's read than any format does.")
            element = Element(tag, namespace, {local(k)[1]: v for k, v in attributes.items()})
            if parent is not None:
                parent.children.append(element)
            else:
                root.append(element)
        stack.append(element)
        if halt:
            raise _Stopped

    def end(_: str) -> None:
        if stop is not None and stop(tuple(path), False):
            raise _Stopped
        stack.pop()
        path.pop()

    def text(data: str) -> None:
        element = stack[-1] if stack else None
        if element is not None and len(element.text) <= _MAX_TEXT:
            element.text = (element.text + data)[: _MAX_TEXT + 1]

    parser.StartDoctypeDeclHandler = refuse
    parser.EntityDeclHandler = refuse
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = text
    try:
        for chunk in chunks:
            parser.Parse(chunk, False)
        parser.Parse(b"", True)
    except _Stopped:
        return (root[0] if root else None), True
    except expat.ExpatError as error:
        raise XmlError(str(error)) from None
    return (root[0] if root else None), False
