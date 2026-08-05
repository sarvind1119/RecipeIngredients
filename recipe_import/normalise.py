"""Vocabulary mapping: what the mess writes -> what the schema accepts.

Pure text work. Imports neither Flask, sqlite3 nor anthropic, so every mapping
rule here is directly testable - the same discipline scaling.py follows, and for
the same reason: this is where a wrong answer becomes a wrong indent.
"""

import difflib
import re
import unicodedata

from scaling import UNITS

# Every spelling seen in the department's documents, mapped onto scaling.UNITS.
# Extend this table rather than teaching the parser to guess.
_UNIT_ALIASES = {
    "g": "g", "gm": "g", "gms": "g", "gram": "g", "grams": "g", "gr": "g",
    "kg": "kg", "kgs": "kg", "kilo": "kg", "kilos": "kg", "kilogram": "kg",
    "kilograms": "kg",
    "ml": "ml", "mls": "ml", "millilitre": "ml", "milliliter": "ml",
    "l": "litre", "lt": "litre", "ltr": "litre", "ltrs": "litre", "lit": "litre",
    "litre": "litre", "litres": "litre", "liter": "litre", "liters": "litre",
    "no": "nos", "nos": "nos", "pc": "nos", "pcs": "nos", "piece": "nos",
    "pieces": "nos", "each": "nos",
    "pkt": "packet", "pkts": "packet", "packet": "packet", "packets": "packet",
    "pack": "packet",
    "bunch": "bunch", "bunches": "bunch", "gaddi": "bunch",
    "tsp": "tsp", "tsps": "tsp", "teaspoon": "tsp", "teaspoons": "tsp",
    "tbsp": "tbsp", "tbsps": "tbsp", "tablespoon": "tbsp",
    "tablespoons": "tbsp", "tbs": "tbsp",
}

# Units the department uses that the Store cannot issue against. Recognised on
# purpose - so the row can be flagged with a useful note instead of the name
# swallowing the word "inch" and the row looking merely unitless.
UNCONVERTIBLE_UNITS = {
    "inch": "measured by length",
    "inches": "measured by length",
    "pinch": "measured by hand",
    "pinches": "measured by hand",
    "handful": "measured by hand",
    "cup": "no agreed cup size",
    "cups": "no agreed cup size",
    "glass": "no agreed glass size",
    "sprig": "no agreed sprig weight",
    "sprigs": "no agreed sprig weight",
}

# Longest first, so "tablespoon" is not matched as "tbs" + "poon".
UNIT_WORDS = sorted(
    set(_UNIT_ALIASES) | set(UNCONVERTIBLE_UNITS), key=len, reverse=True
)

# Prep notes that trail an ingredient name. Stripped so "Onion, finely chopped"
# and "Onion" resolve to one master ingredient rather than two.
_PREP_SUFFIXES = [
    "finely chopped", "roughly chopped", "coarsely chopped", "chopped",
    "for garnish", "to garnish", "garnish",
    "cut into triangle shape", "cut into cubes", "cut into pieces",
    "to taste", "as required", "as needed",
    "divided", "slit", "slitted", "grated", "sliced", "diced", "julienned",
    "washed", "soaked", "peeled", "deseeded", "crushed", "ground", "boiled",
    "optional",
]

_PREP_PREFIXES = ["chopped", "finely chopped", "grated", "sliced", "diced", "crushed"]


def normalise_text(text):
    """Flatten the typographic noise Word leaves behind.

    Non-breaking spaces and vulgar fractions both arrive from the department's
    documents. A NBSP that survives here makes "1 kg" fail a plain-space regex;
    an unhandled U+00BD makes "half a teaspoon" parse as no quantity at all.
    """
    if not text:
        return ""

    text = unicodedata.normalize("NFKC", text)
    text = text.replace(" ", " ").replace(" ", " ").replace(" ", " ")

    # Vulgar fractions: NFKC turns U+00BD into "1⁄2", which is still not a
    # number. Resolve any remaining fraction character to its decimal value.
    out = []
    for ch in text:
        value = unicodedata.numeric(ch, None)
        if value is not None and not ch.isdigit() and not ch.isascii():
            out.append(_trim_number(value))
        else:
            out.append(ch)
    text = "".join(out)

    # "1⁄2" (NFKC's output) and "1/2" both mean 0.5 to a cook.
    text = re.sub(
        r"(?<!\d)(\d+)\s*[/⁄]\s*(\d+)(?!\d)",
        lambda m: _trim_number(int(m.group(1)) / int(m.group(2))),
        text,
    )

    text = re.sub(r"[\s​]+", " ", text)
    return text.strip()


def _trim_number(value):
    return f"{value:.4f}".rstrip("0").rstrip(".")


def resolve_unit(raw):
    """Map a written unit onto scaling.UNITS.

    Returns (unit, note). `unit` is None when the word cannot be honoured, with
    `note` explaining why - never a guessed substitute. Converting "2-inch
    ginger" into a weight means inventing a figure the mess never wrote down.
    """
    if not raw:
        return None, "no unit given"

    word = raw.strip().lower().rstrip(".").rstrip("s.")
    exact = raw.strip().lower().rstrip(".")

    for candidate in (exact, word):
        if candidate in _UNIT_ALIASES:
            return _UNIT_ALIASES[candidate], ""
        if candidate in UNCONVERTIBLE_UNITS:
            return None, f"'{raw.strip()}' cannot be indented ({UNCONVERTIBLE_UNITS[candidate]})"

    return None, f"'{raw.strip()}' is not a unit the Store issues"


def clean_name(raw):
    """Trim an ingredient name down to the thing the Store actually issues.

    Strips prep notes but never corrects spelling. "R.oil" stays "R.oil" - the
    reviewer decides whether it means Refined Oil, because a parser that
    silently rewrites names would rewrite the wrong one eventually.
    """
    name = normalise_text(raw)
    name = name.strip(" ,.;:-–—")

    changed = True
    while changed:
        changed = False
        lowered = name.lower()
        for suffix in _PREP_SUFFIXES:
            if lowered.endswith(" " + suffix) or lowered == suffix:
                name = name[: len(name) - len(suffix)].strip(" ,.;:-")
                changed = True
                break
        for prefix in _PREP_PREFIXES:
            if lowered.startswith(prefix + " ") and len(name) > len(prefix) + 1:
                name = name[len(prefix) :].strip(" ,.;:-")
                changed = True
                break

    name = re.sub(r"\s+", " ", name).strip(" ,.;:-")
    return name


def title_case(name):
    """Sentence-style capitalisation, leaving all-caps words (R.OIL) alone."""
    if not name:
        return name
    words = [w if w.isupper() and len(w) > 1 else w.capitalize() for w in name.split(" ")]
    return " ".join(words)


def suggest_known_name(name, known_names, cutoff=0.82):
    """Best fuzzy match against the existing master ingredient list.

    A suggestion only - it lands in its own review column and is never applied
    automatically. Its job is to stop "R.oil" becoming a second master row
    alongside "Refined Oil", which is exactly what would defeat the COLLATE
    NOCASE uniqueness that cross-dish consolidation will depend on.
    """
    if not name or not known_names:
        return ""

    lookup = {n.casefold(): n for n in known_names}
    if name.casefold() in lookup:
        return ""  # already an exact match; nothing to suggest

    matches = difflib.get_close_matches(name.casefold(), list(lookup), n=1, cutoff=cutoff)
    return lookup[matches[0]] if matches else ""


def default_unit_for(unit):
    """The unit a newly created master ingredient should carry."""
    return unit if unit in UNITS else "kg"
