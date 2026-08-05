-- Officers' Mess - Recipe Scaling & Ingredient Requirement System
-- Schema is normalised so that menu-wise consolidation (V2) becomes a
-- GROUP BY over dish_ingredients.ingredient_id rather than a migration.

CREATE TABLE IF NOT EXISTS users (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    username             TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    full_name            TEXT    NOT NULL DEFAULT '',
    password_hash        TEXT    NOT NULL,
    role                 TEXT    NOT NULL CHECK (role IN ('admin', 'staff')),
    must_change_password INTEGER NOT NULL DEFAULT 0,
    is_active            INTEGER NOT NULL DEFAULT 1,
    created_at           TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Master ingredient list. Powers autocomplete today; keeps "Onion", "onions"
-- and "Onion " from fragmenting into three separate items, which is what makes
-- cross-dish consolidation possible later.
CREATE TABLE IF NOT EXISTS ingredients (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    default_unit TEXT NOT NULL DEFAULT 'kg',
    created_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS dishes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    category     TEXT    NOT NULL DEFAULT 'Main Course',
    base_persons INTEGER NOT NULL CHECK (base_persons > 0),
    notes        TEXT    NOT NULL DEFAULT '',
    is_active    INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at   TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS dish_ingredients (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    dish_id       INTEGER NOT NULL REFERENCES dishes (id)      ON DELETE CASCADE,
    ingredient_id INTEGER NOT NULL REFERENCES ingredients (id) ON DELETE RESTRICT,
    quantity      REAL    NOT NULL CHECK (quantity > 0),
    unit          TEXT    NOT NULL,
    sort_order    INTEGER NOT NULL DEFAULT 0,
    UNIQUE (dish_id, ingredient_id)
);

-- A requisition is a FROZEN record of what was submitted to the Store.
-- The *_snapshot columns mean a reprint survives the dish being renamed,
-- re-costed or soft-deleted afterwards.
CREATE TABLE IF NOT EXISTS requisitions (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    dish_id               INTEGER          REFERENCES dishes (id) ON DELETE SET NULL,
    dish_name_snapshot    TEXT    NOT NULL,
    base_persons_snapshot INTEGER NOT NULL,
    persons_required      INTEGER NOT NULL CHECK (persons_required > 0),
    course_name           TEXT    NOT NULL DEFAULT '',
    meal_type             TEXT    NOT NULL
                                  CHECK (meal_type IN ('Breakfast', 'Lunch', 'High Tea',
                                                       'Dinner', 'Special Event')),
    meal_date             TEXT    NOT NULL,
    generated_by          INTEGER          REFERENCES users (id) ON DELETE SET NULL,
    generated_by_name     TEXT    NOT NULL DEFAULT '',
    generated_at          TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS requisition_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    requisition_id  INTEGER NOT NULL REFERENCES requisitions (id) ON DELETE CASCADE,
    ingredient_name TEXT    NOT NULL,
    base_quantity   REAL    NOT NULL,
    base_unit       TEXT    NOT NULL,
    exact_quantity  REAL    NOT NULL,
    display_quantity REAL   NOT NULL,
    display_unit    TEXT    NOT NULL,
    sort_order      INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_dish_ingredients_dish  ON dish_ingredients (dish_id);
CREATE INDEX IF NOT EXISTS idx_requisition_items_req  ON requisition_items (requisition_id);
CREATE INDEX IF NOT EXISTS idx_requisitions_generated ON requisitions (generated_at DESC);
CREATE INDEX IF NOT EXISTS idx_dishes_active          ON dishes (is_active, name);
