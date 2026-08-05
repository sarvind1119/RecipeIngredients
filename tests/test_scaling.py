"""Tests for the scaling arithmetic.

Run from the project root:  python -m pytest tests/ -v
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scaling import format_qty, scale_quantity, scale_recipe  # noqa: E402


class TestRequirementExample:
    """The worked example from the requirement document itself."""

    def test_chicken_biryani_3kg_base_10_for_90_persons(self):
        r = scale_quantity(3, 10, 90, "kg")
        assert r["exact"] == 27.0
        assert r["display_qty"] == 27.0
        assert r["display_unit"] == "kg"


class TestProportionality:
    def test_scale_down(self):
        # Dal Makhani written for 15, cooked for 10.
        r = scale_quantity(3, 15, 10, "kg")
        assert r["exact"] == 2.0

    def test_same_strength_is_identity(self):
        r = scale_quantity(2.5, 60, 60, "kg")
        assert r["exact"] == 2.5

    def test_large_course_strength(self):
        r = scale_quantity(2, 10, 180, "kg")
        assert r["exact"] == 36.0


class TestUnitNormalisation:
    def test_grams_promote_to_kg(self):
        # 250 g at base 10, for 90 -> 2250 g, which reads better as 2.25 kg.
        r = scale_quantity(250, 10, 90, "g")
        assert r["exact"] == 2250.0
        assert r["exact_unit"] == "g"
        assert r["display_qty"] == 2.25
        assert r["display_unit"] == "kg"

    def test_kg_demotes_to_grams(self):
        # 0.05 kg x 9 = 0.45 kg -> 450 g.
        r = scale_quantity(0.05, 10, 90, "kg")
        assert r["exact"] == 0.45
        assert r["display_qty"] == 450.0
        assert r["display_unit"] == "g"

    def test_ml_promotes_to_litre(self):
        r = scale_quantity(200, 10, 90, "ml")
        assert r["display_qty"] == 1.8
        assert r["display_unit"] == "litre"

    def test_litre_demotes_to_ml(self):
        r = scale_quantity(0.1, 10, 5, "litre")
        assert r["display_qty"] == 50.0
        assert r["display_unit"] == "ml"

    def test_countable_units_never_convert(self):
        r = scale_quantity(5, 10, 90, "nos")
        assert r["display_unit"] == "nos"


class TestPracticalRounding:
    def test_countables_round_up(self):
        # 5 eggs for 10 persons, cooked for 65 -> 32.5, and you cannot indent
        # half an egg.
        r = scale_quantity(5, 10, 65, "nos")
        assert r["exact"] == 32.5
        assert r["display_qty"] == 33.0

    def test_exact_whole_countable_is_not_pushed_up(self):
        # 2 nos x 4.5 = 9.0 exactly - must stay 9, not creep to 10.
        r = scale_quantity(2, 10, 45, "nos")
        assert r["exact"] == 9.0
        assert r["display_qty"] == 9.0

    def test_fractional_countable_ceils(self):
        # 2 nos x 4.6 = 9.2 -> 10.
        r = scale_quantity(2, 10, 46, "nos")
        assert r["exact"] == 9.2
        assert r["display_qty"] == 10.0

    def test_kg_rounds_up_to_nearest_005(self):
        r = scale_quantity(1.5, 10, 61, "kg")
        assert r["exact"] == 9.15
        assert r["display_qty"] == 9.15

        r = scale_quantity(1.11, 10, 61, "kg")
        assert r["exact"] == 6.771
        assert r["display_qty"] == 6.8  # up to the next 0.05

    def test_grams_round_up_to_nearest_5(self):
        r = scale_quantity(10, 10, 61, "g")
        assert r["exact"] == 61.0
        assert r["display_qty"] == 65.0

    def test_rounding_is_always_upward(self):
        # Never round down: a short indent stops the cooking.
        for persons in range(61, 120):
            r = scale_quantity(1.3, 10, persons, "kg")
            assert r["display_qty"] >= r["exact"]

    def test_tablespoon_rounds_to_half(self):
        r = scale_quantity(1, 10, 65, "tbsp")
        assert r["exact"] == 6.5
        assert r["display_qty"] == 6.5


class TestFloatHygiene:
    def test_no_binary_float_drift(self):
        # Without round(..., 3) at the calculation point this is
        # 0.030000000000000002 and prints that way on the requisition.
        r = scale_quantity(0.1, 10, 3, "kg")
        assert r["exact"] == 0.03

    def test_repr_is_clean_for_the_headline_case(self):
        r = scale_quantity(3, 10, 90, "kg")
        assert repr(r["exact"]) == "27.0"
        assert format_qty(r["exact"]) == "27"


class TestGuards:
    def test_zero_base_persons_raises_not_divides(self):
        with pytest.raises(ValueError, match="base_persons"):
            scale_quantity(3, 0, 90, "kg")

    def test_negative_base_persons_raises(self):
        with pytest.raises(ValueError, match="base_persons"):
            scale_quantity(3, -10, 90, "kg")

    def test_zero_target_persons_raises(self):
        with pytest.raises(ValueError, match="target_persons"):
            scale_quantity(3, 10, 0, "kg")

    def test_zero_quantity_raises(self):
        with pytest.raises(ValueError, match="quantity"):
            scale_quantity(0, 10, 90, "kg")

    def test_unknown_unit_raises(self):
        with pytest.raises(ValueError, match="unknown unit"):
            scale_quantity(3, 10, 90, "handful")


class TestScaleRecipe:
    def test_scales_a_whole_dish(self):
        rows = [
            {"name": "Chicken", "quantity": 3, "unit": "kg"},
            {"name": "Ghee", "quantity": 500, "unit": "g"},
            {"name": "Eggs", "quantity": 5, "unit": "nos"},
        ]
        out = scale_recipe(rows, 10, 90)

        assert len(out) == 3
        assert out[0]["exact"] == 27.0 and out[0]["display_unit"] == "kg"
        # 500 g x 9 = 4500 g -> 4.5 kg
        assert out[1]["display_qty"] == 4.5 and out[1]["display_unit"] == "kg"
        assert out[2]["display_qty"] == 45.0 and out[2]["display_unit"] == "nos"

    def test_preserves_ingredient_order(self):
        rows = [
            {"name": "B", "quantity": 1, "unit": "kg"},
            {"name": "A", "quantity": 1, "unit": "kg"},
        ]
        out = scale_recipe(rows, 10, 20)
        assert [r["name"] for r in out] == ["B", "A"]
        assert [r["sort_order"] for r in out] == [0, 1]


class TestFormatQty:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [(27.0, "27"), (2.25, "2.25"), (0.5, "0.5"), (450.0, "450"), (6.8, "6.8")],
    )
    def test_trims_trailing_zeros(self, value, expected):
        assert format_qty(value) == expected
