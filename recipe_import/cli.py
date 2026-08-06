"""Command line: extract documents to a review sheet, then load the sheet.

    python -m recipe_import extract SampleIngredient.docx -o review.csv
    python -m recipe_import load review.csv

Two verbs on purpose. A human sits between them, because the documents never
state how many persons a recipe serves and every scaled figure on a requisition
is divided by that number.
"""

import argparse
import glob
import os
import re
import sys
from dataclasses import dataclass, field

import db
from recipe_import import extract as extract_mod
from recipe_import import llm, review
from recipe_import.normalise import (
    clean_name,
    default_unit_for,
    resolve_unit,
    suggest_known_name,
    title_case,
)
from recipe_import.parse import ParsedRow, parse_recipe_lines
from scaling import DISH_CATEGORIES, validate_ingredient_rows

DEFAULT_CATEGORY = DISH_CATEGORIES[0]


@dataclass
class ReviewRecipe:
    dish_name: str = ""
    base_persons: str = ""
    category: str = DEFAULT_CATEGORY
    rows: list = field(default_factory=list)


# --------------------------------------------------------------------------
# extract
# --------------------------------------------------------------------------


def build_recipes(paths, use_llm=True, known_names=(), collect_warnings=None):
    """Read documents into review-ready recipes.

    If collect_warnings is a list, human-readable notes are appended (e.g. Groq
    skipped, empty vision result) so the web UI can flash them instead of
    silently showing an empty form.
    """
    recipes = []
    warnings = collect_warnings if collect_warnings is not None else []

    for path in paths:
        ext = os.path.splitext(path)[1].lower()
        is_image = ext in extract_mod.IMAGE_EXTENSIONS

        # Photos/scans need AI; without it Paddle is often missing and the form
        # would open empty — force the LLM path when Groq is configured.
        effective_llm = use_llm or (is_image and llm.is_available())

        for raw in extract_mod.extract(path):
            rows = (
                [_row_from_sheet(r) for r in raw.sheet_rows]
                if raw.sheet_rows
                else parse_recipe_lines(raw.lines)
            )
            dish_name = raw.dish_name
            base_persons = raw.base_persons
            had_page_image = bool(raw.page_images)

            if effective_llm:
                rows, dish_name, base_persons = _apply_llm_fallback(
                    raw, rows, dish_name, base_persons, warnings
                )
            elif had_page_image or is_image:
                warnings.append(
                    "This looks like a photo/scan. AI is off or not configured "
                    "(GROQ_API_KEY), so little or nothing may be read. "
                    "Enable AI assistance and set the key in recipe_import/.env."
                )

            for row in rows:
                row.suggested_name = suggest_known_name(row.name, known_names)
                # Do not title-case AI/Hindi names aggressively — title_case is
                # for Latin OCR crumbs; leave non-ASCII as transcribed.
                if row.name.isascii():
                    row.name = title_case(row.name)

            if (had_page_image or is_image) and not rows:
                warnings.append(
                    f"No ingredients could be read from '{os.path.basename(path)}'. "
                    "Try a clearer photo (full page, good light, less glare) or type the recipe by hand."
                )

            recipes.append(
                ReviewRecipe(
                    dish_name=dish_name or _dish_name_from_file(raw.source_file),
                    base_persons=base_persons,
                    category=DEFAULT_CATEGORY,
                    rows=rows,
                )
            )

    return recipes


def _row_from_sheet(record):
    """A spreadsheet row skips the line parser but not the validation."""
    name = clean_name(record.get("ingredient_name", ""))
    qty_raw = (record.get("quantity") or "").strip()
    unit_raw = (record.get("unit") or "").strip()

    # "2 kg" in the quantity cell, or unit column filled separately.
    quantity = None
    unit = None
    unit_note = "no unit stated in the document"

    if qty_raw:
        match = re.match(
            r"^(?P<qty>\d+(?:\.\d+)?)\s*(?P<unit>[A-Za-z.]+)?\s*$", qty_raw
        )
        if match:
            try:
                quantity = float(match.group("qty"))
            except ValueError:
                quantity = None
            if match.group("unit") and not unit_raw:
                unit_raw = match.group("unit")
        else:
            try:
                quantity = float(qty_raw)
            except ValueError:
                # Leave free text for the human — may be "to taste".
                quantity = None

    if unit_raw:
        unit, unit_note = resolve_unit(unit_raw)
    else:
        unit, unit_note = None, "no unit stated in the document"

    source_line = " ".join(
        v for k, v in record.items() if v and k != "category"
    )

    row = ParsedRow(
        name=name,
        quantity=quantity if quantity and quantity > 0 else None,
        unit=unit,
        source_line=source_line,
        source="sheet",
    )

    if not name:
        row.status, row.note = "needs_name", "no ingredient name in this row"
    elif row.quantity is None:
        row.status, row.note = "needs_qty", "quantity missing or not a number"
    elif row.unit is None:
        row.status, row.note = "needs_unit", unit_note
    return row


def _apply_llm_fallback(raw, rows, dish_name, base_persons, warnings=None):
    """Groq assist: vision-first for page images; text only for flagged lines.

    Photos/scans/handwriting keep the page image on `raw`. When Groq is
    available those pages are read by vision first — PaddleOCR alone is not
    enough for handwriting or messy phone photos. Digital Word/PDF text still
    uses the rules parser; only flagged residue is sent as text.
    """
    warnings = warnings if warnings is not None else []

    if not llm.is_available():
        if raw.page_images:
            warnings.append(
                "Groq AI is not available (missing GROQ_API_KEY or groq package). "
                "Photo/scan import needs it."
            )
        return rows, dish_name, base_persons

    try:
        if raw.page_images:
            # Vision-first whenever we have a page image (photo, scan, or
            # scanned PDF page). OCR lines are a fallback if vision is empty.
            vision = llm.resolve_page_image(raw.page_images[0])
            if vision.rows:
                if vision.dish_name and not dish_name:
                    dish_name = vision.dish_name
                if vision.base_persons and not base_persons:
                    base_persons = vision.base_persons
                return vision.rows, dish_name, base_persons

            # Vision returned nothing useful — keep OCR/rules rows if any.
            msg = "Groq vision returned no ingredient rows; kept any OCR text found."
            print(f"  {msg}", file=sys.stderr)
            warnings.append(msg)
            return rows, dish_name, base_persons

        flagged = [r for r in rows if not r.is_ok]
        if not flagged:
            return rows, dish_name, base_persons

        lines = list(dict.fromkeys(r.source_line for r in flagged if r.source_line))
        resolved = llm.resolve_lines(lines, dish_name=dish_name or raw.dish_name)
        if not resolved:
            return rows, dish_name, base_persons

        # Replace the flagged rows with the model's reading of the same lines,
        # keeping every row the rules parser already settled.
        replaced_lines = set(lines)
        kept = [r for r in rows if r.source_line not in replaced_lines or r.is_ok]
        return kept + resolved, dish_name, base_persons

    except Exception as exc:  # the fallback is a bonus, never a dependency
        msg = f"Groq could not read this page: {exc}"
        print(f"  {msg}", file=sys.stderr)
        warnings.append(msg)
        return rows, dish_name, base_persons


def _dish_name_from_file(source_file):
    stem = os.path.splitext(source_file or "")[0]
    return title_case(stem.replace("_", " ").replace("-", " ").strip())


def _known_ingredient_names():
    """Read the master ingredient list so near-duplicates can be suggested."""
    if not os.path.exists(db.DB_PATH):
        return []
    conn = db.connect()
    try:
        return [r["name"] for r in conn.execute("SELECT name FROM ingredients")]
    except Exception:
        return []
    finally:
        conn.close()


def cmd_extract(args):
    paths = _expand(args.paths)
    if not paths:
        print("No importable documents found.", file=sys.stderr)
        return 1

    if args.no_llm:
        print("AI fallback disabled (--no-llm).")
    elif not llm.is_available():
        print("Groq AI unavailable - photo/scan lines may stay empty or flagged.")
        print("  Set GROQ_API_KEY in recipe_import/.env and: pip install groq")

    recipes = build_recipes(
        paths, use_llm=not args.no_llm, known_names=_known_ingredient_names()
    )
    if not recipes:
        print("No recipes found in those documents.", file=sys.stderr)
        return 1

    review.write_review(args.output, recipes)

    total = sum(len(r.rows) for r in recipes)
    flagged = sum(1 for r in recipes for row in r.rows if not row.is_ok)
    print(f"\n{len(recipes)} recipe(s), {total} ingredient row(s) -> {args.output}")
    for index, recipe in enumerate(recipes, start=1):
        bad = sum(1 for row in recipe.rows if not row.is_ok)
        note = f", {bad} need attention" if bad else ""
        print(f"  {index}. {recipe.dish_name} - {len(recipe.rows)} rows{note}")

    print(f"\nBefore loading: fill base_persons for every recipe"
          f"{f' and settle {flagged} flagged row(s)' if flagged else ''}.")
    return 0


def _expand(paths):
    found = []
    for entry in paths:
        candidates = [entry]
        if os.path.isdir(entry):
            candidates = sorted(glob.glob(os.path.join(entry, "*")))
        elif any(ch in entry for ch in "*?["):
            candidates = sorted(glob.glob(entry))
        found.extend(c for c in candidates if extract_mod.is_importable(c))
    return found


# --------------------------------------------------------------------------
# load
# --------------------------------------------------------------------------


def cmd_load(args):
    recipes = review.read_review(args.review_file)
    if not recipes:
        print(f"{args.review_file} has no recipe rows.", file=sys.stderr)
        return 1

    conn = db.connect()
    try:
        loaded, skipped = 0, 0
        for recipe in recipes:
            ok, message = _load_one(conn, recipe, dry_run=args.dry_run)
            print(message)
            loaded += 1 if ok else 0
            skipped += 0 if ok else 1

        if args.dry_run:
            conn.rollback()
            print(f"\nDry run - nothing written. {loaded} ready, {skipped} refused.")
        else:
            conn.commit()
            print(f"\n{loaded} recipe(s) imported, {skipped} refused.")
    finally:
        conn.close()

    return 0 if skipped == 0 else 1


def _load_one(conn, recipe, dry_run=False):
    """Import one reviewed recipe, or refuse it with a reason.

    Refusing is the point. Unlike the web form, a CSV can be edited anywhere by
    anyone, so every rule the form enforces is enforced again here - via the
    same scaling.validate_ingredient_rows the form itself calls, so the two can
    never drift apart.
    """
    name = (recipe.get("dish_name") or "").strip()
    if not name:
        return False, "REFUSED: a recipe has no dish name."

    persons_raw = (recipe.get("base_persons") or "").strip()
    try:
        base_persons = int(float(persons_raw))
    except ValueError:
        return False, f"REFUSED {name}: base_persons is blank or not a number."
    if base_persons < 1:
        return False, f"REFUSED {name}: base_persons must be at least 1."

    category = (recipe.get("category") or "").strip() or DEFAULT_CATEGORY
    if category not in DISH_CATEGORIES:
        return False, f"REFUSED {name}: '{category}' is not a known category."

    unresolved = [
        r for r in recipe["rows"] if (r.get("status") or "").strip().casefold() != "ok"
    ]
    if unresolved:
        first = unresolved[0]
        return False, (
            f"REFUSED {name}: {len(unresolved)} row(s) still flagged, first is "
            f"'{first.get('ingredient_name')}' ({first.get('status')}) on line "
            f"{first.get('_line_no')}."
        )

    rows, errors = validate_ingredient_rows(
        [r.get("ingredient_name", "") for r in recipe["rows"]],
        [r.get("quantity", "") for r in recipe["rows"]],
        [r.get("unit", "") for r in recipe["rows"]],
    )
    if errors:
        return False, f"REFUSED {name}: {errors[0]}"
    if not rows:
        return False, f"REFUSED {name}: no usable ingredient rows."

    # Soft-deleted dishes still occupy the UNIQUE name; only active clashes
    # are "already exists". An inactive holder is reclaimed below so the CLI
    # matches the web form's restore-by-re-add behaviour.
    clash = conn.execute(
        "SELECT id, is_active FROM dishes WHERE name = ?", (name,)
    ).fetchone()
    if clash and clash["is_active"]:
        return False, f"SKIPPED {name}: a dish with this name already exists."

    if dry_run:
        return True, f"READY {name}: {len(rows)} ingredients for {base_persons} persons."

    if clash and not clash["is_active"]:
        dish_id = clash["id"]
        conn.execute(
            """UPDATE dishes
                  SET category = ?, base_persons = ?, notes = ?, is_active = 1,
                      updated_at = datetime('now', 'localtime')
                WHERE id = ?""",
            (category, base_persons, "Imported from a mess department document.", dish_id),
        )
        conn.execute("DELETE FROM dish_ingredients WHERE dish_id = ?", (dish_id,))
    else:
        dish_id = conn.execute(
            "INSERT INTO dishes (name, category, base_persons, notes) VALUES (?, ?, ?, ?)",
            (name, category, base_persons, "Imported from a mess department document."),
        ).lastrowid

    for order, row in enumerate(rows):
        ingredient_id = db.get_or_create_ingredient(
            row["name"], default_unit_for(row["unit"]), conn=conn
        )
        conn.execute(
            """INSERT INTO dish_ingredients
                   (dish_id, ingredient_id, quantity, unit, sort_order)
               VALUES (?, ?, ?, ?, ?)""",
            (dish_id, ingredient_id, row["quantity"], row["unit"], order),
        )

    return True, f"IMPORTED {name}: {len(rows)} ingredients for {base_persons} persons."


# --------------------------------------------------------------------------


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m recipe_import",
        description="Import mess department recipe documents into the scaling tool.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_extract = sub.add_parser("extract", help="read documents into a review CSV")
    p_extract.add_argument("paths", nargs="+", help="files, folders or globs")
    p_extract.add_argument("-o", "--output", default="review.csv")
    p_extract.add_argument(
        "--no-llm", action="store_true", help="rules parser only; do not call Groq"
    )
    p_extract.set_defaults(func=cmd_extract)

    p_load = sub.add_parser("load", help="import a reviewed CSV into mess.db")
    p_load.add_argument("review_file")
    p_load.add_argument(
        "--dry-run", action="store_true", help="report what would be imported"
    )
    p_load.set_defaults(func=cmd_load)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
