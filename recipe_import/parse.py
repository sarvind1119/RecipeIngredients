"""The rules parser: one written line -> zero or more ingredient rows.

Pure text work - no Flask, no sqlite3, no anthropic - so every rule below is
directly unit-testable against the department's real wording. This is the module
that decides what a requisition will ask the Store for, so it is the one that
has to be right, and the one that must never invent a figure.

A fragment it cannot resolve comes back flagged, with the words the mess
actually wrote attached, for a human to settle.
"""

import re
from dataclasses import dataclass, field

from recipe_import.normalise import (
    UNIT_WORDS,
    clean_name,
    normalise_text,
    resolve_unit,
)

ROW_STATUSES = ("ok", "needs_qty", "needs_unit", "needs_name", "duplicate")

# Lines that are structure, not ingredients.
_SECTION_HEADERS = {
    "ingredients", "ingredient", "method", "preparation", "procedure",
    "directions", "steps", "for the garnish", "garnish", "notes",
}

# Phrases that mean "the cook judges this amount". They always belong to an
# ingredient of their own, which is what lets the run-on splitter below find the
# boundary in "30 g deggi mirch Salt to taste".
_QUANTITYLESS_MARKERS = ("to taste", "for garnish", "to garnish", "as required", "as needed")

_QTY_LEADING = re.compile(r"^(?P<qty>\d+(?:\.\d+)?)\s*(?P<rest>.*)$", re.DOTALL)
_QTY_TRAILING = re.compile(r"^(?P<name>.+?)\s+(?P<qty>\d+(?:\.\d+)?)\s*(?P<unit>[A-Za-z.]*)$")
_UNIT_HEAD = re.compile(
    r"^(?P<unit>" + "|".join(re.escape(w) for w in UNIT_WORDS) + r")\b\.?\s*(?P<rest>.*)$",
    re.IGNORECASE | re.DOTALL,
)

# Split on commas and conjunctions. Whether a fragment is a new ingredient or a
# prep note trailing the previous one is decided in _is_prep_note, not here.
_FRAGMENT_SPLIT = re.compile(r"\s*,\s*(?:and\s+)?|\s+and\s+", re.IGNORECASE)


@dataclass
class ParsedRow:
    """One candidate line on a requisition, resolved or flagged."""

    name: str = ""
    quantity: float | None = None
    unit: str | None = None
    status: str = "ok"
    note: str = ""
    source_line: str = ""
    source: str = "rules"
    suggested_name: str = field(default="")

    @property
    def is_ok(self):
        return self.status == "ok"


def is_section_header(line):
    stripped = normalise_text(line).strip(" :.-").casefold()
    return stripped in _SECTION_HEADERS


def parse_recipe_lines(lines):
    """Parse a recipe's ingredient lines and flag names that repeat.

    Duplicates are caught here rather than at insert time because
    dish_ingredients carries UNIQUE (dish_id, ingredient_id) - two rows for
    "Onion" would fail the insert after the reviewer had already signed the
    sheet off. Better to ask which figure is right while they are still looking.
    """
    rows = []
    for line in lines:
        if is_section_header(line):
            continue
        rows.extend(parse_ingredient_line(line))

    counts = {}
    for row in rows:
        if row.name:
            counts[row.name.casefold()] = counts.get(row.name.casefold(), 0) + 1

    for row in rows:
        if row.name and counts[row.name.casefold()] > 1:
            row.status = "duplicate"
            row.note = _join_note(row.note, f"'{row.name}' appears more than once in this recipe")

    return rows


def parse_ingredient_line(line):
    """Parse one written line into rows. Returns [] for structure or noise."""
    text = normalise_text(line)
    if not text or is_section_header(text):
        return []

    rows = []
    for fragment in split_fragments(text):
        rows.extend(_parse_fragment(fragment, source_line=text))
    return rows


def split_fragments(text):
    """Break a line into one fragment per ingredient.

    The department writes both "1kg Paneer, cut into triangle shape" (one
    ingredient plus a prep note) and "500g chopped onion, 250g chopped tomatoes"
    (two ingredients), with identical punctuation. Splitting on every comma
    would invent an ingredient called "cut into triangle shape"; splitting on
    none would bury the tomatoes inside the onion's name.

    The discriminator is what survives prep-word stripping. "cut into triangle
    shape" reduces to nothing, so it is a note on the fragment before it.
    "salt to taste" reduces to "salt", so it is an ingredient in its own right -
    which is what keeps salt on the requisition instead of losing it to the
    previous line's name.
    """
    text = _strip_trailing_conjunction(text)
    parts = [p.strip() for p in _FRAGMENT_SPLIT.split(text) if p and p.strip()]
    if not parts:
        return []

    fragments = [parts[0]]
    for part in parts[1:]:
        if _is_prep_note(part):
            fragments[-1] = f"{fragments[-1]}, {part}"
        else:
            fragments.append(part)
    return fragments


def _is_prep_note(fragment):
    """True when nothing is left of the fragment once prep words are removed."""
    return not clean_name(fragment)


def _strip_trailing_conjunction(text):
    return re.sub(r"[\s,]*\b(and|&)\b[\s,.]*$", "", text.strip(" ,.;"), flags=re.IGNORECASE)


def _parse_fragment(fragment, source_line):
    fragment = fragment.strip(" ,.;:")
    if not fragment:
        return []

    leading = _QTY_LEADING.match(fragment)
    if leading:
        return _build_rows(
            qty_text=leading.group("qty"),
            remainder=leading.group("rest"),
            source_line=source_line,
        )

    trailing = _QTY_TRAILING.match(fragment)
    if trailing:
        return _build_rows(
            qty_text=trailing.group("qty"),
            remainder=f"{trailing.group('unit')} {trailing.group('name')}".strip(),
            source_line=source_line,
        )

    # No number anywhere - "salt to taste", "Fresh coriander leaves for garnish".
    name = clean_name(fragment)
    if not name:
        return []
    return [
        ParsedRow(
            name=name,
            status="needs_qty",
            note="no quantity written - enter what the mess actually issues",
            source_line=source_line,
        )
    ]


def _build_rows(qty_text, remainder, source_line):
    quantity = float(qty_text)
    remainder = remainder.lstrip(" -–—")

    unit, unit_note, rest = _peel_unit(remainder)
    head, tail_row = _split_run_on(rest, source_line)

    name = clean_name(head)
    rows = []

    if not name:
        rows.append(
            ParsedRow(
                quantity=quantity,
                unit=unit,
                status="needs_name",
                note=_join_note(unit_note, "quantity written with no ingredient name"),
                source_line=source_line,
            )
        )
    elif unit is None:
        rows.append(
            ParsedRow(
                name=name,
                quantity=quantity,
                unit=None,
                status="needs_unit",
                note=_join_note(unit_note, "choose the unit the Store issues this in"),
                source_line=source_line,
            )
        )
    else:
        rows.append(
            ParsedRow(
                name=name,
                quantity=quantity,
                unit=unit,
                status="ok",
                source_line=source_line,
            )
        )

    if tail_row is not None:
        rows.append(tail_row)
    return rows


def _peel_unit(remainder):
    """Take a unit word off the front of the text after the number.

    Returns (unit, note, rest). An unrecognised leading word is *not* consumed -
    in "50 Chopped garlic" the mess simply omitted the unit, and swallowing
    "Chopped" as one would lose a word from the ingredient's name on top of
    getting the unit wrong.
    """
    match = _UNIT_HEAD.match(remainder)
    if not match:
        return None, "no unit written", remainder

    unit, note = resolve_unit(match.group("unit"))
    if unit is None:
        # Recognised word, but not one the Store can issue against ("2-inch").
        return None, note, match.group("rest")
    return unit, "", match.group("rest")


def _split_run_on(text, source_line):
    """Separate two ingredients that were typed onto one line without a comma.

    "30 g deggi mirch Salt to taste" is 30 g of deggi mirch plus salt, not an
    ingredient called "deggi mirch Salt". The give-away is a judged-amount
    marker at the end with a capitalised word before it: that capital is where
    the second ingredient starts. Without a capital ("30 g salt to taste") there
    is only one ingredient and the line is left alone.
    """
    stripped = text.strip()
    lowered = stripped.casefold()

    marker = next((m for m in _QUANTITYLESS_MARKERS if lowered.endswith(m)), None)
    if marker is None:
        return text, None

    head = stripped[: len(stripped) - len(marker)].strip(" ,.;-")
    words = head.split()
    if len(words) < 2:
        return text, None

    boundary = next(
        (i for i in range(len(words) - 1, 0, -1) if words[i][:1].isupper()), None
    )
    if boundary is None:
        return text, None

    second = clean_name(" ".join(words[boundary:]))
    if not second:
        return text, None

    tail = ParsedRow(
        name=second,
        status="needs_qty",
        note=f"written as '{second} {marker}' on the same line - enter the issued quantity",
        source_line=source_line,
    )
    return " ".join(words[:boundary]), tail


def _join_note(*parts):
    return "; ".join(p for p in parts if p)
