"""Claude fallback for what the rules parser could not settle.

Two jobs, both narrow:

* re-read the handful of lines parse.py flagged, in the context of their recipe;
* re-read a whole page image when OCR came back doubtful.

Never the whole document. The rules parser handles the bulk deterministically
and for free; the model is for the residue.

Two properties matter more than accuracy here:

* **Quantity is nullable in the schema.** The model is told to answer "I don't
  know" rather than produce a plausible number, and a null comes back as a
  flagged row. A recipe that asks a question is recoverable; a requisition
  carrying an invented figure is not.
* **It degrades, it never blocks.** No API key, no package, no network - the
  importer still runs and the affected lines stay flagged.

Every row that came from here is marked source="llm" on the review sheet, so a
reviewer knows which figures to check hardest.
"""

import base64
import os

from recipe_import.normalise import clean_name, resolve_unit, normalise_text
from recipe_import.parse import ParsedRow
from scaling import UNITS

MODEL = "claude-opus-5"

# OCR below this mean confidence is not trusted into a requisition; the page
# image is re-read by the vision model instead.
OCR_CONFIDENCE_FLOOR = 0.80

_SYSTEM = (
    "You transcribe Indian institutional-kitchen recipes into structured rows. "
    "You are a transcriber, not a chef: report only what the document states.\n\n"
    "Rules:\n"
    "- Never invent, estimate, convert or infer a quantity. If the document does "
    "not state a number for an ingredient, set quantity to null.\n"
    "- 'salt to taste', 'for garnish' and similar mean no quantity was stated: "
    "null.\n"
    "- Do not convert between units. If the stated unit is not one of the "
    f"permitted units ({', '.join(UNITS)}), set unit to null and leave the "
    "quantity as written.\n"
    "- Do not correct spelling of ingredient names. Transcribe them as written.\n"
    "- Strip preparation notes from names: 'Onion, finely chopped' is 'Onion'.\n"
    "- One row per ingredient. A line naming two ingredients yields two rows."
)


class LLMUnavailable(Exception):
    """Raised when the fallback cannot run. Always caught by the caller."""


def is_available():
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return bool(
        os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        or _has_cli_profile()
    )


def _has_cli_profile():
    """`ant auth login` stores a profile the SDK picks up with no env var set."""
    config = os.environ.get("ANTHROPIC_CONFIG_DIR") or os.path.join(
        os.environ.get("APPDATA") or os.path.expanduser("~/.config"), "anthropic"
    )
    return os.path.isdir(os.path.join(config, "credentials"))


def _client():
    try:
        import anthropic
    except ImportError as exc:
        raise LLMUnavailable(
            "the Claude fallback needs the anthropic package: "
            "pip install -r requirements-import.txt"
        ) from exc
    return anthropic.Anthropic()


def _schema():
    """Quantity and unit are nullable on purpose - see the module docstring."""
    return {
        "type": "object",
        "properties": {
            "rows": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Ingredient name as written, prep notes removed.",
                        },
                        "quantity": {
                            "type": ["number", "null"],
                            "description": "Number as stated. null if the document states none.",
                        },
                        "unit": {
                            "type": ["string", "null"],
                            "enum": [*UNITS, None],
                            "description": "Permitted unit as stated, or null.",
                        },
                        "source_line": {
                            "type": "string",
                            "description": "The document text this row came from.",
                        },
                    },
                    "required": ["name", "quantity", "unit", "source_line"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["rows"],
        "additionalProperties": False,
    }


def resolve_lines(lines, dish_name="", effort="low"):
    """Re-read flagged lines. Returns [] if the fallback cannot run."""
    if not lines:
        return []

    context = f"Recipe: {dish_name}\n\n" if dish_name else ""
    prompt = (
        f"{context}Transcribe these ingredient lines. They defeated a rules-based "
        "parser, so expect run-on lines, missing units and missing quantities.\n\n"
        + "\n".join(f"- {ln}" for ln in lines)
    )

    return _request([{"type": "text", "text": prompt}], effort=effort, why="unparsed line")


def resolve_page_image(image_bytes, media_type="image/png", effort="medium"):
    """Re-read a scanned page the OCR engine was not confident about."""
    if not image_bytes:
        return []

    return _request(
        [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": base64.standard_b64encode(image_bytes).decode("ascii"),
                },
            },
            {
                "type": "text",
                "text": (
                    "Transcribe every ingredient and its stated quantity from this "
                    "recipe page. Where a figure is illegible or absent, set "
                    "quantity to null rather than guessing."
                ),
            },
        ],
        effort=effort,
        why="low-confidence scan",
    )


def _request(content, effort, why):
    client = _client()

    response = client.messages.create(
        model=MODEL,
        max_tokens=8000,
        system=_SYSTEM,
        thinking={"type": "adaptive"},
        output_config={
            "effort": effort,
            "format": {"type": "json_schema", "schema": _schema()},
        },
        messages=[{"role": "user", "content": content}],
    )

    if response.stop_reason == "refusal":
        raise LLMUnavailable("the model declined to transcribe this document")

    import json

    text = next((b.text for b in response.content if b.type == "text"), "")
    if not text:
        return []

    payload = json.loads(text)
    return [_to_row(r, why) for r in payload.get("rows", [])]


def _to_row(record, why):
    """Convert one model row, re-applying our own validation to its answer."""
    name = clean_name(str(record.get("name") or ""))
    quantity = record.get("quantity")
    unit = record.get("unit")
    source_line = normalise_text(str(record.get("source_line") or ""))

    if unit:
        unit, unit_note = resolve_unit(str(unit))
    else:
        unit, unit_note = None, "no unit stated in the document"

    row = ParsedRow(
        name=name,
        quantity=float(quantity) if isinstance(quantity, (int, float)) else None,
        unit=unit,
        source_line=source_line,
        source="llm",
    )

    if not row.name:
        row.status, row.note = "needs_name", f"read from {why}; no ingredient name found"
    elif row.quantity is None or row.quantity <= 0:
        row.quantity = None
        row.status = "needs_qty"
        row.note = f"read from {why}; no quantity stated - enter what the mess issues"
    elif row.unit is None:
        row.status = "needs_unit"
        row.note = f"read from {why}; {unit_note}"
    else:
        # A figure a model produced is still a figure nobody has checked.
        row.status = "ok"
        row.note = f"read from {why} by Claude - please confirm against the document"

    return row
