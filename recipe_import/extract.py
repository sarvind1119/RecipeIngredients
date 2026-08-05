"""Format adapters: whatever the department sends -> lines of text.

Word documents, digital PDFs, scans, photographs and typed spreadsheets all
arrive. Each adapter's job is only to produce lines (or, for a scan, a page
image to be read); deciding what a line *means* is parse.py's job.

Third-party readers are imported lazily so that a missing optional dependency
only fails the format that needs it - a mess PC that never receives a scan
should not have to install PaddlePaddle to open a Word document.
"""

import io
import os
import re
import zipfile
from dataclasses import dataclass, field
from xml.etree import ElementTree

from recipe_import.normalise import normalise_text, title_case

DOCX_EXTENSIONS = {".docx"}
PDF_EXTENSIONS = {".pdf"}
EXCEL_EXTENSIONS = {".xlsx", ".xlsm"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
SUPPORTED_EXTENSIONS = (
    DOCX_EXTENSIONS | PDF_EXTENSIONS | EXCEL_EXTENSIONS | IMAGE_EXTENSIONS
)

# A digital PDF page yielding less than this much text is a scan, not text.
_SCANNED_PAGE_TEXT_THRESHOLD = 40

_TITLE_PATTERNS = [
    re.compile(r"^(?:recipe|receipe)\s+(?:of|for)\s+[:\-]?\s*(?P<name>.+)$", re.IGNORECASE),
    re.compile(r"^(?P<name>.+?)\s+(?:recipe|receipe)s?\s*$", re.IGNORECASE),
    re.compile(r"^dish\s*[:\-]\s*(?P<name>.+)$", re.IGNORECASE),
]

_INGREDIENTS_HEADER = re.compile(r"^ingredients?\s*[:\-]?\s*$", re.IGNORECASE)
_SERVES = re.compile(
    r"\b(?:serves|for|servings?|persons?|pax)\b\D{0,12}(?P<n>\d{1,5})"
    r"|\b(?P<n2>\d{1,5})\s*(?:persons?|pax|servings?|portions?)\b",
    re.IGNORECASE,
)


@dataclass
class RawRecipe:
    """One recipe lifted out of a document, before any parsing."""

    dish_name: str = ""
    lines: list = field(default_factory=list)
    sheet_rows: list = field(default_factory=list)
    base_persons: str = ""
    page_images: list = field(default_factory=list)
    source_file: str = ""
    ocr_confidence: float | None = None


class UnsupportedFormat(Exception):
    pass


def is_importable(path):
    """Skip Word's ~$ lock files - they are not documents, they are locks."""
    name = os.path.basename(path)
    if name.startswith("~$") or name.startswith("."):
        return False
    return os.path.splitext(name)[1].lower() in SUPPORTED_EXTENSIONS


def extract(path, ocr=True):
    """Read a document into RawRecipe objects."""
    ext = os.path.splitext(path)[1].lower()

    if ext in DOCX_EXTENSIONS:
        recipes = split_recipes(read_docx(path))
    elif ext in PDF_EXTENSIONS:
        recipes = _extract_pdf(path, ocr=ocr)
    elif ext in EXCEL_EXTENSIONS:
        recipes = read_excel(path)
    elif ext in IMAGE_EXTENSIONS:
        recipes = _extract_image(path, ocr=ocr)
    else:
        raise UnsupportedFormat(f"{ext or path} is not a format the importer reads")

    for recipe in recipes:
        recipe.source_file = os.path.basename(path)
    return recipes


# --------------------------------------------------------------------------
# Word
# --------------------------------------------------------------------------

_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def read_docx(path):
    """Read paragraphs and table cells in document order.

    Deliberately reads the XML directly rather than through python-docx: the
    dependency buys nothing here, and paragraph-and-table order is exactly what
    a recipe document needs preserved. Typed recipes often arrive as a table
    even though the sample is prose, so both are read.
    """
    with zipfile.ZipFile(path) as archive:
        xml = archive.read("word/document.xml")

    body = ElementTree.fromstring(xml).find(f"{_W_NS}body")
    if body is None:
        return []

    lines = []
    for node in body.iter():
        if node.tag == f"{_W_NS}p":
            text = normalise_text(_node_text(node))
            if text:
                lines.append(text)
        elif node.tag == f"{_W_NS}tr":
            cells = [normalise_text(_node_text(c)) for c in node.findall(f"{_W_NS}tc")]
            cells = [c for c in cells if c]
            if cells:
                # A table row is one ingredient: "Onion | 400 | g" reads back as
                # a line the same parser handles.
                lines.append(" ".join(cells))

    # Paragraphs inside table cells are visited twice by iter(); drop the repeat.
    return _dedupe_consecutive(lines)


def _node_text(node):
    return "".join(t.text or "" for t in node.iter(f"{_W_NS}t"))


def _dedupe_consecutive(lines):
    out = []
    for line in lines:
        if out and line == out[-1]:
            continue
        if out and line and line in out[-1] and len(out[-1]) > len(line):
            continue
        out.append(line)
    return out


# --------------------------------------------------------------------------
# PDF (digital text, falling through to OCR for scans)
# --------------------------------------------------------------------------


def _extract_pdf(path, ocr=True):
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover - depends on local install
        raise UnsupportedFormat(
            "reading PDFs needs pdfplumber: pip install -r requirements-import.txt"
        ) from exc

    text_lines = []
    scanned_pages = []

    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text() or ""
            if len(page_text.strip()) >= _SCANNED_PAGE_TEXT_THRESHOLD:
                text_lines.extend(
                    normalise_text(ln) for ln in page_text.splitlines() if ln.strip()
                )
            elif ocr:
                scanned_pages.append(_render_page(page))

    recipes = split_recipes([ln for ln in text_lines if ln])

    for image_bytes in scanned_pages:
        recipes.extend(_recipes_from_image(image_bytes, ocr=ocr))

    return recipes


def _render_page(page, resolution=300):
    buffer = io.BytesIO()
    page.to_image(resolution=resolution).original.save(buffer, format="PNG")
    return buffer.getvalue()


# --------------------------------------------------------------------------
# Scans and photographs
# --------------------------------------------------------------------------


def _extract_image(path, ocr=True):
    with open(path, "rb") as fh:
        return _recipes_from_image(fh.read(), ocr=ocr)


def _recipes_from_image(image_bytes, ocr=True):
    """OCR a page, keeping the image so a poor read can be escalated to Claude.

    PaddleOCR reports per-line confidence, which is the signal used later to
    decide whether to re-read the page with a vision model. OCR noise turns
    "100g" into "1OOg", so a low-confidence page must never be trusted straight
    into a requisition - the image travels with the text for exactly that
    reason.
    """
    lines, confidence = ([], None)
    if ocr:
        lines, confidence = run_paddle_ocr(image_bytes)

    recipes = split_recipes(lines) if lines else [RawRecipe()]
    for recipe in recipes:
        recipe.page_images = [image_bytes]
        recipe.ocr_confidence = confidence
    return recipes


def run_paddle_ocr(image_bytes):
    """Return (lines, mean_confidence). Empty and None when OCR is unavailable."""
    try:
        import numpy as np
        from paddleocr import PaddleOCR
        from PIL import Image
    except ImportError:  # pragma: no cover - depends on local install
        return [], None

    global _PADDLE
    try:
        _PADDLE
    except NameError:
        _PADDLE = PaddleOCR(use_angle_cls=True, lang="en", show_log=False)

    image = np.array(Image.open(io.BytesIO(image_bytes)).convert("RGB"))
    result = _PADDLE.ocr(image, cls=True) or []

    lines, scores = [], []
    for page in result:
        for entry in page or []:
            text, score = entry[1][0], float(entry[1][1])
            text = normalise_text(text)
            if text:
                lines.append(text)
                scores.append(score)

    return lines, (sum(scores) / len(scores) if scores else None)


# --------------------------------------------------------------------------
# Spreadsheets
# --------------------------------------------------------------------------

_SHEET_ALIASES = {
    "dish_name": {"dish", "dish name", "recipe", "recipe name", "item"},
    "base_persons": {"base persons", "persons", "serves", "servings", "pax", "for"},
    "category": {"category", "course", "type"},
    "ingredient_name": {"ingredient", "ingredient name", "item name", "particulars"},
    "quantity": {"quantity", "qty", "amount", "weight"},
    "unit": {"unit", "uom", "units", "measure"},
}


def read_excel(path):
    """Read a typed spreadsheet, mapping loose column headings onto our fields."""
    from openpyxl import load_workbook

    workbook = load_workbook(path, data_only=True, read_only=True)
    recipes, order = {}, []

    for sheet in workbook.worksheets:
        rows = list(sheet.iter_rows(values_only=True))
        header_index = _find_header_row(rows)
        if header_index is None:
            continue

        mapping = _map_columns(rows[header_index])
        sheet_dish = title_case(normalise_text(sheet.title))

        for raw in rows[header_index + 1 :]:
            record = {
                field: normalise_text(str(raw[col])) if col < len(raw) and raw[col] is not None else ""
                for field, col in mapping.items()
            }
            if not record.get("ingredient_name"):
                continue

            dish = record.get("dish_name") or sheet_dish
            if dish not in recipes:
                recipes[dish] = RawRecipe(
                    dish_name=dish, base_persons=record.get("base_persons", "")
                )
                order.append(dish)
            if record.get("base_persons") and not recipes[dish].base_persons:
                recipes[dish].base_persons = record["base_persons"]
            recipes[dish].sheet_rows.append(record)

    workbook.close()
    return [recipes[d] for d in order]


def _find_header_row(rows):
    for index, row in enumerate(rows[:20]):
        cells = {str(c).strip().casefold() for c in row if c is not None}
        if cells & _SHEET_ALIASES["ingredient_name"] and cells & _SHEET_ALIASES["quantity"]:
            return index
    return None


def _map_columns(header_row):
    mapping = {}
    for index, cell in enumerate(header_row):
        if cell is None:
            continue
        heading = str(cell).strip().casefold()
        for field_name, aliases in _SHEET_ALIASES.items():
            if heading in aliases and field_name not in mapping:
                mapping[field_name] = index
    return mapping


# --------------------------------------------------------------------------
# Splitting a document into recipes
# --------------------------------------------------------------------------


def split_recipes(lines):
    """Cut a stream of lines into one RawRecipe per dish.

    Documents routinely hold several recipes - the sample holds two - so
    assuming one recipe per file would merge two dishes' ingredients into a
    single indent. A line is a title if it says so ("Recipe of X", "X Receipe")
    or if the very next line is a bare "Ingredients" heading.
    """
    lines = [ln for ln in (normalise_text(ln) for ln in lines) if ln]
    if not lines:
        return []

    recipes = []
    current = None

    for index, line in enumerate(lines):
        title = _as_title(line, next_line=lines[index + 1] if index + 1 < len(lines) else "")
        if title is not None:
            current = RawRecipe(dish_name=title)
            recipes.append(current)
            continue

        if current is None:
            current = RawRecipe()
            recipes.append(current)

        serves = _SERVES.search(line)
        if serves and not current.base_persons:
            current.base_persons = serves.group("n") or serves.group("n2")
            continue

        current.lines.append(line)

    return [r for r in recipes if r.lines or r.sheet_rows]


def _as_title(line, next_line):
    for pattern in _TITLE_PATTERNS:
        match = pattern.match(line)
        if match:
            return title_case(match.group("name").strip(" .:-"))

    # An untitled dish name directly above the word "Ingredients".
    if _INGREDIENTS_HEADER.match(next_line) and len(line.split()) <= 8:
        if not _INGREDIENTS_HEADER.match(line):
            return title_case(line.strip(" .:-"))

    return None
