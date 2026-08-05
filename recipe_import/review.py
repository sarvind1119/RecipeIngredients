"""The review sheet: the human checkpoint between a document and the database.

CSV on purpose. The mess office already has Excel, the file opens with a
double-click, and nothing here needs a format the department would have to be
taught. Every row carries the words the mess actually wrote in `source_line`, so
a reviewer settling a flagged row never has to go back to the original document.
"""

import csv

REVIEW_COLUMNS = [
    "recipe_no",
    "dish_name",
    "base_persons",
    "category",
    "ingredient_name",
    "quantity",
    "unit",
    "status",
    "source",
    "source_line",
    "note",
    "suggested_name",
]

# Written into the sheet's first data column so a reviewer opening it cold knows
# what is being asked of them. Skipped on read.
INSTRUCTIONS = [
    "# Review before importing. Fill base_persons for every recipe - the",
    "# documents never state it, and every scaled figure is divided by it.",
    "# Settle each row whose status is not 'ok', then set its status to 'ok'.",
    "# Rows left flagged are refused by the loader, not imported quietly.",
]


def write_review(path, recipes):
    """Write the review sheet. `recipes` is a list of ReviewRecipe-like objects."""
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        for line in INSTRUCTIONS:
            writer.writerow([line])
        writer.writerow(REVIEW_COLUMNS)

        for index, recipe in enumerate(recipes, start=1):
            for row in recipe.rows:
                writer.writerow(
                    [
                        index,
                        recipe.dish_name,
                        recipe.base_persons or "",
                        recipe.category,
                        row.name,
                        _format_qty(row.quantity),
                        row.unit or "",
                        row.status,
                        row.source,
                        row.source_line,
                        row.note,
                        row.suggested_name,
                    ]
                )


def read_review(path):
    """Read an edited review sheet back into per-recipe dicts, in file order."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = [r for r in csv.reader(fh) if r and not r[0].startswith("#")]

    if not rows:
        raise ValueError(f"{path} is empty")

    header = [h.strip() for h in rows[0]]
    missing = [c for c in REVIEW_COLUMNS if c not in header]
    if missing:
        raise ValueError(f"{path} is missing column(s): {', '.join(missing)}")

    recipes = {}
    order = []
    for line_no, raw in enumerate(rows[1:], start=2):
        record = dict(zip(header, [c.strip() for c in raw]))
        if not any(record.get(c) for c in ("dish_name", "ingredient_name", "quantity")):
            continue

        key = record.get("recipe_no", "") or record.get("dish_name", "")
        if key not in recipes:
            recipes[key] = {
                "dish_name": record.get("dish_name", ""),
                "base_persons": record.get("base_persons", ""),
                "category": record.get("category", ""),
                "rows": [],
            }
            order.append(key)

        # A reviewer may fill base_persons on only the first row of a recipe.
        if record.get("base_persons") and not recipes[key]["base_persons"]:
            recipes[key]["base_persons"] = record["base_persons"]

        record["_line_no"] = line_no
        recipes[key]["rows"].append(record)

    return [recipes[k] for k in order]


def _format_qty(value):
    if value is None:
        return ""
    text = f"{float(value):.3f}".rstrip("0").rstrip(".")
    return text or "0"
