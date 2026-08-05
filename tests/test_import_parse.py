"""Tests for the recipe document importer.

Run from the project root:  python -m pytest tests/ -v

No network and no OCR here - every case below is the pure rules parser working
on wording taken verbatim from the department's own documents. The point of
these tests is that a flagged line stays flagged: the failure mode that matters
is not a crash, it is a plausible-looking figure nobody wrote down reaching a
Store requisition.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from recipe_import.extract import is_importable, split_recipes  # noqa: E402
from recipe_import.normalise import (  # noqa: E402
    clean_name,
    normalise_text,
    resolve_unit,
    suggest_known_name,
)
from recipe_import.parse import (  # noqa: E402
    parse_ingredient_line,
    parse_recipe_lines,
    split_fragments,
)
from scaling import UNITS, validate_ingredient_rows  # noqa: E402

SAMPLE_DOCX = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "SampleIngredient.docx"
)


def one(line):
    rows = parse_ingredient_line(line)
    assert len(rows) == 1, f"expected 1 row from {line!r}, got {len(rows)}"
    return rows[0]


class TestTextNormalisation:
    """Word leaves typographic debris that silently defeats a plain regex."""

    def test_non_breaking_space_becomes_a_plain_space(self):
        # "\xa01 kg \xa0Grated Paneer" is exactly what the sample contains.
        row = one("\xa01 kg \xa0Grated Paneer ,")
        assert (row.quantity, row.unit) == (1.0, "kg")

    def test_vulgar_fraction_resolves_to_a_decimal(self):
        # Without this, "½ tsp Turmeric" parses as no quantity at all.
        row = one("½  tsp Turmeric powder")
        assert row.quantity == 0.5
        assert row.unit == "tsp"

    def test_written_fraction_resolves_to_a_decimal(self):
        assert normalise_text("1/2 tsp") == "0.5 tsp"

    def test_zero_width_and_repeated_spaces_collapse(self):
        assert normalise_text("100g​   Ginger") == "100g Ginger"


class TestFragmentSplitting:
    """One comma means two different things and the parser must tell them apart."""

    def test_prep_note_after_a_comma_stays_with_its_ingredient(self):
        # Splitting here would invent an ingredient called "cut into triangle shape".
        assert split_fragments("1kg Paneer, cut into triangle shape") == [
            "1kg Paneer, cut into triangle shape"
        ]
        row = one("1kg Paneer, cut into triangle shape")
        assert row.name == "Paneer"

    def test_second_ingredient_after_a_comma_becomes_its_own_row(self):
        rows = parse_ingredient_line("500g  chopped onion, 250g  chopped tomatoes, ")
        assert [(r.name, r.quantity, r.unit) for r in rows] == [
            ("onion", 500.0, "g"),
            ("tomatoes", 250.0, "g"),
        ]

    def test_quantityless_ingredient_after_a_comma_is_not_swallowed(self):
        # "salt to taste" reduces to "salt", so it is an ingredient - not a note
        # on the kasuri methi before it. Getting this wrong loses salt entirely.
        rows = parse_ingredient_line(
            "30 g pav bhaji masala,10g  haldi, 5g kasuri methi ,salt to taste"
        )
        assert [r.name for r in rows] == [
            "pav bhaji masala",
            "haldi",
            "kasuri methi",
            "salt",
        ]
        assert rows[-1].status == "needs_qty"

    def test_and_separates_ingredients(self):
        rows = parse_ingredient_line(" 50 Chopped garlic, and 50g  green chillies.")
        assert [r.name for r in rows] == ["garlic", "green chillies"]

    def test_trailing_conjunction_is_dropped(self):
        rows = parse_ingredient_line("100g  Roasted chana powder  and ")
        assert [(r.name, r.quantity) for r in rows] == [("Roasted chana powder", 100.0)]


class TestUnits:
    def test_recognised_aliases_map_onto_scaling_units(self):
        for written, expected in [
            ("g", "g"), ("gm", "g"), ("gms", "g"), ("Kg", "kg"), ("kgs", "kg"),
            ("ml", "ml"), ("ltr", "litre"), ("L", "litre"),
            ("pc", "nos"), ("pcs", "nos"), ("nos", "nos"),
            ("pkt", "packet"), ("tsp", "tsp"), ("tablespoon", "tbsp"),
        ]:
            unit, _ = resolve_unit(written)
            assert unit == expected, f"{written!r} -> {unit!r}"
            assert unit in UNITS

    def test_pc_becomes_nos_so_countable_rounding_applies(self):
        row = one("4 pc  Green chilies, slit")
        assert (row.name, row.quantity, row.unit, row.status) == (
            "Green chilies", 4.0, "nos", "ok",
        )

    def test_length_units_are_flagged_never_converted(self):
        # Turning "2-inch ginger" into grams means inventing a weight.
        row = one("2-inch chopped ginger,")
        assert row.status == "needs_unit"
        assert row.name == "ginger"
        assert row.quantity == 2.0
        assert row.unit is None
        assert "inch" in row.note

    def test_missing_unit_is_flagged_and_does_not_eat_a_name_word(self):
        row = one("50 Chopped garlic")
        assert row.status == "needs_unit"
        assert row.name == "garlic"
        assert row.quantity == 50.0
        assert row.unit is None


class TestQuantitylessLines:
    def test_garnish_line_is_kept_and_flagged(self):
        row = one("Fresh coriander leaves for garnish\xa0")
        assert row.status == "needs_qty"
        assert row.name == "Fresh coriander leaves"
        assert row.quantity is None

    def test_run_on_line_yields_two_rows(self):
        # "30 g deggi mirch Salt to taste" is 30 g of deggi mirch AND salt, not
        # an ingredient called "deggi mirch Salt".
        rows = parse_ingredient_line("30 g deggi mirch Salt to taste")
        assert len(rows) == 2
        assert (rows[0].name, rows[0].quantity, rows[0].unit, rows[0].status) == (
            "deggi mirch", 30.0, "g", "ok",
        )
        assert (rows[1].name, rows[1].quantity, rows[1].status) == (
            "Salt", None, "needs_qty",
        )

    def test_single_ingredient_with_to_taste_is_not_split(self):
        # No capitalised second ingredient, so there is only one thing here.
        row = one("30 g salt to taste")
        assert (row.name, row.quantity, row.unit, row.status) == ("salt", 30.0, "g", "ok")


class TestNames:
    def test_prep_notes_are_stripped_so_names_resolve_to_one_master_row(self):
        assert clean_name("Onion, finely chopped") == "Onion"
        assert clean_name("Ginger-garlic paste, divided") == "Ginger-garlic paste"
        assert clean_name("chopped tomatoes") == "tomatoes"
        assert clean_name("Green chilies, slit") == "Green chilies"

    def test_spelling_is_never_corrected_only_suggested(self):
        # "R.oil" must survive as written; the reviewer decides what it means.
        row = one("100 ml  R.oil    ")
        assert row.name == "R.oil"
        assert suggest_known_name("R.oil", ["Refined Oil", "Onion"]) == ""
        assert suggest_known_name("Onions", ["Refined Oil", "Onion"]) == "Onion"

    def test_typos_parse_without_being_rewritten(self):
        row = one("15g re chilli powder")
        assert (row.name, row.quantity, row.unit, row.status) == (
            "re chilli powder", 15.0, "g", "ok",
        )


class TestDuplicates:
    def test_repeated_ingredient_is_flagged_not_inserted_twice(self):
        # dish_ingredients has UNIQUE (dish_id, ingredient_id), so this would
        # fail at insert time - after the reviewer had signed the sheet off.
        rows = parse_recipe_lines(["200 g Onion", "300 g Onion"])
        assert [r.status for r in rows] == ["duplicate", "duplicate"]

    def test_section_headers_are_not_ingredients(self):
        assert parse_recipe_lines(["Ingredients", "Method", "200 g Onion"]) != []
        assert len(parse_recipe_lines(["Ingredients", "Method", "200 g Onion"])) == 1


class TestRecipeSplitting:
    def test_two_recipes_in_one_document_are_separated(self):
        recipes = split_recipes(
            [
                "Paneer Kaleji Receipe",
                "Ingredients",
                "1kg Paneer",
                "Recipe of Amritsari Paneer Bhurji",
                "Ingredients",
                "1 kg Grated Paneer",
            ]
        )
        assert [r.dish_name for r in recipes] == [
            "Paneer Kaleji",
            "Amritsari Paneer Bhurji",
        ]

    def test_a_stated_serving_count_is_captured(self):
        recipes = split_recipes(["Dal Makhani Recipe", "Serves 100 persons", "3 kg Dal"])
        assert recipes[0].base_persons == "100"

    def test_word_lock_files_are_skipped(self):
        assert is_importable("SampleIngredient.docx")
        assert not is_importable("~$mpleIngredient.docx")


@pytest.mark.skipif(not os.path.exists(SAMPLE_DOCX), reason="sample document absent")
class TestSampleDocument:
    """Golden test over the department's actual document.

    Pinned figures, so a later change to the parser that quietly drops an
    ingredient or invents a quantity fails here rather than on a requisition.
    """

    @pytest.fixture(scope="class")
    def recipes(self):
        from recipe_import.cli import build_recipes

        return build_recipes([SAMPLE_DOCX], use_llm=False)

    def test_both_recipes_are_found(self, recipes):
        assert [r.dish_name for r in recipes] == [
            "Paneer Kaleji",
            "Amritsari Paneer Bhurji",
        ]

    def test_no_ingredient_is_lost(self, recipes):
        assert [len(r.rows) for r in recipes] == [16, 18]

    def test_exactly_the_expected_rows_need_attention(self, recipes):
        flagged = {
            (row.name, row.status) for r in recipes for row in r.rows if not row.is_ok
        }
        assert flagged == {
            ("Salt", "needs_qty"),
            ("Fresh Coriander Leaves", "needs_qty"),
            ("Ginger", "needs_unit"),
            ("Garlic", "needs_unit"),
            ("Salt", "needs_qty"),
        }

    def test_no_row_carries_an_invented_quantity(self, recipes):
        for recipe in recipes:
            for row in recipe.rows:
                if row.status == "needs_qty":
                    assert row.quantity is None, f"{row.name} was given a figure"
                if row.is_ok:
                    assert row.quantity and row.quantity > 0
                    assert row.unit in UNITS

    def test_base_persons_is_left_blank_for_a_human(self, recipes):
        # The documents never state it, and scale_quantity divides by it.
        assert all(not r.base_persons for r in recipes)

    def test_resolved_rows_would_be_accepted_by_the_app(self, recipes):
        for recipe in recipes:
            ok_rows = [r for r in recipe.rows if r.is_ok]
            rows, errors = validate_ingredient_rows(
                [r.name for r in ok_rows],
                [r.quantity for r in ok_rows],
                [r.unit for r in ok_rows],
            )
            assert errors == []
            assert len(rows) == len(ok_rows)


class TestSharedValidation:
    """The importer and the web form must enforce identical rules."""

    def test_flagged_values_are_refused(self):
        _, errors = validate_ingredient_rows(["Ginger"], [2.0], [""])
        assert errors and "not a recognised unit" in errors[0]

        _, errors = validate_ingredient_rows(["Salt"], [""], ["g"])
        assert errors and "no quantity" in errors[0]

        _, errors = validate_ingredient_rows(["Onion", "onion"], [1, 2], ["kg", "kg"])
        assert errors and "more than once" in errors[0]

    def test_clean_rows_pass(self):
        rows, errors = validate_ingredient_rows(["Paneer"], [1.0], ["kg"])
        assert errors == []
        assert rows == [{"name": "Paneer", "quantity": 1.0, "unit": "kg"}]
