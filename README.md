# Officers' Mess — Recipe Scaling & Ingredient Requirement System

Holds the Mess's standard recipes once and works out ingredient quantities for
any number of diners, producing an item-wise requirement for the Store.

A recipe is entered for whatever strength it was originally written for —
Chicken Biryani for 10 persons, Dal Makhani for 15. When the same dish is needed
for a course of 60, 90, 120 or 180, the dish is selected, the strength entered,
and every quantity is scaled in direct proportion. Chicken at 3 kg for 10 persons
becomes 27 kg for 90.

---

## Getting started

Double-click **`run_app.bat`**. On the first run it creates a virtual
environment and installs the dependencies, which takes a minute or two.

Then open <http://localhost:5002>.

| First login | |
|---|---|
| Username | `admin` |
| Password | `admin123` |

**You will be required to change this password immediately.** Until you do, no
part of the system is reachable.

To install manually instead:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

---

## Using it from other Mess computers

The app listens on all network interfaces, so any PC on the same network can
reach it in a browser at:

```
http://<ip-of-the-pc-running-the-app>:5002
```

The address is printed in the console window when the app starts. For this to
work day to day:

- the PC running the app must stay switched on,
- it should have a fixed (static) IP address, otherwise the address changes,
- Windows Firewall must allow inbound connections on port 5002.

---

## Who can do what

| | Admin | Staff |
|---|---|---|
| Add, edit and remove recipes | Yes | Yes |
| Import recipes from a document | Yes | Yes |
| Calculate a requirement | Yes | Yes |
| Print / download PDF and Excel | Yes | Yes |
| View history | Yes | Yes |
| Manage users, download backups | Yes | **No** |

Everyone who can sign in can maintain the recipe book — the Mess cooks are the
people who know what a dish actually takes, so keeping recipes admin-only just
meant corrections never got made.

Only an Admin can create or deactivate accounts, reset passwords, or download
the database backup. That is enforced on the server, not merely by hiding
buttons: a Staff account that types `/users` or `/admin/backup` gets a 403.

Removing a recipe is never destructive (see *Past requirements are never
rewritten* below), so a mistaken deletion is recoverable — open the dish and
restore it.

---

## Day-to-day use

**Adding a recipe** — *Recipes → Add recipe*. Enter the dish name, the
number of persons the recipe as written serves, and the ingredients. Press
**Enter** in an ingredient row to jump to the next one. Ingredient names
autocomplete from those already entered, so recipes get quicker to key in as the
database grows. Blank rows at the bottom are ignored.

**Calculating a requirement** — *Calculate*. Choose the dish, enter the number
of persons (or click one of the common course strengths), choose the meal and
date, and generate. You get:

| Column | Meaning |
|---|---|
| Base qty | the recipe as written |
| Exact qty | the true proportional figure |
| **Required qty** | rounded up to a quantity the Store can issue |

Both figures are always shown. Nothing is silently adjusted.

**Getting it to the Store** — *Open PDF* for a formatted requisition with
signature blocks, *Download Excel* to edit or re-sort first, or *Print*
straight from the browser.

---

## How the rounding works

Quantities are rounded **up**, never down — a short indent stops the cooking,
while a slight excess does not.

| Unit | Rounded up to |
|---|---|
| kg, litre | nearest 0.05 |
| g, ml | nearest 5 |
| nos, packet, bunch | the next whole number |
| tsp, tbsp | nearest 0.5 |

Units are also shown in whatever reads most naturally: 4500 g appears as 4.5 kg,
0.45 kg appears as 450 g. The amount is unchanged — only its presentation.

---

## Past requirements are never rewritten

When a requirement is generated, the calculated figures are **stored as a frozen
copy**. Correcting a recipe afterwards, renaming a dish, or removing it from the
active list does not change any requisition that was already issued. Reprinting
an old requisition always reproduces exactly what went to the Store.

Removing a dish is a *soft* removal: it disappears from the active recipe list
but its history stays intact.

---

## Backups

The entire database is one file, `mess.db`. Take a copy regularly.

The safest way is *Users → Download backup* (Admin only), which produces a
consistent snapshot even while the app is in use. If you copy `mess.db` by hand
instead, shut the app down first — otherwise the most recent entries may still be
sitting in the write-ahead log file and would be missing from your copy.

---

## Running the tests

```bash
python -m pytest tests/ -v
```

69 tests covering the scaling arithmetic, access control, recipe validation,
snapshot integrity, exports and database behaviour under concurrent use.

---

## Not included in this version

Menu-wise planning, consolidating common ingredients across several dishes into
one indent, Store approval workflow, and stock/inventory tracking. The database
is structured so these can be added without rebuilding what is already here.

Also worth revisiting once real recipes are loaded: salt and whole spices do not
genuinely scale in strict proportion, and this will be noticeable at 180 persons.
The system currently scales everything linearly, as specified.
