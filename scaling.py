"""Proportional recipe scaling.

Pure arithmetic - deliberately imports neither Flask nor sqlite3, so every rule
here is directly unit-testable without a request context or a database. This is
the module that replaces the manual calculation, so it is the module that has to
be right.
"""

import math

# Units offered on the recipe form.
UNITS = ["kg", "g", "litre", "ml", "nos", "packet", "bunch", "tsp", "tbsp"]

DISH_CATEGORIES = [
    "Main Course",
    "Rice & Breads",
    "Dal & Vegetables",
    "Snacks",
    "Sweets",
    "Beverages",
]

MEAL_TYPES = ["Breakfast", "Lunch", "High Tea", "Dinner", "Special Event"]

# Units you cannot draw a fraction of from the Store.
_COUNTABLE = {"nos", "packet", "bunch"}

# Practical rounding step per unit. Everything rounds UP: under-indenting the
# Store stops the cooking, over-indenting slightly does not.
_ROUNDING_STEP = {
    "kg": 0.05,
    "litre": 0.05,
    "g": 5.0,
    "ml": 5.0,
    "tsp": 0.5,
    "tbsp": 0.5,
}


def scale_quantity(base_qty, base_persons, target_persons, unit):
    """Scale one ingredient line.

    Returns a dict with:
        exact         - the true proportional figure, in the original unit
        exact_unit    - the original unit (unchanged)
        display_qty   - a kitchen-practical figure, rounded up
        display_unit  - possibly promoted/demoted (g <-> kg, ml <-> litre)

    Both figures are surfaced on screen and on the requisition. The exact value
    is never hidden behind the rounded one - staff replacing a manual sum need
    to see that the arithmetic was not quietly adjusted for them.
    """
    base_persons = int(base_persons)
    target_persons = int(target_persons)

    if base_persons <= 0:
        raise ValueError("base_persons must be greater than zero")
    if target_persons <= 0:
        raise ValueError("target_persons must be greater than zero")
    if base_qty <= 0:
        raise ValueError("base quantity must be greater than zero")

    unit = unit.strip().lower()
    if unit not in UNITS:
        raise ValueError(f"unknown unit: {unit!r}")

    # round() here is not cosmetic. Binary floats make 0.1 * 3 come out as
    # 0.30000000000000004, and without this the requisition would print
    # "27.000000000000004 kg". Clamp at the point of calculation so every
    # downstream value - stored, displayed and exported - is already clean.
    exact = round(base_qty * (target_persons / base_persons), 3)

    display_qty, display_unit = _normalise_unit(exact, unit)
    display_qty = _round_up_practical(display_qty, display_unit)

    return {
        "exact": exact,
        "exact_unit": unit,
        "display_qty": display_qty,
        "display_unit": display_unit,
    }


def _normalise_unit(qty, unit):
    """Present a quantity in the unit a cook would actually say out loud.

    Purely cosmetic: 2250 g and 2.25 kg are the same amount. Never changes what
    is being requested, only how it reads on the indent.
    """
    if unit == "g" and qty >= 1000:
        return round(qty / 1000.0, 3), "kg"
    if unit == "ml" and qty >= 1000:
        return round(qty / 1000.0, 3), "litre"
    if unit == "kg" and qty < 1:
        return round(qty * 1000.0, 3), "g"
    if unit == "litre" and qty < 1:
        return round(qty * 1000.0, 3), "ml"
    return qty, unit


def _round_up_practical(qty, unit):
    """Round up to something the Store can actually issue."""
    if unit in _COUNTABLE:
        # You cannot indent 3.2 eggs. Tolerance absorbs float noise so an exact
        # 9.0 stays 9 rather than being pushed to 10.
        return float(math.ceil(round(qty, 6)))

    step = _ROUNDING_STEP.get(unit)
    if step is None:
        return round(qty, 3)

    return round(math.ceil(round(qty / step, 6)) * step, 3)


def scale_recipe(ingredient_rows, base_persons, target_persons):
    """Scale a whole dish.

    `ingredient_rows` is any sequence of mappings carrying `name`, `quantity`
    and `unit` - sqlite3.Row works directly.
    """
    scaled = []
    for order, row in enumerate(ingredient_rows):
        result = scale_quantity(row["quantity"], base_persons, target_persons, row["unit"])
        scaled.append(
            {
                "name": row["name"],
                "base_quantity": row["quantity"],
                "base_unit": row["unit"],
                "exact": result["exact"],
                "display_qty": result["display_qty"],
                "display_unit": result["display_unit"],
                "sort_order": order,
            }
        )
    return scaled


def validate_ingredient_rows(names, quantities, units):
    """Validate parallel name/quantity/unit lists into clean ingredient rows.

    The single set of rules for what the database will accept, shared by the web
    form and the document importer so the two can never drift apart. Kept here,
    with no Flask and no sqlite3, for the same reason as the arithmetic above.

    Trailing blank rows are habit, not error - dropped silently. A *partially*
    filled row is a genuine slip and is reported. Returns (rows, errors).
    """
    rows, errors = [], []

    for idx in range(len(names)):
        name = (names[idx] or "").strip()
        qty_raw = str(quantities[idx]).strip() if idx < len(quantities) and quantities[idx] is not None else ""
        # A blank unit is rejected rather than defaulted. The web form posts a
        # <select> that always carries one of UNITS, so a blank can only reach
        # here from an imported sheet - where quietly reading it as "kg" would
        # turn "2-inch ginger" into a 2 kg indent.
        unit = str(units[idx] or "").strip().lower() if idx < len(units) else "kg"

        if not name and not qty_raw:
            continue  # empty row - discard without comment

        if not name:
            errors.append(f"Row {idx + 1}: quantity entered without an ingredient name.")
            continue
        if not qty_raw:
            errors.append(f"Row {idx + 1}: '{name}' has no quantity.")
            continue

        try:
            quantity = float(qty_raw)
        except ValueError:
            errors.append(f"Row {idx + 1}: '{qty_raw}' is not a valid quantity.")
            continue

        if quantity <= 0:
            errors.append(f"Row {idx + 1}: quantity for '{name}' must be greater than zero.")
            continue
        if unit not in UNITS:
            errors.append(f"Row {idx + 1}: '{unit}' is not a recognised unit.")
            continue

        rows.append({"name": name, "quantity": quantity, "unit": unit})

    seen = {}
    for row in rows:
        key = row["name"].casefold()
        seen[key] = seen.get(key, 0) + 1
    for key, count in seen.items():
        if count > 1:
            errors.append(f"'{key}' is listed more than once — combine it into a single row.")

    return rows, errors


def format_qty(value):
    """Trim trailing zeros so 27.0 prints as '27' and 2.25 stays '2.25'."""
    text = f"{float(value):.3f}".rstrip("0").rstrip(".")
    return text if text else "0"
