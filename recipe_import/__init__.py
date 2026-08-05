"""Turn the mess department's unstructured recipes into tool-ready rows.

The department writes recipes as prose in Word documents, and will also send
digital PDFs, scans, photographs and typed spreadsheets. This package reads all
of those and produces a review CSV a human checks before anything is written to
mess.db.

The guiding rule: a line the parser cannot resolve is emitted flagged and blank.
It is never dropped and never guessed. A recipe that silently loses its salt is
worse than one that asks a question.
"""

from recipe_import.parse import ROW_STATUSES, ParsedRow, parse_ingredient_line
from recipe_import.review import REVIEW_COLUMNS, read_review, write_review

__all__ = [
    "REVIEW_COLUMNS",
    "ROW_STATUSES",
    "ParsedRow",
    "parse_ingredient_line",
    "read_review",
    "write_review",
]
