"""OCR + AI cross-check for photos and scans.

Two independent readers look at the same page image:

* OCR (RapidOCR, local, no API key) - reads print almost perfectly, handwriting
  roughly;
* Groq vision (llm.resolve_page_image) - reads handwriting far better, but is a
  model, and a model can produce a clean-looking figure that is not on the page.

Neither is trusted alone. Each AI row is looked up in the OCR text:

* OCR finds the same ingredient with the same figure -> row stays "ok", noted
  as confirmed;
* OCR finds the ingredient with a *different* figure -> the quantity is blanked
  and the row flagged, both readings quoted. Two readers disagreeing is an
  unsettled question, and an unsettled question must not reach the Store as an
  indent (see CLAUDE.md, "never guessed");
* OCR cannot find the line -> the AI reading is kept as before, noted as
  unconfirmed.

With no Groq key (or a Groq failure) the OCR lines go through the ordinary
rules parser, so a photo of a printed recipe still imports offline.

Only image files take this path. Everything else is handed to
cli.build_recipes unchanged, and this module adds to the importer without
altering any existing function. RapidOCR is imported lazily: without it the
module falls back to extract.run_paddle_ocr, and then to AI-only.

Try it on one file:

    python -m recipe_import.ocr_verify recipe_import/sample/sample.png
"""

from __future__ import annotations

import difflib
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

from recipe_import import cli, llm
from recipe_import import extract as extract_mod
from recipe_import.normalise import normalise_text, resolve_unit, suggest_known_name, title_case
from recipe_import.parse import parse_recipe_lines

# A name counts as found in an OCR line at or above this similarity. OCR on
# handwriting misspells ("Cabbuge", "anion"), so exact matching finds nothing.
NAME_MATCH_CUTOFF = 0.75

# Quantities compared in base units; float tolerance only.
_QTY_TOLERANCE = 1e-6
_TO_BASE = {"kg": ("g", 1000.0), "litre": ("ml", 1000.0)}

_TOKEN = re.compile(r"[A-Za-z]+|\d+(?:\.\d+)?")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


# --------------------------------------------------------------------------
# OCR
# --------------------------------------------------------------------------

_ENGINE = None


def ocr_available():
    try:
        import rapidocr  # noqa: F401
        return True
    except ImportError:
        return False


def run_ocr(image_bytes):
    """Return (lines, mean_confidence). ([], None) when no OCR is installed.

    RapidOCR returns one box per text fragment; a handwritten "Atta - 6kg" often
    arrives as two boxes. Boxes are regrouped into visual lines so a name and its
    figure end up on the same line again.
    """
    global _ENGINE
    try:
        from rapidocr import RapidOCR
    except ImportError:
        return extract_mod.run_paddle_ocr(image_bytes)

    if _ENGINE is None:
        _ENGINE = RapidOCR()

    result = _ENGINE(image_bytes)
    if not result or result.txts is None:
        return [], None

    boxes = []
    for box, text, score in zip(result.boxes, result.txts, result.scores):
        # Bullets come back as junk glyphs ("� Paneer"); drop leading symbols.
        text = re.sub(r"^[^\w(]+", "", normalise_text(text))
        if not text:
            continue
        ys = [p[1] for p in box]
        xs = [p[0] for p in box]
        boxes.append((min(ys), max(ys), min(xs), text, float(score)))

    lines = _group_into_lines(boxes)
    scores = [b[4] for b in boxes]
    return lines, (sum(scores) / len(scores) if scores else None)


def _group_into_lines(boxes):
    """Join boxes whose vertical centres fall within half a box height."""
    if not boxes:
        return []
    heights = sorted(b[1] - b[0] for b in boxes)
    tolerance = max(1.0, heights[len(heights) // 2] / 2)

    rows = []
    for box in sorted(boxes, key=lambda b: (b[0] + b[1]) / 2):
        centre = (box[0] + box[1]) / 2
        if rows and abs(centre - rows[-1]["centre"]) <= tolerance:
            rows[-1]["boxes"].append(box)
        else:
            rows.append({"centre": centre, "boxes": [box]})

    return [
        " ".join(b[3] for b in sorted(row["boxes"], key=lambda b: b[2]))
        for row in rows
    ]


# --------------------------------------------------------------------------
# Cross-check
# --------------------------------------------------------------------------


def cross_check(ai_rows, ocr_lines):
    """Verify each AI row against the OCR text. Returns (rows, counts)."""
    counts = {"confirmed": 0, "conflict": 0, "unconfirmed": 0}
    for row in ai_rows:
        verdict = _verify_row(row, ocr_lines)
        counts[verdict] += 1
    return ai_rows, counts


def _verify_row(row, ocr_lines):
    if not row.name:
        return "unconfirmed"

    match = _find_in_ocr(row.name, ocr_lines)
    if match is None:
        row.note = _join(row.note, "OCR could not confirm this line")
        return "unconfirmed"

    line, candidates = match
    if not candidates or row.quantity is None:
        # Nothing to compare: the name is there, the figure is not legible to
        # OCR (or the AI left it blank). Not a disagreement.
        row.note = _join(row.note, f"OCR saw the name ('{line}') but no figure")
        return "unconfirmed"

    if any(_same_quantity(row.quantity, row.unit, q, u) for q, u in candidates):
        if row.is_ok:
            row.note = f"OCR and AI agree ('{line}') - please still glance at the document"
        return "confirmed"

    ocr_qty, ocr_unit_word = candidates[0]
    ai_reading = f"{_fmt(row.quantity)} {row.unit or ''}".strip()
    ocr_reading = f"{_fmt(ocr_qty)} {ocr_unit_word or ''}".strip()
    row.quantity = None
    row.status = "needs_qty"
    row.note = (
        f"OCR and AI disagree: AI read {ai_reading}, OCR read {ocr_reading} "
        f"('{line}') - check the document and enter the figure"
    )
    return "conflict"


def _find_in_ocr(name, ocr_lines):
    """Best OCR line containing `name`, with the figures either side of it.

    Returns (line, [(quantity, unit word or None), ...]) nearest first, or None
    if no line matches well enough.
    """
    name_words = re.findall(r"[a-z]+", name.casefold())
    if not name_words:
        return None
    target = " ".join(name_words)
    width = len(name_words)

    best = None  # (score, line, tokens, start, end)
    for line in ocr_lines:
        tokens = list(_TOKEN.finditer(line))
        words = [(i, t) for i, t in enumerate(tokens) if t.group().isalpha()]
        for k in range(len(words)):
            window = words[k : k + width]
            if len(window) < width:
                break
            candidate = " ".join(t.group().casefold() for _, t in window)
            score = difflib.SequenceMatcher(None, target, candidate).ratio()
            if score >= NAME_MATCH_CUTOFF and (best is None or score > best[0]):
                best = (score, line, tokens, window[0][0], window[-1][0])

    if best is None:
        return None

    _, line, tokens, start, end = best
    return line, _quantities_around(tokens, start, end)


def _quantities_around(tokens, start, end):
    """The nearest figure before the name and the nearest after it.

    Both, because a list line has one ingredient's figure on each side of a
    name: "1kg roasted chana powder 500g corn flour". Which one belongs to the
    name depends on how the cook writes, so either may confirm the AI's figure.
    """
    before, after = None, None
    for i, token in enumerate(tokens):
        if not _NUMBER.fullmatch(token.group()):
            continue
        # "Q1", "S00": a digit run glued onto letters is OCR noise, not a figure.
        if i > 0 and tokens[i - 1].end() == token.start() and tokens[i - 1].group().isalpha():
            continue
        if i < start:
            before = i
        elif i > end and after is None:
            after = i

    found = [i for i in (before, after) if i is not None]
    found.sort(key=lambda i: start - i if i < start else i - end)
    return [(float(tokens[i].group()), _unit_after(tokens, i)) for i in found]


def _unit_after(tokens, i):
    # Only a word glued to the number or one space away, not a word further on.
    if i + 1 < len(tokens) and tokens[i + 1].group().isalpha():
        if tokens[i + 1].start() - tokens[i].end() <= 1:
            return tokens[i + 1].group()
    return None


def _same_quantity(ai_qty, ai_unit, ocr_qty, ocr_unit_word):
    ocr_unit = None
    if ocr_unit_word:
        # "gkas" (OCR ran "500g kas kas" together) -> try the leading letters.
        for cut in range(len(ocr_unit_word), 0, -1):
            ocr_unit, _ = resolve_unit(ocr_unit_word[:cut])
            if ocr_unit:
                break

    if ai_unit and ocr_unit:
        a_unit, a_qty = _to_base(ai_qty, ai_unit)
        o_unit, o_qty = _to_base(ocr_qty, ocr_unit)
        if a_unit == o_unit:
            return abs(a_qty - o_qty) <= _QTY_TOLERANCE
    # OCR unit unreadable ("3ks", "5lkg"): compare the figure alone.
    return abs(ai_qty - ocr_qty) <= _QTY_TOLERANCE


def _to_base(qty, unit):
    base, factor = _TO_BASE.get(unit, (unit, 1.0))
    return base, qty * factor


def _fmt(value):
    return f"{value:g}"


def _join(note, extra):
    return f"{note}; {extra}" if note else extra


# --------------------------------------------------------------------------
# Entry point used by the web import route
# --------------------------------------------------------------------------


def build_recipes(paths, use_llm=True, known_names=(), collect_warnings=None):
    """Drop-in for cli.build_recipes that cross-checks images with OCR.

    Non-image documents go to cli.build_recipes untouched.
    """
    warnings = collect_warnings if collect_warnings is not None else []
    recipes = []
    for path in paths:
        ext = os.path.splitext(path)[1].lower()
        if ext in extract_mod.IMAGE_EXTENSIONS:
            recipes.append(_read_image(path, use_llm, known_names, warnings))
        else:
            recipes.extend(
                cli.build_recipes(
                    [path], use_llm=use_llm, known_names=known_names,
                    collect_warnings=warnings,
                )
            )
    return recipes


def _read_image(path, use_llm, known_names, warnings):
    with open(path, "rb") as fh:
        image_bytes = fh.read()
    filename = os.path.basename(path)
    ai_ready = use_llm and llm.is_available()

    # Both readers run at once: a phone photo takes each of them several
    # seconds, and the staff member is waiting on the upload page.
    with ThreadPoolExecutor(max_workers=2) as pool:
        ocr_future = pool.submit(_safe_ocr, image_bytes, warnings)
        ai_future = pool.submit(llm.resolve_page_image, image_bytes) if ai_ready else None
        ocr_lines, ocr_conf = ocr_future.result()
        vision, ai_error = None, None
        if ai_future is not None:
            try:
                vision = ai_future.result()
            except Exception as exc:  # the AI is a bonus, never a dependency
                ai_error = exc

    if ai_error is not None:
        warnings.append(f"Groq could not read this page: {ai_error}")
    elif not ai_ready:
        warnings.append(
            "AI is off or not configured (GROQ_API_KEY), so this photo was read "
            "by OCR alone. OCR handles print well and handwriting poorly - check "
            "every row."
        )

    dish_name, base_persons = "", ""
    if vision is not None and vision.rows:
        rows, counts = cross_check(vision.rows, ocr_lines)
        dish_name, base_persons = vision.dish_name, vision.base_persons
        if ocr_lines:
            warnings.append(
                f"OCR cross-check: {counts['confirmed']} row(s) confirmed, "
                f"{counts['conflict']} disagreement(s) flagged, "
                f"{counts['unconfirmed']} not confirmable - check those against the photo."
            )
        else:
            warnings.append(
                "OCR is not installed or read nothing, so AI figures were not "
                "cross-checked. Install it with: pip install -r requirements-import.txt"
            )
    else:
        if vision is not None:
            warnings.append("Groq vision returned no ingredient rows; using OCR text.")
        rows = []
        for raw in extract_mod.split_recipes(ocr_lines):
            dish_name = dish_name or raw.dish_name
            base_persons = base_persons or raw.base_persons
            rows.extend(parse_recipe_lines(raw.lines))
        for row in rows:
            row.source = "ocr"
            row.note = _join(row.note, "read by OCR only - check against the document")
        if ocr_conf is not None and ocr_conf < llm.OCR_CONFIDENCE_FLOOR:
            warnings.append(
                f"OCR confidence is low ({ocr_conf:.0%}); this looks handwritten. "
                "Groq vision reads handwriting far better."
            )

    # Same finishing as cli.build_recipes.
    for row in rows:
        row.suggested_name = suggest_known_name(row.name, known_names)
        if row.name.isascii():
            row.name = title_case(row.name)

    if not rows:
        warnings.append(
            f"No ingredients could be read from '{filename}'. Try a clearer photo "
            "(full page, good light, less glare) or type the recipe by hand."
        )

    return cli.ReviewRecipe(
        dish_name=dish_name or cli._dish_name_from_file(filename),
        base_persons=base_persons,
        rows=rows,
    )


def _safe_ocr(image_bytes, warnings):
    try:
        return run_ocr(image_bytes)
    except Exception as exc:  # OCR is a check, not a gate
        warnings.append(f"OCR failed on this page: {exc}")
        return [], None


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("usage: python -m recipe_import.ocr_verify IMAGE [IMAGE ...]")
        return 2
    warnings = []
    for recipe in build_recipes(argv, collect_warnings=warnings):
        print(f"\n{recipe.dish_name}  (persons: {recipe.base_persons or '?'})")
        for row in recipe.rows:
            qty = _fmt(row.quantity) if row.quantity is not None else "-"
            print(f"  [{row.status:10}] {row.name:28} {qty:>7} {row.unit or '':7} {row.note}")
    for note in warnings:
        print(f"\n! {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
