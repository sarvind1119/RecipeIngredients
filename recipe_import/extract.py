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
CSV_EXTENSIONS = {".csv"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
SPREADSHEET_EXTENSIONS = EXCEL_EXTENSIONS | CSV_EXTENSIONS
SUPPORTED_EXTENSIONS = (
    DOCX_EXTENSIONS
    | PDF_EXTENSIONS
    | SPREADSHEET_EXTENSIONS
    | IMAGE_EXTENSIONS
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
    elif ext in CSV_EXTENSIONS:
        recipes = read_csv(path)
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
    """OCR a page, keeping the image so Groq vision can re-read it.

    For photos and handwriting the CLI prefers Groq vision over these OCR
    lines. PaddleOCR remains an offline fallback when the API is unavailable.
    The page image always travels with the recipe for that reason.
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
# Spreadsheets (Excel + CSV)
# --------------------------------------------------------------------------

# Headings are normalised (punctuation stripped) then matched. Prefer specific
# ingredient aliases over the bare word "item", which some mess sheets use for
# the dish and others for the commodity line.
_SHEET_ALIASES = {
    "dish_name": {
        "dish", "dish name", "recipe", "recipe name", "menu item", "menu",
        "dish item", "name of dish", "item name dish",
    },
    "base_persons": {
        "base persons", "persons", "person", "serves", "servings", "pax",
        "no of persons", "no of pax", "number of persons", "for persons",
        "strength", "head count", "headcount",
    },
    "category": {"category", "course", "type", "dish type"},
    "ingredient_name": {
        "ingredient", "ingredients", "ingredient name", "item name",
        "particulars", "particular", "commodity", "commodities",
        "raw material", "raw materials", "material", "materials",
        "item", "items", "name", "description", "stores item",
        "ingredient list", "name of ingredient", "name of item",
    },
    "quantity": {
        "quantity", "qty", "qnty", "qyt", "amount", "weight", "wt",
        "qty required", "required qty", "req qty", "issue qty",
        "indent qty", "qty kg", "net qty",
    },
    "unit": {
        "unit", "uom", "units", "measure", "unit of measure", "u o m",
        "unit name", "issue unit",
    },
}

# When a header is ambiguous, prefer these fields first.
_FIELD_PRIORITY = (
    "ingredient_name",
    "quantity",
    "unit",
    "dish_name",
    "base_persons",
    "category",
)

_QTY_UNIT_CELL = re.compile(
    r"^(?P<qty>\d+(?:\.\d+)?)\s*(?P<unit>[A-Za-z.]+)?\s*$"
)


def read_excel(path):
    """Read a typed .xlsx/.xlsm spreadsheet into RawRecipe objects."""
    from openpyxl import load_workbook

    workbook = load_workbook(path, data_only=True, read_only=True)
    try:
        table_groups = []
        for sheet in workbook.worksheets:
            rows = [tuple(r) for r in sheet.iter_rows(values_only=True)]
            sheet_dish = title_case(normalise_text(sheet.title))
            # Skip default Sheet1-style titles as dish names.
            if re.fullmatch(r"sheet\s*\d*", sheet_dish, flags=re.IGNORECASE):
                sheet_dish = ""
            table_groups.append((sheet_dish, rows))
    finally:
        workbook.close()

    return _recipes_from_tables(table_groups, default_dish=_dish_from_filename(path))


def read_csv(path):
    """Read a UTF-8/CSV spreadsheet (Excel 'CSV UTF-8' or plain export)."""
    import csv

    rows = _read_csv_rows(path)
    return _recipes_from_tables(
        [("", rows)], default_dish=_dish_from_filename(path)
    )


def _read_csv_rows(path):
    import csv

    # utf-8-sig strips a BOM that Excel on Windows often writes.
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            with open(path, newline="", encoding=encoding) as fh:
                sample = fh.read(4096)
                fh.seek(0)
                try:
                    dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
                except csv.Error:
                    dialect = csv.excel
                reader = csv.reader(fh, dialect)
                return [tuple(row) for row in reader]
        except UnicodeDecodeError:
            continue
    with open(path, newline="", encoding="utf-8", errors="replace") as fh:
        return [tuple(row) for row in csv.reader(fh)]


def _dish_from_filename(path):
    stem = os.path.splitext(os.path.basename(path))[0]
    stem = re.sub(r"[_\-]+", " ", stem).strip()
    return title_case(normalise_text(stem)) if stem else ""


def _recipes_from_tables(table_groups, default_dish=""):
    """Build RawRecipes from (fallback_dish_name, rows) table groups."""
    recipes, order = {}, []

    for fallback_dish, rows in table_groups:
        if not rows:
            continue

        header_index = _find_header_row(rows)
        if header_index is not None:
            mapping = _map_columns(rows[header_index])
            _append_mapped_sheet_rows(
                recipes,
                order,
                rows[header_index + 1 :],
                mapping,
                fallback_dish=fallback_dish or default_dish,
            )
            continue

        # No recognised header — treat non-empty cells as free-text lines for
        # the rules parser (works for a single column of "2 kg onion" lines).
        lines = _rows_as_text_lines(rows)
        if not lines:
            continue
        for recipe in split_recipes(lines):
            if not recipe.dish_name:
                recipe.dish_name = fallback_dish or default_dish
            key = recipe.dish_name or f"__anon_{len(order)}"
            if key not in recipes:
                recipes[key] = recipe
                order.append(key)
            else:
                recipes[key].lines.extend(recipe.lines)
                if recipe.base_persons and not recipes[key].base_persons:
                    recipes[key].base_persons = recipe.base_persons

    return [recipes[d] for d in order if recipes[d].lines or recipes[d].sheet_rows]


def _append_mapped_sheet_rows(recipes, order, data_rows, mapping, fallback_dish=""):
    for raw in data_rows:
        if raw is None or all(c is None or str(c).strip() == "" for c in raw):
            continue

        record = {}
        for field, col in mapping.items():
            if col < len(raw) and raw[col] is not None:
                record[field] = normalise_text(str(raw[col]))
            else:
                record[field] = ""

        # Split "2 kg" when unit column is missing or empty.
        _split_qty_unit_fields(record)

        if not record.get("ingredient_name"):
            continue
        # Skip total/summary rows masquerading as ingredients.
        name_key = record["ingredient_name"].casefold()
        if name_key in {"total", "grand total", "sub total", "subtotal", "s.no", "sno"}:
            continue

        dish = record.get("dish_name") or fallback_dish or "Imported recipe"
        if dish not in recipes:
            recipes[dish] = RawRecipe(
                dish_name=dish if dish != "Imported recipe" else fallback_dish or "",
                base_persons=record.get("base_persons", ""),
            )
            order.append(dish)
        if record.get("base_persons") and not recipes[dish].base_persons:
            recipes[dish].base_persons = record["base_persons"]
        recipes[dish].sheet_rows.append(record)


def _split_qty_unit_fields(record):
    """If quantity is '2 kg' and unit is blank, split them."""
    qty_raw = (record.get("quantity") or "").strip()
    unit_raw = (record.get("unit") or "").strip()
    if not qty_raw:
        return
    match = _QTY_UNIT_CELL.match(qty_raw)
    if not match:
        # Also try "2kg" without space
        match = re.match(
            r"^(?P<qty>\d+(?:\.\d+)?)(?P<unit>[A-Za-z.]+)$", qty_raw
        )
    if match and match.group("unit") and not unit_raw:
        record["quantity"] = match.group("qty")
        record["unit"] = match.group("unit")
    elif match:
        record["quantity"] = match.group("qty")
        if match.group("unit") and not unit_raw:
            record["unit"] = match.group("unit")


def _rows_as_text_lines(rows):
    lines = []
    for raw in rows:
        if not raw:
            continue
        parts = [
            normalise_text(str(c))
            for c in raw
            if c is not None and str(c).strip()
        ]
        if not parts:
            continue
        lines.append(" ".join(parts))
    return lines


def _normalise_heading(cell):
    if cell is None:
        return ""
    text = normalise_text(str(cell)).casefold()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _find_header_row(rows):
    """Locate a header that names an ingredient column (qty optional)."""
    best = None
    best_score = 0
    for index, row in enumerate(rows[:30]):
        if not row:
            continue
        mapping = _map_columns(row)
        score = 0
        if "ingredient_name" in mapping:
            score += 3
        if "quantity" in mapping:
            score += 2
        if "unit" in mapping:
            score += 1
        if "dish_name" in mapping:
            score += 1
        # Need at least an ingredient column to be useful as a header.
        if "ingredient_name" in mapping and score > best_score:
            best_score = score
            best = index
    return best


def _map_columns(header_row):
    """Map spreadsheet columns onto our field names.

    Exact alias match wins; otherwise a heading that *contains* an alias as a
    whole word (e.g. 'qty required' → quantity). Each field maps once.
    """
    mapping = {}
    headings = [_normalise_heading(c) for c in header_row]

    # Pass 1: exact match by priority.
    for field_name in _FIELD_PRIORITY:
        aliases = _SHEET_ALIASES[field_name]
        for index, heading in enumerate(headings):
            if not heading or index in mapping.values():
                continue
            if heading in aliases and field_name not in mapping:
                mapping[field_name] = index
                break

    # Pass 2: substring / contains match for remaining fields.
    for field_name in _FIELD_PRIORITY:
        if field_name in mapping:
            continue
        aliases = sorted(_SHEET_ALIASES[field_name], key=len, reverse=True)
        for index, heading in enumerate(headings):
            if not heading or index in mapping.values():
                continue
            for alias in aliases:
                if alias == heading or re.search(
                    rf"\b{re.escape(alias)}\b", heading
                ):
                    mapping[field_name] = index
                    break
            if field_name in mapping:
                break

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
