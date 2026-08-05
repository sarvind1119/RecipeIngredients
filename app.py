"""Officers' Mess - Recipe Scaling & Ingredient Requirement System.

Run from the project root:  python app.py
Then open http://localhost:5002 (or http://<this-pc-ip>:5002 from the LAN).
"""

import json
import os
import secrets
import sqlite3
import tempfile
from datetime import date
from functools import wraps

from flask import (
    Flask,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

import db
from exports import build_excel, build_pdf, export_filename
from scaling import (
    DISH_CATEGORIES,
    MEAL_TYPES,
    UNITS,
    format_qty,
    scale_recipe,
    validate_ingredient_rows,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SECRET_KEY_FILE = os.path.join(BASE_DIR, ".secret_key")
# 5002 by default, avoiding the 5001 used by ../Live_Demo_App. This is the port
# the Mess PCs are told to use, so run_app.bat sets nothing and gets it. PORT is
# read from the environment only so a second instance can be started alongside
# the running one for development without fighting it for the port.
PORT = int(os.environ.get("PORT") or 5002)


def _load_secret_key():
    """Persist a generated key so sessions survive a restart of the Mess PC."""
    key = os.environ.get("MESS_SECRET_KEY")
    if key:
        return key
    if os.path.exists(SECRET_KEY_FILE):
        with open(SECRET_KEY_FILE, encoding="utf-8") as fh:
            stored = fh.read().strip()
        if stored:
            return stored
    key = secrets.token_hex(32)
    with open(SECRET_KEY_FILE, "w", encoding="utf-8") as fh:
        fh.write(key)
    return key


app = Flask(__name__)
app.config["SECRET_KEY"] = _load_secret_key()
db.init_app(app)


# --- Authentication -------------------------------------------------------

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            flash("Please log in to continue.", "warning")
            return redirect(url_for("login", next=request.path))
        # A seeded or reset account must not be able to wander the app until
        # its default password has been replaced.
        if session.get("must_change_password") and request.endpoint != "change_password":
            return redirect(url_for("change_password"))
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login", next=request.path))
        # Same gate as login_required. Without this an admin still on a default
        # password could reach admin routes by typing the URL, bypassing the
        # forced change that the navigation flow enforces.
        if session.get("must_change_password") and request.endpoint != "change_password":
            return redirect(url_for("change_password"))
        if session.get("role") != "admin":
            # 403 rather than a hidden button: the UI hides admin controls from
            # Staff, but the routes have to refuse them too.
            abort(403)
        return view(*args, **kwargs)

    return wrapped


@app.context_processor
def inject_globals():
    return {
        "current_user": {
            "id": session.get("user_id"),
            "username": session.get("username"),
            "full_name": session.get("full_name"),
            "role": session.get("role"),
        },
        "is_admin": session.get("role") == "admin",
        "format_qty": format_qty,
        "today": date.today().isoformat(),
        "import_available": importer_available(),
    }


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = db.query(
            "SELECT * FROM users WHERE username = ? AND is_active = 1", (username,), one=True
        )
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["full_name"] = user["full_name"] or user["username"]
            session["role"] = user["role"]
            session["must_change_password"] = bool(user["must_change_password"])
            if user["must_change_password"]:
                flash("Please set a new password before continuing.", "warning")
                return redirect(url_for("change_password"))
            return redirect(request.args.get("next") or url_for("dashboard"))
        flash("Invalid username or password.", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Signed out.", "success")
    return redirect(url_for("login"))


@app.route("/change-password", methods=["GET", "POST"])
def change_password():
    if not session.get("user_id"):
        return redirect(url_for("login"))

    if request.method == "POST":
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")

        user = db.query("SELECT * FROM users WHERE id = ?", (session["user_id"],), one=True)
        if not check_password_hash(user["password_hash"], current):
            flash("Current password is incorrect.", "danger")
        elif len(new) < 6:
            flash("New password must be at least 6 characters.", "danger")
        elif new != confirm:
            flash("New passwords do not match.", "danger")
        else:
            db.execute(
                "UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?",
                (generate_password_hash(new), session["user_id"]),
            )
            session["must_change_password"] = False
            flash("Password updated.", "success")
            return redirect(url_for("dashboard"))

    return render_template("change_password.html")


# --- Dashboard ------------------------------------------------------------

@app.route("/")
@login_required
def dashboard():
    dish_count = db.query("SELECT COUNT(*) AS n FROM dishes WHERE is_active = 1", one=True)["n"]
    ingredient_count = db.query("SELECT COUNT(*) AS n FROM ingredients", one=True)["n"]
    requisition_count = db.query("SELECT COUNT(*) AS n FROM requisitions", one=True)["n"]
    recent = db.query(
        "SELECT * FROM requisitions ORDER BY generated_at DESC, id DESC LIMIT 8"
    )
    return render_template(
        "dashboard.html",
        dish_count=dish_count,
        ingredient_count=ingredient_count,
        requisition_count=requisition_count,
        recent=recent,
    )


# --- Dish master ----------------------------------------------------------

@app.route("/dishes")
@login_required
def dishes():
    search = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()

    sql = "SELECT * FROM dishes WHERE is_active = 1"
    args = []
    if search:
        sql += " AND name LIKE ?"
        args.append(f"%{search}%")
    if category:
        sql += " AND category = ?"
        args.append(category)
    sql += " ORDER BY category, name"

    rows = db.query(sql, args)
    counts = {
        r["dish_id"]: r["n"]
        for r in db.query(
            "SELECT dish_id, COUNT(*) AS n FROM dish_ingredients GROUP BY dish_id"
        )
    }
    return render_template(
        "dishes.html",
        dishes=rows,
        counts=counts,
        search=search,
        category=category,
        categories=DISH_CATEGORIES,
    )


@app.route("/dishes/<int:dish_id>")
@login_required
def dish_detail(dish_id):
    dish = db.query("SELECT * FROM dishes WHERE id = ?", (dish_id,), one=True)
    if dish is None:
        abort(404)
    ingredients = _dish_ingredients(dish_id)
    return render_template("dish_detail.html", dish=dish, ingredients=ingredients)


def _dish_ingredients(dish_id):
    return db.query(
        """SELECT di.id, di.quantity, di.unit, di.sort_order, i.name
             FROM dish_ingredients di
             JOIN ingredients i ON i.id = di.ingredient_id
            WHERE di.dish_id = ?
            ORDER BY di.sort_order, di.id""",
        (dish_id,),
    )


def _parse_ingredient_rows(form):
    """Pull ingredient rows off the form and validate them.

    The rules themselves live in scaling.validate_ingredient_rows so the
    document importer applies exactly the same ones - a row rejected here must
    also be rejected on the way in from a Word document.
    """
    return validate_ingredient_rows(
        form.getlist("ingredient_name"),
        form.getlist("ingredient_quantity"),
        form.getlist("ingredient_unit"),
    )


@app.route("/dishes/new", methods=["GET", "POST"])
@admin_required
def dish_new():
    if request.method == "POST":
        return _save_dish(None)
    return render_template(
        "dish_form.html",
        dish=None,
        ingredients=[],
        categories=DISH_CATEGORIES,
        units=UNITS,
    )


@app.route("/dishes/<int:dish_id>/edit", methods=["GET", "POST"])
@admin_required
def dish_edit(dish_id):
    dish = db.query("SELECT * FROM dishes WHERE id = ?", (dish_id,), one=True)
    if dish is None:
        abort(404)
    if request.method == "POST":
        return _save_dish(dish_id)
    return render_template(
        "dish_form.html",
        dish=dish,
        ingredients=_dish_ingredients(dish_id),
        categories=DISH_CATEGORIES,
        units=UNITS,
    )


def _save_dish(dish_id):
    name = request.form.get("name", "").strip()
    category = request.form.get("category", "").strip() or DISH_CATEGORIES[0]
    base_persons_raw = request.form.get("base_persons", "").strip()
    notes = request.form.get("notes", "").strip()

    rows, errors = _parse_ingredient_rows(request.form)

    if not name:
        errors.insert(0, "Dish name is required.")
    if category not in DISH_CATEGORIES:
        errors.append("Unknown category.")

    base_persons = None
    try:
        base_persons = int(base_persons_raw)
        if base_persons < 1:
            errors.append("Base number of persons must be at least 1.")
    except ValueError:
        errors.append("Base number of persons must be a whole number.")

    if not rows:
        errors.append("At least one ingredient with a quantity is required.")

    if name:
        clash_sql = "SELECT id FROM dishes WHERE name = ?"
        clash_args = [name]
        if dish_id:
            clash_sql += " AND id != ?"
            clash_args.append(dish_id)
        if db.query(clash_sql, clash_args, one=True):
            errors.append(f"A dish named '{name}' already exists.")

    if errors:
        for message in errors:
            flash(message, "danger")
        # Re-render with what they typed. Never make anyone key a whole recipe
        # in twice because of one bad row - including mid-import, where losing
        # the form would also lose their place in the document's queue.
        return render_template(
            "dish_form.html",
            dish={
                "id": dish_id,
                "name": name,
                "category": category,
                "base_persons": base_persons_raw,
                "notes": notes,
            },
            ingredients=rows,
            categories=DISH_CATEGORIES,
            units=UNITS,
            import_ctx=_current_import_ctx(),
        )

    if dish_id:
        db.execute(
            """UPDATE dishes
                  SET name = ?, category = ?, base_persons = ?, notes = ?,
                      updated_at = datetime('now', 'localtime')
                WHERE id = ?""",
            (name, category, base_persons, notes, dish_id),
        )
        db.execute("DELETE FROM dish_ingredients WHERE dish_id = ?", (dish_id,))
    else:
        dish_id = db.execute(
            """INSERT INTO dishes (name, category, base_persons, notes)
               VALUES (?, ?, ?, ?)""",
            (name, category, base_persons, notes),
        )

    for order, row in enumerate(rows):
        ingredient_id = db.get_or_create_ingredient(row["name"], row["unit"])
        db.execute(
            """INSERT INTO dish_ingredients
                   (dish_id, ingredient_id, quantity, unit, sort_order)
               VALUES (?, ?, ?, ?, ?)""",
            (dish_id, ingredient_id, row["quantity"], row["unit"], order),
        )

    flash(f"Recipe for '{name}' saved.", "success")

    # Mid-import, go straight to the next recipe from the document rather than
    # to the saved dish - the queue is the thing the admin is working through.
    if request.form.get("import_next") and session.get("import_token"):
        session["import_index"] = session.get("import_index", 0) + 1
        return redirect(url_for("dish_import_review"))

    return redirect(url_for("dish_detail", dish_id=dish_id))


# --- Importing recipe documents -------------------------------------------
#
# recipe_import is an optional add-on, imported lazily inside these routes and
# never at module scope. A Mess PC that only serves the app can have the folder
# and its dependencies absent entirely: importer_available() then returns False,
# the navigation hides the page, and the routes 404. The app must keep starting
# without it, which tests/test_app.py asserts.

IMPORT_UPLOAD_EXTENSIONS = {".docx", ".pdf", ".xlsx", ".xlsm",
                            ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}
MAX_IMPORT_BYTES = 25 * 1024 * 1024


def _load_importer():
    """Return the importer's cli module, or None when it is not installed."""
    try:
        from recipe_import import cli
    except ImportError:
        return None
    return cli


def importer_available():
    return _load_importer() is not None


def _import_store_path(token):
    folder = os.path.join(tempfile.gettempdir(), "mess_recipe_imports")
    os.makedirs(folder, exist_ok=True)
    # basename() so a token from the session can never walk out of the folder.
    return os.path.join(folder, f"{os.path.basename(token)}.json")


def _read_import_queue():
    """Load the parsed-but-unsaved recipes for this session, if any."""
    token = session.get("import_token")
    if not token:
        return None
    try:
        with open(_import_store_path(token), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        session.pop("import_token", None)
        session.pop("import_index", None)
        return None


def _current_import_ctx():
    """Import banner state for a form re-render, or None when not importing."""
    if not request.form.get("import_next"):
        return None
    queue = _read_import_queue()
    if not queue:
        return None
    index = session.get("import_index", 0)
    if index >= len(queue):
        return None
    return {
        "position": index + 1,
        "total": len(queue),
        "source_file": queue[index]["source_file"],
        "flagged": 0,  # the rows on screen are now what the admin typed
    }


def _clear_import_queue():
    token = session.pop("import_token", None)
    session.pop("import_index", None)
    if token:
        try:
            os.remove(_import_store_path(token))
        except OSError:
            pass


@app.route("/dishes/import", methods=["GET", "POST"])
@admin_required
def dish_import():
    importer = _load_importer()
    if importer is None:
        abort(404)

    if request.method == "GET":
        return render_template("dish_import.html")

    upload = request.files.get("document")
    if upload is None or not upload.filename:
        flash("Choose a document to import.", "danger")
        return redirect(url_for("dish_import"))

    filename = secure_filename(upload.filename)
    extension = os.path.splitext(filename)[1].lower()
    if extension not in IMPORT_UPLOAD_EXTENSIONS:
        flash(f"'{extension or filename}' is not a document type the importer reads.", "danger")
        return redirect(url_for("dish_import"))

    with tempfile.TemporaryDirectory() as workdir:
        saved = os.path.join(workdir, filename)
        upload.save(saved)

        if os.path.getsize(saved) > MAX_IMPORT_BYTES:
            flash("That file is larger than 25 MB.", "danger")
            return redirect(url_for("dish_import"))

        try:
            recipes = importer.build_recipes(
                [saved],
                use_llm=request.form.get("use_llm") == "on",
                known_names=[r["name"] for r in db.query("SELECT name FROM ingredients")],
            )
        except Exception as exc:  # a bad document must not 500 the app
            flash(f"Could not read that document: {exc}", "danger")
            return redirect(url_for("dish_import"))

    if not recipes:
        flash("No recipes were found in that document.", "warning")
        return redirect(url_for("dish_import"))

    token = secrets.token_urlsafe(16)
    payload = [
        {
            "dish_name": r.dish_name,
            "base_persons": r.base_persons,
            "category": r.category,
            "source_file": filename,
            "rows": [
                {
                    "name": row.name,
                    "quantity": row.quantity,
                    "unit": row.unit,
                    "status": row.status,
                    "note": row.note,
                    "source_line": row.source_line,
                    "suggested_name": row.suggested_name,
                }
                for row in r.rows
            ],
        }
        for r in recipes
    ]

    # Parked on disk rather than in the session cookie: a long recipe would blow
    # the 4KB cookie limit, and the cookie is not the place for a whole document.
    with open(_import_store_path(token), "w", encoding="utf-8") as fh:
        json.dump(payload, fh)

    session["import_token"] = token
    session["import_index"] = 0

    flagged = sum(1 for r in payload for row in r["rows"] if row["status"] != "ok")
    flash(
        f"Read {len(payload)} recipe(s) from {filename}."
        + (f" {flagged} row(s) need your attention." if flagged else ""),
        "success",
    )
    return redirect(url_for("dish_import_review"))


@app.route("/dishes/import/review")
@admin_required
def dish_import_review():
    """Show the next parsed recipe in the ordinary Add-recipe form.

    Deliberately the same form, and the same POST target, as adding a recipe by
    hand. The importer only pre-fills it - saving still runs the identical
    validation, so a flagged row cannot reach the database by arriving through a
    document instead of a keyboard.
    """
    if not importer_available():
        abort(404)

    queue = _read_import_queue()
    if not queue:
        flash("Nothing left to import.", "info")
        return redirect(url_for("dishes"))

    index = session.get("import_index", 0)
    if index >= len(queue):
        _clear_import_queue()
        flash("All recipes from that document have been dealt with.", "success")
        return redirect(url_for("dishes"))

    recipe = queue[index]
    return render_template(
        "dish_form.html",
        dish={
            "id": None,
            "name": recipe["dish_name"],
            "category": recipe["category"],
            "base_persons": recipe["base_persons"] or "",
            "notes": f"Imported from {recipe['source_file']}.",
        },
        ingredients=recipe["rows"],
        categories=DISH_CATEGORIES,
        units=UNITS,
        import_ctx={
            "position": index + 1,
            "total": len(queue),
            "source_file": recipe["source_file"],
            "flagged": sum(1 for r in recipe["rows"] if r["status"] != "ok"),
        },
    )


@app.route("/dishes/import/skip", methods=["POST"])
@admin_required
def dish_import_skip():
    if not importer_available():
        abort(404)
    session["import_index"] = session.get("import_index", 0) + 1
    return redirect(url_for("dish_import_review"))


@app.route("/dishes/import/cancel", methods=["POST"])
@admin_required
def dish_import_cancel():
    _clear_import_queue()
    flash("Import abandoned. Nothing was saved.", "info")
    return redirect(url_for("dishes"))


@app.route("/dishes/<int:dish_id>/delete", methods=["POST"])
@admin_required
def dish_delete(dish_id):
    dish = db.query("SELECT * FROM dishes WHERE id = ?", (dish_id,), one=True)
    if dish is None:
        abort(404)
    # Soft delete only. Past requisitions reference this row, and the Store's
    # historical record must keep resolving.
    db.execute("UPDATE dishes SET is_active = 0 WHERE id = ?", (dish_id,))
    flash(f"'{dish['name']}' removed from the active list. Past requisitions are unaffected.",
          "success")
    return redirect(url_for("dishes"))


@app.route("/api/ingredients")
@login_required
def api_ingredients():
    term = request.args.get("q", "").strip()
    if term:
        rows = db.query(
            "SELECT name, default_unit FROM ingredients WHERE name LIKE ? ORDER BY name LIMIT 20",
            (f"%{term}%",),
        )
    else:
        rows = db.query("SELECT name, default_unit FROM ingredients ORDER BY name LIMIT 200")
    return jsonify([{"name": r["name"], "unit": r["default_unit"]} for r in rows])


# --- Scaling and requisitions --------------------------------------------

@app.route("/calculate", methods=["GET", "POST"])
@login_required
def calculate():
    active_dishes = db.query(
        "SELECT id, name, category, base_persons FROM dishes WHERE is_active = 1 ORDER BY name"
    )

    if request.method == "POST":
        dish_id = request.form.get("dish_id", "")
        persons_raw = request.form.get("persons_required", "").strip()
        course_name = request.form.get("course_name", "").strip()
        meal_type = request.form.get("meal_type", "").strip()
        meal_date = request.form.get("meal_date", "").strip() or date.today().isoformat()

        errors = []
        dish = None
        if dish_id:
            dish = db.query("SELECT * FROM dishes WHERE id = ?", (dish_id,), one=True)
        if dish is None:
            errors.append("Please select a dish.")

        persons = None
        try:
            persons = int(persons_raw)
            if persons < 1:
                errors.append("Number of persons must be at least 1.")
        except ValueError:
            errors.append("Number of persons must be a whole number.")

        if meal_type not in MEAL_TYPES:
            errors.append("Please select a meal type.")

        ingredients = _dish_ingredients(dish["id"]) if dish else []
        if dish and not ingredients:
            errors.append(f"'{dish['name']}' has no ingredients recorded yet.")

        if errors:
            for message in errors:
                flash(message, "danger")
            return render_template(
                "calculate.html",
                dishes=active_dishes,
                meal_types=MEAL_TYPES,
                selected=request.form,
            )

        scaled = scale_recipe(ingredients, dish["base_persons"], persons)

        # Freeze the result. If the recipe is corrected next month, a reprint of
        # this requisition must still show what actually went to the Store.
        requisition_id = db.execute(
            """INSERT INTO requisitions
                   (dish_id, dish_name_snapshot, base_persons_snapshot, persons_required,
                    course_name, meal_type, meal_date, generated_by, generated_by_name)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                dish["id"],
                dish["name"],
                dish["base_persons"],
                persons,
                course_name,
                meal_type,
                meal_date,
                session["user_id"],
                session.get("full_name") or session.get("username"),
            ),
        )
        for item in scaled:
            db.execute(
                """INSERT INTO requisition_items
                       (requisition_id, ingredient_name, base_quantity, base_unit,
                        exact_quantity, display_quantity, display_unit, sort_order)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    requisition_id,
                    item["name"],
                    item["base_quantity"],
                    item["base_unit"],
                    item["exact"],
                    item["display_qty"],
                    item["display_unit"],
                    item["sort_order"],
                ),
            )

        return redirect(url_for("requisition_view", requisition_id=requisition_id))

    return render_template(
        "calculate.html",
        dishes=active_dishes,
        meal_types=MEAL_TYPES,
        selected={"meal_date": date.today().isoformat()},
    )


def _load_requisition(requisition_id):
    requisition = db.query(
        "SELECT * FROM requisitions WHERE id = ?", (requisition_id,), one=True
    )
    if requisition is None:
        abort(404)
    items = db.query(
        "SELECT * FROM requisition_items WHERE requisition_id = ? ORDER BY sort_order, id",
        (requisition_id,),
    )
    return requisition, items


@app.route("/requisition/<int:requisition_id>")
@login_required
def requisition_view(requisition_id):
    requisition, items = _load_requisition(requisition_id)
    return render_template("result.html", requisition=requisition, items=items)


@app.route("/requisition/<int:requisition_id>/pdf")
@login_required
def requisition_pdf(requisition_id):
    requisition, items = _load_requisition(requisition_id)
    buffer = build_pdf(requisition, items)
    return send_file(
        buffer,
        mimetype="application/pdf",
        as_attachment=False,
        download_name=export_filename(requisition, "pdf"),
    )


@app.route("/requisition/<int:requisition_id>/excel")
@login_required
def requisition_excel(requisition_id):
    requisition, items = _load_requisition(requisition_id)
    buffer = build_excel(requisition, items)
    return send_file(
        buffer,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=export_filename(requisition, "xlsx"),
    )


@app.route("/history")
@login_required
def history():
    search = request.args.get("q", "").strip()
    sql = "SELECT * FROM requisitions"
    args = []
    if search:
        sql += " WHERE dish_name_snapshot LIKE ? OR course_name LIKE ?"
        args = [f"%{search}%", f"%{search}%"]
    sql += " ORDER BY generated_at DESC, id DESC LIMIT 300"
    return render_template("history.html", requisitions=db.query(sql, args), search=search)


# --- Administration -------------------------------------------------------

@app.route("/users", methods=["GET", "POST"])
@admin_required
def users():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        full_name = request.form.get("full_name", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "staff")

        errors = []
        if not username:
            errors.append("Username is required.")
        if len(password) < 6:
            errors.append("Password must be at least 6 characters.")
        if role not in ("admin", "staff"):
            errors.append("Invalid role.")
        if username and db.query("SELECT id FROM users WHERE username = ?", (username,), one=True):
            errors.append(f"User '{username}' already exists.")

        if errors:
            for message in errors:
                flash(message, "danger")
        else:
            db.execute(
                """INSERT INTO users (username, full_name, password_hash, role,
                                      must_change_password)
                   VALUES (?, ?, ?, ?, 1)""",
                (username, full_name, generate_password_hash(password), role),
            )
            flash(f"User '{username}' created. They must change this password at first login.",
                  "success")
        return redirect(url_for("users"))

    return render_template(
        "users.html", users=db.query("SELECT * FROM users ORDER BY role, username")
    )


@app.route("/users/<int:user_id>/toggle", methods=["POST"])
@admin_required
def user_toggle(user_id):
    if user_id == session["user_id"]:
        flash("You cannot deactivate your own account.", "danger")
        return redirect(url_for("users"))
    user = db.query("SELECT * FROM users WHERE id = ?", (user_id,), one=True)
    if user is None:
        abort(404)
    db.execute("UPDATE users SET is_active = ? WHERE id = ?", (0 if user["is_active"] else 1, user_id))
    flash(f"User '{user['username']}' {'deactivated' if user['is_active'] else 'reactivated'}.",
          "success")
    return redirect(url_for("users"))


@app.route("/users/<int:user_id>/reset", methods=["POST"])
@admin_required
def user_reset(user_id):
    user = db.query("SELECT * FROM users WHERE id = ?", (user_id,), one=True)
    if user is None:
        abort(404)
    new_password = request.form.get("new_password", "")
    if len(new_password) < 6:
        flash("Password must be at least 6 characters.", "danger")
    else:
        db.execute(
            "UPDATE users SET password_hash = ?, must_change_password = 1 WHERE id = ?",
            (generate_password_hash(new_password), user_id),
        )
        flash(f"Password reset for '{user['username']}'. They must change it at next login.",
              "success")
    return redirect(url_for("users"))


@app.route("/admin/backup")
@admin_required
def admin_backup():
    """Download a consistent snapshot of mess.db.

    Deliberately NOT send_file('mess.db'): under WAL, recent commits may still
    be sitting in mess.db-wal, so streaming the main file alone can hand back a
    backup that silently omits the newest recipes - the worst possible failure
    mode for a backup feature. sqlite3's online backup API produces a coherent
    single-file copy of a live database instead.
    """
    tmp_dir = tempfile.mkdtemp(prefix="mess_backup_")
    path = os.path.join(tmp_dir, f"mess_backup_{date.today():%Y-%m-%d}.db")
    dest = sqlite3.connect(path)
    try:
        db.get_db().backup(dest)
    finally:
        dest.close()
    return send_file(path, as_attachment=True, download_name=os.path.basename(path))


# --- Error pages ----------------------------------------------------------

@app.errorhandler(403)
def forbidden(error):  # noqa: ARG001
    return render_template("error.html", code=403,
                           message="Administrator access is required for this action."), 403


@app.errorhandler(404)
def not_found(error):  # noqa: ARG001
    return render_template("error.html", code=404, message="Page not found."), 404


if __name__ == "__main__":
    seeded = db.init_db()
    if seeded:
        print("=" * 62)
        print(f"  First run: created administrator '{db.DEFAULT_ADMIN_USERNAME}' "
              f"with password '{db.DEFAULT_ADMIN_PASSWORD}'.")
        print("  You will be asked to change it immediately after logging in.")
        print("=" * 62)
    print(f"  Officers' Mess Recipe System running on http://localhost:{PORT}")
    print(f"  On the LAN, other PCs use http://<this-pc-ip>:{PORT}")
    # 0.0.0.0 is what makes the app reachable from other Mess PCs.
    app.run(host="0.0.0.0", port=PORT, debug=False)
