"""OCR cross-check of AI rows. Pure text: no network, no OCR engine needed."""

from recipe_import.ocr_verify import _group_into_lines, cross_check
from recipe_import.parse import ParsedRow


def _ai(name, qty, unit):
    return ParsedRow(name=name, quantity=qty, unit=unit, source="llm",
                     note="read from page image by AI - please confirm against the document")


def _check(row, lines):
    rows, counts = cross_check([row], lines)
    return rows[0], counts


def test_matching_figure_is_confirmed_despite_ocr_misspelling():
    row, counts = _check(_ai("Cabbage", 6, "kg"), ["Cabbuge-6kg"])
    assert counts["confirmed"] == 1
    assert row.status == "ok" and row.quantity == 6


def test_different_figure_is_flagged_and_blanked():
    row, counts = _check(_ai("Tomato", 3, "kg"), ["tomato-31g"])
    assert counts["conflict"] == 1
    assert row.status == "needs_qty"
    assert row.quantity is None
    assert "3 kg" in row.note and "31" in row.note


def test_units_compared_in_base_units():
    row, counts = _check(_ai("Butter", 0.5, "kg"), ["Butter 500 g"])
    assert counts["confirmed"] == 1


def test_either_neighbouring_figure_may_confirm_on_a_list_line():
    line = ["Binding: 1kg roasted chana powder 500g corn flour"]
    assert _check(_ai("roasted chana powder", 1, "kg"), line)[1]["confirmed"] == 1
    assert _check(_ai("corn flour", 500, "g"), line)[1]["confirmed"] == 1


def test_digits_glued_to_letters_are_noise_not_a_disagreement():
    row, counts = _check(_ai("Butter", 500, "g"), ["Butter- Swo Q1."])
    assert counts["unconfirmed"] == 1
    assert row.status == "ok" and row.quantity == 500


def test_name_absent_from_ocr_keeps_ai_reading_but_says_so():
    row, counts = _check(_ai("Makka", 4, "kg"), ["Cabbuge-6kg"])
    assert counts["unconfirmed"] == 1
    assert row.quantity == 4
    assert "OCR could not confirm" in row.note


def test_boxes_on_one_visual_line_are_joined_left_to_right():
    # (top, bottom, left, text, score): "Atta" and "6kg" are separate boxes.
    boxes = [(241, 280, 340, "6kg", 0.9), (259, 290, 33, "Atta-", 0.9),
             (500, 540, 55, "Cabbage-6kg", 0.9)]
    assert _group_into_lines(boxes) == ["Atta- 6kg", "Cabbage-6kg"]
