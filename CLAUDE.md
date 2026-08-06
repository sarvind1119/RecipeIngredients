# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with
code in this repository.

## Running the App

Always run from the project root (`RecipeIngredients/`):

```bash
# Activate virtual environment first
.venv\Scripts\activate

# Run the server
python app.py
```

Server starts at `http://localhost:5002` and binds `0.0.0.0`, so other PCs on
the Mess LAN reach it at `http://<host-ip>:5002`. `run_app.bat` creates the venv
and installs dependencies if they are absent.

Port 5002 was chosen to avoid the 5001 used by `../Live_Demo_App`.

## Installing Dependencies

```bash
pip install -r requirements.txt
```

Core dependencies: `flask`, `reportlab` (PDF), `openpyxl` (Excel). Password
hashing uses `werkzeug.security`, which ships with Flask. `pytest` is for tests
only.

`requirements-import.txt` is separate and is **only** needed on a machine that
converts the department's recipe documents (see Importing Recipes below). It
pulls in `paddlepaddle`, which is several hundred MB — the Mess PC that serves
the app must not be made to install it to run a Flask server.

## Running Tests

```bash
python -m pytest tests/ -v
```

`tests/test_scaling.py` covers the arithmetic in isolation; `tests/test_app.py`
covers the app end to end with a throwaway database per test.

## Architecture

A four-module Flask app over one SQLite file. There is no build step and no
frontend framework — the dynamic parts are vanilla JS inline in the templates.

- **[scaling.py](scaling.py)** — pure arithmetic. Deliberately imports neither
  Flask nor sqlite3 so every rule is testable without a request context. This is
  the module that replaces the manual calculation, so it is the one that has to
  be right.
- **[db.py](db.py)** — connection helper, schema bootstrap, first-run admin seed.
- **[exports.py](exports.py)** — ReportLab PDF and openpyxl Excel generation.
- **[app.py](app.py)** — routes, session auth, form validation.
- **[recipe_import/](recipe_import/)** — offline CLI that converts the mess
  department's documents into recipes. Not imported by the Flask app; the app
  runs with none of its dependencies installed.

**Data flow for a requirement:** `/calculate` → `scale_recipe()` →
rows written to `requisitions` + `requisition_items` → redirect to
`/requisition/<id>` → PDF/Excel read back from those stored rows, never
recalculated.

## Key Implementation Notes

**Requisitions are snapshots, not views.** `requisition_items` stores the
calculated figures, and `requisitions` stores `dish_name_snapshot` and
`base_persons_snapshot`. Editing a recipe, renaming a dish or soft-deleting it
must never change a requisition that was already issued to the Store. Do not
"simplify" the export routes to recompute from the live recipe — that silently
rewrites history. `TestSnapshotIntegrity` guards this.

**Dish deletion is always soft** (`is_active = 0`). Past requisitions reference
the row.

**Both pragmas in `db.connect()` are load-bearing** and neither is a SQLite
default:
- `foreign_keys = ON` — without it the `ON DELETE` clauses in `schema.sql` are
  decorative, because SQLite silently ignores FK constraints per connection.
- `journal_mode = WAL` plus `timeout=10.0` — the app is served to several PCs.
  With the default rollback journal, two staff submitting requisitions at the
  same instant get `database is locked`.

**`/admin/backup` uses `sqlite3.Connection.backup()`, not `send_file('mess.db')`.**
Under WAL, recent commits can still be in `mess.db-wal`, so streaming the main
file alone may hand back a backup missing the newest recipes.

**Rounding is always upward** (`scaling._round_up_practical`). A short indent
stops the cooking; a small excess does not. Countable units (`nos`, `packet`,
`bunch`) use `ceil` — you cannot indent 3.2 eggs — with a rounding tolerance so
an exact 9.0 is not pushed to 10.

**`round(..., 3)` in `scale_quantity` is not cosmetic.** Without it, binary
float representation puts `27.000000000000004` on a Store requisition.

**Exact quantity and required quantity can be in different units.** Unit
normalisation may promote 4500 g to 4.5 kg while `exact_quantity` stays in the
base unit (g). Anywhere the exact figure is displayed it must carry its own
unit, or the row reads as "4500 kg". This applies to `result.html`, the PDF, and
the Excel export (which uses a separate `Exact Unit` column so the numbers stay
numeric and sortable).

**Both auth decorators enforce the forced password change.** `admin_required`
repeats the `must_change_password` check rather than deferring to
`login_required` — they are applied independently, so an admin on a default
password could otherwise reach admin routes by typing the URL.

**Ingredient names are `COLLATE NOCASE` unique.** "Onion" and "onion" resolve to
one master row. This drives autocomplete today and is what will make cross-dish
ingredient consolidation possible later without a migration.

## Importing Recipes

The department supplies recipes as prose in Word documents, and also as digital
PDFs, scans, photographs and typed spreadsheets.

**In the app** (any logged-in user): Recipes → *Import from document* → upload → each
recipe appears in the ordinary Add-recipe form, pre-filled, with unreadable rows
highlighted and the document's own wording shown beneath them.

**On the command line**, two commands with a human between them:

```bash
python -m recipe_import extract SampleIngredient.docx -o review.csv
python -m recipe_import load review.csv --dry-run
```

**The human step is not optional, for one reason:** the documents never state
how many persons a recipe serves. `dishes.base_persons` is `NOT NULL CHECK (> 0)`
and every figure `scale_quantity()` produces is divided by it, so it has to be
supplied by someone who knows. The review CSV emits it blank and the loader
refuses a recipe without it.

**A line the parser cannot resolve is emitted flagged and blank — never dropped
and never guessed.** "salt to taste", "2-inch ginger" and "50 Chopped garlic"
all appear in the sample. Converting any of them into a weight means inventing a
figure the mess never wrote down, and that figure would reach the Store as an
indent. The loader refuses any row still flagged, so an unsettled question fails
loudly instead of importing quietly.

**`scaling.validate_ingredient_rows()` is shared with the web form on purpose.**
[app.py](app.py)'s `_parse_ingredient_rows` delegates to it, so a row the form
would reject cannot enter through a CSV instead. A blank unit is rejected rather
than defaulted to `kg` there for the same reason — the form's `<select>` can
never post one, but an edited spreadsheet can, and "2-inch ginger" silently
becoming a 2 kg indent is exactly the failure this system exists to prevent.

**The Groq AI fallback is a bonus, never a dependency.** Set `GROQ_API_KEY` in
`recipe_import/.env` (loaded by `app.py` and `recipe_import.llm`). Photos,
scans and handwriting are read **vision-first** via Groq; Word/PDF text still
uses the rules parser, with Groq only for flagged lines. With no API key the
importer still runs and hard lines stay flagged. Quantity is nullable so the
model answers "not stated" rather than inventing a figure; every AI row is
`source=llm` on the review sheet.

**PaddleOCR is optional offline fallback only.** Prefer Groq vision for images.
`extract.py` still keeps the page image so vision can re-read it.

Recipe splitting matters: documents routinely hold several recipes (the sample
holds two), so assuming one per file would merge two dishes into a single indent.

**`recipe_import` is imported lazily inside the import routes, never at module
scope.** `importer_available()` gates the navigation and the routes 404 without
it, so a Mess PC can run the app with the folder and its dependencies absent —
`TestDocumentImportUI` asserts the app still serves with the importer missing.
Keep it that way: a top-level `import recipe_import` in [app.py](app.py) would
make a broken optional add-on stop the server from starting.

**The import review screen is `dish_form.html`, and it posts to `/dishes/new`.**
The importer only pre-fills the ordinary form; saving runs `_save_dish` and the
same validation as hand entry, so a flagged row cannot reach the database by
arriving through a document. Do not give the importer its own save path — that
is how the two would drift and how an unchecked figure would reach the Store.

Parsed-but-unsaved recipes are parked as JSON under the system temp directory
with a token in the session, not in the session cookie itself, which is signed
and capped at 4 KB.

Imported rows carry `status`/`note`/`source_line`/`suggested_name`. The form's
JS treats `tr.note-row` as presentation only — excluded from row numbering and
removed alongside the row it annotates.

## Scope

V1 is single-dish scaling only. Menu-wise planning, consolidation of common
ingredients across dishes, Store approval workflow and inventory are out of
scope but the schema is normalised for them — consolidation becomes a
`GROUP BY ingredient_id` across several dishes.

Per-ingredient non-linear scaling (salt and whole spices do not truly scale in
proportion) was considered and deferred; everything scales linearly as the
requirement specifies.

## Files Not in Version Control

`mess.db` (and its `-wal`/`-shm` companions) and `.secret_key`, which holds the
generated Flask session key so sessions survive a restart.
