"""AI fallback for what the rules parser could not settle — Groq vision/text.

Two jobs, both narrow:

* re-read a whole page image (photos, scans, handwriting) — primary path for
  images, not only a low-confidence OCR rescue;
* re-read the handful of text lines parse.py flagged.

Never invent quantities. The model is told to answer null when a figure is
illegible or absent; those rows stay flagged for a human on the review form.

It degrades, it never blocks: no GROQ_API_KEY, no package, no network — the
importer still runs and the affected lines stay flagged.

Every row that came from here is marked source="llm" so a reviewer knows which
figures to check hardest.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
from dataclasses import dataclass, field

from recipe_import.normalise import clean_name, resolve_unit, normalise_text
from recipe_import.parse import ParsedRow
from scaling import UNITS

# Override with GROQ_VISION_MODEL / GROQ_TEXT_MODEL if Groq renames models.
# Vision default matches console.groq.com/docs/vision (account-available list).
DEFAULT_VISION_MODEL = "qwen/qwen3.6-27b"
DEFAULT_TEXT_MODEL = "llama-3.3-70b-versatile"

# OCR below this mean confidence is not trusted into a requisition without a
# vision re-read (used when deciding whether OCR-only rows are good enough).
OCR_CONFIDENCE_FLOOR = 0.80

# Keep payloads under Groq's practical image limits.
_MAX_IMAGE_EDGE = 1600
_JPEG_QUALITY = 85

_SYSTEM = (
    "You transcribe Indian institutional-kitchen recipes into structured data. "
    "You are a transcriber, not a chef: report only what the document shows.\n\n"
    "Language:\n"
    "- Sheets may mix English, Hindi (Devanagari), and Hinglish. Transcribe "
    "ingredient names as written. Do not translate names into English.\n"
    "- Do not correct spelling.\n\n"
    "Quantities (critical):\n"
    "- Never invent, estimate, convert, or infer a quantity. If a number is "
    "illegible, crossed out, or absent, set quantity to null.\n"
    "- 'salt to taste', 'for garnish', 'as required' and similar mean no "
    "quantity was stated: quantity null.\n"
    "- Do not convert between units. If the unit is not one of "
    f"({', '.join(UNITS)}), set unit to null.\n"
    "- Hindi unit words (e.g. किलो, ग्राम) map only when clearly kg/g/l/ml; "
    "otherwise unit null.\n\n"
    "Structure:\n"
    "- Strip preparation notes from names: 'Onion, finely chopped' is 'Onion'.\n"
    "- One row per ingredient. A line naming two ingredients yields two rows.\n"
    "- base_persons: only if the page states serves/persons/pax; else null.\n"
    "- dish_name: title if visible; else empty string.\n"
    "- source_line: short quote of the wording you read for that row."
)


class LLMUnavailable(Exception):
    """Raised when the fallback cannot run. Always caught by the caller."""


@dataclass
class VisionRecipe:
    """Structured read of one page image."""

    dish_name: str = ""
    base_persons: str = ""
    rows: list = field(default_factory=list)


def load_dotenv_files():
    """Load .env from project root and recipe_import/ without overriding env."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        _load_dotenv_manual()
        return

    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    for path in (os.path.join(root, ".env"), os.path.join(here, ".env")):
        if os.path.isfile(path):
            load_dotenv(path, override=False)


def _load_dotenv_manual():
    """Minimal KEY=VALUE loader when python-dotenv is not installed."""
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    for path in (os.path.join(root, ".env"), os.path.join(here, ".env")):
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key and key not in os.environ:
                        os.environ[key] = value
        except OSError:
            continue


# Load once on import so CLI and Flask both see recipe_import/.env
load_dotenv_files()


def is_available():
    # Re-read .env each check so a key added after server start is picked up
    # without a full process restart (common when first configuring Groq).
    load_dotenv_files()
    if not os.environ.get("GROQ_API_KEY", "").strip():
        return False
    try:
        import groq  # noqa: F401
    except ImportError:
        return False
    return True


def availability_status():
    """Human-readable AI readiness for the import UI."""
    load_dotenv_files()
    if not os.environ.get("GROQ_API_KEY", "").strip():
        return (
            False,
            "GROQ_API_KEY is not set. Put it in recipe_import/.env and restart, "
            "or photos/scans will not be read.",
        )
    try:
        import groq  # noqa: F401
    except ImportError:
        return (
            False,
            "The groq package is not installed. Run: pip install -r requirements-import.txt",
        )
    return True, f"Groq vision ready ({_vision_model()}). Photo reads may take 30–90 seconds."


def _client():
    try:
        from groq import Groq
    except ImportError as exc:
        raise LLMUnavailable(
            "the Groq fallback needs the groq package: "
            "pip install -r requirements-import.txt"
        ) from exc
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if not key:
        raise LLMUnavailable(
            "set GROQ_API_KEY in recipe_import/.env or the environment"
        )
    return Groq(api_key=key)


def _vision_model():
    return os.environ.get("GROQ_VISION_MODEL", DEFAULT_VISION_MODEL).strip()


def _text_model():
    return os.environ.get("GROQ_TEXT_MODEL", DEFAULT_TEXT_MODEL).strip()


def resolve_lines(lines, dish_name=""):
    """Re-read flagged lines. Returns [] if the fallback cannot run."""
    if not lines:
        return []

    context = f"Recipe: {dish_name}\n\n" if dish_name else ""
    prompt = (
        f"{context}Transcribe these ingredient lines into JSON. They defeated a "
        "rules-based parser, so expect run-on lines, missing units and missing "
        "quantities. Mixed English/Hindi is allowed — do not translate names.\n\n"
        "Return a JSON object with key \"rows\": an array of objects with "
        "name (string), quantity (number or null), unit (string or null; only "
        f"{', '.join(UNITS)} or null), source_line (string).\n\n"
        + "\n".join(f"- {ln}" for ln in lines)
    )

    payload = _chat_json(
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": prompt},
        ],
        model=_text_model(),
        why="unparsed line",
    )
    return [_to_row(r, "unparsed line") for r in payload.get("rows", [])]


def resolve_page_image(image_bytes, media_type="image/jpeg"):
    """Transcribe a photo/scan/handwritten page via Groq vision.

    Returns a VisionRecipe (dish_name, base_persons, rows). Empty rows when
    the model returns nothing useful.
    """
    if not image_bytes:
        return VisionRecipe()

    encoded, media_type = _prepare_image(image_bytes, media_type)
    prompt = (
        "Transcribe every ingredient and its stated quantity from this recipe "
        "page into JSON. Handwriting, photocopies, and phone photos are "
        "expected. Mixed English/Hindi is allowed — do not translate names.\n\n"
        "Where a figure is illegible or absent, set quantity to null rather "
        "than guessing.\n\n"
        "Return a JSON object with:\n"
        '- "dish_name": string (empty if no title),\n'
        '- "base_persons": number or null (only if the page states how many '
        "persons/servings),\n"
        '- "rows": array of {name, quantity, unit, source_line} where unit is '
        f"one of {', '.join(UNITS)} or null."
    )

    payload = _chat_json(
        messages=[
            {"role": "system", "content": _SYSTEM},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{media_type};base64,{encoded}",
                        },
                    },
                ],
            },
        ],
        model=_vision_model(),
        why="page image",
    )

    dish_name = normalise_text(str(payload.get("dish_name") or ""))
    base_persons = _format_persons(payload.get("base_persons"))
    rows = [_to_row(r, "page image") for r in payload.get("rows", [])]
    return VisionRecipe(dish_name=dish_name, base_persons=base_persons, rows=rows)


def _format_persons(value):
    if value is None or value == "":
        return ""
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return ""
    return str(n) if n > 0 else ""


def _prepare_image(image_bytes, media_type="image/jpeg"):
    """Resize/orient/contrast and re-encode so phone photos fit API limits."""
    try:
        from PIL import Image, ImageOps, ImageEnhance, ImageFilter
    except ImportError:
        return base64.standard_b64encode(image_bytes).decode("ascii"), media_type

    try:
        image = Image.open(io.BytesIO(image_bytes))
        image = ImageOps.exif_transpose(image)
        image = image.convert("RGB")
        # Screenshots and dim phone photos benefit from mild contrast stretch.
        image = ImageOps.autocontrast(image, cutoff=1)
        image = ImageEnhance.Sharpness(image).enhance(1.15)
        w, h = image.size
        scale = min(1.0, _MAX_IMAGE_EDGE / max(w, h))
        if scale < 1.0:
            image = image.resize(
                (max(1, int(w * scale)), max(1, int(h * scale))),
                Image.Resampling.LANCZOS,
            )
        # Slight denoise on very large/noisy phone shots after resize.
        if max(image.size) >= 1200:
            image = image.filter(ImageFilter.UnsharpMask(radius=1, percent=120, threshold=3))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
        return base64.standard_b64encode(buffer.getvalue()).decode("ascii"), "image/jpeg"
    except Exception:
        return base64.standard_b64encode(image_bytes).decode("ascii"), media_type


def _chat_json(messages, model, why):
    client = _client()
    # Try structured JSON first (with thinking disabled for Qwen).
    attempts = [
        dict(
            model=model,
            messages=messages,
            temperature=0.1,
            max_completion_tokens=4096,
            response_format={"type": "json_object"},
            reasoning_effort="none",
        ),
        # Some accounts/models reject reasoning_effort.
        dict(
            model=model,
            messages=messages,
            temperature=0.1,
            max_completion_tokens=4096,
            response_format={"type": "json_object"},
        ),
        # Last resort: free text, we extract JSON ourselves.
        dict(
            model=model,
            messages=messages,
            temperature=0.1,
            max_completion_tokens=4096,
            reasoning_effort="none",
        ),
    ]

    last_error = None
    text = ""
    for kwargs in attempts:
        try:
            response = client.chat.completions.create(**kwargs)
            text = (response.choices[0].message.content or "").strip()
            if text:
                break
        except Exception as exc:
            last_error = exc
            text = ""
            continue
    else:
        raise LLMUnavailable(
            f"Groq request failed ({why}): {last_error or 'empty response'}"
        ) from last_error

    if not text:
        raise LLMUnavailable(f"Groq returned an empty response for {why}")

    text = _strip_think_blocks(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            raise LLMUnavailable(
                f"Groq returned non-JSON for {why}: {text[:200]!r}"
            ) from None
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise LLMUnavailable(f"Groq JSON parse failed ({why}): {exc}") from exc


def _strip_think_blocks(text):
    """Remove Qwen-style <think>...</think> wrappers if reasoning leaked through."""
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE)
    return cleaned.strip()


def _to_row(record, why):
    """Convert one model row, re-applying our own validation to its answer."""
    if not isinstance(record, dict):
        record = {}

    name = clean_name(str(record.get("name") or ""))
    quantity = record.get("quantity")
    unit = record.get("unit")
    source_line = normalise_text(str(record.get("source_line") or ""))

    if unit not in (None, ""):
        unit, unit_note = resolve_unit(str(unit))
    else:
        unit, unit_note = None, "no unit stated in the document"

    parsed_qty = None
    if isinstance(quantity, (int, float)) and not isinstance(quantity, bool):
        parsed_qty = float(quantity)
    elif isinstance(quantity, str) and quantity.strip():
        try:
            parsed_qty = float(quantity.strip())
        except ValueError:
            parsed_qty = None

    row = ParsedRow(
        name=name,
        quantity=parsed_qty,
        unit=unit,
        source_line=source_line,
        source="llm",
    )

    if not row.name:
        row.status, row.note = (
            "needs_name",
            f"read from {why}; no ingredient name found",
        )
    elif row.quantity is None or row.quantity <= 0:
        row.quantity = None
        row.status = "needs_qty"
        row.note = (
            f"read from {why}; no quantity stated - enter what the mess issues"
        )
    elif row.unit is None:
        row.status = "needs_unit"
        row.note = f"read from {why}; {unit_note}"
    else:
        # A figure a model produced is still a figure nobody has checked.
        row.status = "ok"
        row.note = (
            f"read from {why} by AI - please confirm against the document"
        )

    return row
