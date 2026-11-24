"""Identity spelling of the declared neutral atoms lost in a conjugate attachment."""

import re
from collections import Counter

ATOM = r"(?:\[([1-9][0-9]*)([A-Z][a-z]?)\]|([A-Z][a-z]?))([1-9][0-9]*)?"
TOKEN = re.compile(ATOM)
LOSS_PATTERN = r"^(none|((\[[1-9][0-9]*[A-Z][a-z]?\]|[A-Z][a-z]?)([1-9][0-9]*)?)+)$"


def canonical_loss(text: str) -> str:
    """Collect equivalent atom counts in Hill order, keeping each isotope with its element."""
    if text == "none":
        return text
    counts: Counter[tuple[str, int]] = Counter()
    position = 0
    while position < len(text):
        token = TOKEN.match(text, position)
        if token is None:
            raise ValueError(f"Expected a neutral elemental loss formula, received {text!r}.")
        mass, labelled, unlabelled, count = token.groups()
        element, isotope = labelled or unlabelled, int(mass or 0)
        if unlabelled in ("D", "T"):
            element, isotope = "H", 2 if unlabelled == "D" else 3
        counts[element, isotope] += int(count or 1)
        position = token.end()
    if not counts:
        raise ValueError("An elemental loss needs a formula or none.")

    has_carbon = any(element == "C" for element, _ in counts)
    priority = {"C": 0, "H": 1} if has_carbon else {}
    terms = []
    for element, isotope in sorted(counts, key=lambda atom: (priority.get(atom[0], 2), *atom)):
        symbol = element
        if isotope:
            symbol = {("H", 2): "D", ("H", 3): "T"}.get((element, isotope), f"[{isotope}{element}]")
        count = counts[element, isotope]
        terms.append(symbol if count == 1 else f"{symbol}{count}")
    return "".join(terms)
