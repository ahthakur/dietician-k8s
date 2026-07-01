"""
Personal Dietician — Database Layer
"""

from __future__ import annotations
from typing import Optional, List

import sqlite3
import json
from datetime import date, datetime
from config import DB_PATH


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS fridge_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            quantity TEXT DEFAULT '',
            category TEXT DEFAULT 'other',
            added_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS food_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            log_date TEXT NOT NULL,
            user_id TEXT NOT NULL,
            time_logged TEXT DEFAULT (datetime('now')),
            description TEXT NOT NULL,
            calories INTEGER DEFAULT 0,
            protein INTEGER DEFAULT 0,
            fiber INTEGER DEFAULT 0,
            carbs INTEGER DEFAULT 0,
            fat INTEGER DEFAULT 0,
            source TEXT DEFAULT 'manual',
            photo_analysis TEXT DEFAULT '',
            meal_ref TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS reminder_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            log_date TEXT NOT NULL,
            reminder_type TEXT NOT NULL,
            reminder_time TEXT NOT NULL,
            acknowledged INTEGER DEFAULT 0,
            ack_at TEXT,
            UNIQUE(log_date, reminder_type, reminder_time)
        );

        CREATE TABLE IF NOT EXISTS weekly_protein_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            week_start TEXT NOT NULL,
            protein_type TEXT NOT NULL,
            used_date TEXT NOT NULL,
            UNIQUE(week_start, protein_type)
        );

        CREATE TABLE IF NOT EXISTS grocery_lists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            week_start TEXT NOT NULL,
            items_json TEXT NOT NULL,
            generated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS quick_log_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            description TEXT UNIQUE NOT NULL,
            calories INTEGER DEFAULT 0,
            protein INTEGER DEFAULT 0,
            fiber INTEGER DEFAULT 0,
            times_used INTEGER DEFAULT 1,
            last_used TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS meal_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dish_name TEXT NOT NULL,
            plan_date TEXT NOT NULL,
            meal_slot TEXT NOT NULL,
            logged_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS purchase_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_name TEXT NOT NULL,
            quantity TEXT DEFAULT '',
            unit_price TEXT DEFAULT '',
            store TEXT DEFAULT '',
            purchased_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            chat_date TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS users (
            google_id TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            name TEXT NOT NULL,
            picture TEXT DEFAULT '',
            calories_min INTEGER DEFAULT 1800,
            calories_max INTEGER DEFAULT 2000,
            protein_target INTEGER DEFAULT 150,
            fiber_target INTEGER DEFAULT 30,
            timezone TEXT DEFAULT 'America/Chicago',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
    """)

    for col_sql in [
        "ALTER TABLE fridge_items ADD COLUMN quantity TEXT DEFAULT ''",
        "ALTER TABLE food_log ADD COLUMN fiber INTEGER DEFAULT 0",
        "ALTER TABLE quick_log_history ADD COLUMN fiber INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN calories_min INTEGER DEFAULT 1800",
        "ALTER TABLE users ADD COLUMN timezone TEXT DEFAULT 'America/Chicago'",
    ]:
        try:
            conn.execute(col_sql)
        except sqlite3.OperationalError:
            pass

    conn.commit()
    conn.close()


# ── Fridge ───────────────────────────────────────

def get_fridge_items():
    conn = get_conn()
    rows = conn.execute("SELECT id, name, quantity, category, added_at FROM fridge_items ORDER BY category, name").fetchall()
    conn.close()
    return [dict(r) for r in rows]

def add_fridge_item(name, category="other", quantity=""):
    conn = get_conn()
    try:
        conn.execute("INSERT INTO fridge_items (name, category, quantity) VALUES (?, ?, ?) ON CONFLICT(name) DO UPDATE SET quantity = excluded.quantity, added_at = datetime('now')", (name.strip(), category, quantity))
        conn.commit()
    finally:
        conn.close()

def add_fridge_items_bulk(items):
    conn = get_conn()
    try:
        for item in items:
            if isinstance(item, dict):
                name = item.get("name", "").strip()
                qty = item.get("quantity", "")
                cat = item.get("category", "other")
                if name:
                    conn.execute("INSERT INTO fridge_items (name, quantity, category) VALUES (?, ?, ?) ON CONFLICT(name) DO UPDATE SET quantity = excluded.quantity, category = excluded.category, added_at = datetime('now')", (name, qty, cat))
            elif isinstance(item, str) and item.strip():
                conn.execute("INSERT OR IGNORE INTO fridge_items (name) VALUES (?)", (item.strip(),))
        conn.commit()
    finally:
        conn.close()

def update_fridge_item(item_id, name=None, quantity=None, category=None):
    conn = get_conn()
    updates, vals = [], []
    if name is not None:
        updates.append("name = ?"); vals.append(name.strip())
    if quantity is not None:
        updates.append("quantity = ?"); vals.append(quantity)
    if category is not None:
        updates.append("category = ?"); vals.append(category)
    if updates:
        vals.append(item_id)
        conn.execute(f"UPDATE fridge_items SET {', '.join(updates)} WHERE id = ?", vals)
        conn.commit()
    conn.close()

def remove_fridge_item(name):
    conn = get_conn()
    conn.execute("DELETE FROM fridge_items WHERE name = ?", (name,))
    conn.commit()
    conn.close()

def remove_fridge_item_by_id(item_id):
    conn = get_conn()
    conn.execute("DELETE FROM fridge_items WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()

def clear_fridge():
    conn = get_conn()
    conn.execute("DELETE FROM fridge_items")
    conn.commit()
    conn.close()


# ── Food Log ─────────────────────────────────────

def add_food_log_entry(log_date, user_id, description, calories, protein, fiber=0, carbs=0, fat=0, source="manual", photo_analysis="", meal_ref=""):
    conn = get_conn()
    conn.execute(
        """INSERT INTO food_log (log_date, user_id, description, calories, protein, fiber, carbs, fat, source, photo_analysis, meal_ref)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (log_date, user_id, description, calories, protein, fiber, carbs, fat, source, photo_analysis, meal_ref),
    )
    conn.commit()
    conn.close()

def get_food_log(log_date, user_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, description, calories, protein, fiber, carbs, fat, source, photo_analysis, meal_ref, time_logged FROM food_log WHERE log_date = ? AND user_id = ? ORDER BY time_logged ASC",
        (log_date, user_id),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def delete_food_log_entry(entry_id):
    conn = get_conn()
    conn.execute("DELETE FROM food_log WHERE id = ?", (entry_id,))
    conn.commit()
    conn.close()

def update_food_log_entry(entry_id, calories, protein, fiber=0, description=None):
    conn = get_conn()
    if description is not None:
        conn.execute("UPDATE food_log SET calories = ?, protein = ?, fiber = ?, description = ? WHERE id = ?", (calories, protein, fiber, description, entry_id))
    else:
        conn.execute("UPDATE food_log SET calories = ?, protein = ?, fiber = ? WHERE id = ?", (calories, protein, fiber, entry_id))
    conn.commit()
    conn.close()

def get_food_log_totals(log_date, user_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT COALESCE(SUM(calories), 0) as total_cal, COALESCE(SUM(protein), 0) as total_protein, COALESCE(SUM(fiber), 0) as total_fiber FROM food_log WHERE log_date = ? AND user_id = ?",
        (log_date, user_id),
    ).fetchone()
    conn.close()
    return {"calories": row["total_cal"], "protein": row["total_protein"], "fiber": row["total_fiber"]}

def get_food_log_history(user_id, days=7, reference_date=None):
    ref = reference_date or str(date.today())
    conn = get_conn()
    rows = conn.execute(
        """SELECT log_date,
           COALESCE(SUM(calories), 0) as total_cal,
           COALESCE(SUM(protein), 0) as total_protein,
           COALESCE(SUM(fiber), 0) as total_fiber,
           COUNT(*) as entry_count
           FROM food_log
           WHERE user_id = ? AND log_date >= date(?, ? || ' days')
           GROUP BY log_date ORDER BY log_date ASC""",
        (user_id, ref, f"-{days}"),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def check_food_log_for_keyword(log_date, keyword):
    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM food_log WHERE log_date = ? AND LOWER(description) LIKE ?",
        (log_date, f"%{keyword.lower()}%"),
    ).fetchone()
    conn.close()
    return row["cnt"] > 0


# ── Quick Log History ────────────────────────────

def save_quick_log(description, calories, protein, fiber=0):
    conn = get_conn()
    conn.execute(
        """INSERT INTO quick_log_history (description, calories, protein, fiber, times_used, last_used)
           VALUES (?, ?, ?, ?, 1, datetime('now'))
           ON CONFLICT(description) DO UPDATE SET
           times_used = times_used + 1, last_used = datetime('now'),
           calories = excluded.calories, protein = excluded.protein, fiber = excluded.fiber""",
        (description.strip().lower(), calories, protein, fiber),
    )
    conn.commit()
    conn.close()

def get_frequent_quick_logs(limit=10):
    conn = get_conn()
    rows = conn.execute("SELECT description, calories, protein, fiber, times_used FROM quick_log_history ORDER BY times_used DESC, last_used DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def search_quick_logs(query):
    conn = get_conn()
    rows = conn.execute("SELECT description, calories, protein, fiber, times_used FROM quick_log_history WHERE description LIKE ? ORDER BY times_used DESC LIMIT 5", (f"%{query.lower()}%",)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Reminders ────────────────────────────────────

def log_reminder_sent(log_date, reminder_type, reminder_time):
    conn = get_conn()
    conn.execute("INSERT OR IGNORE INTO reminder_log (log_date, reminder_type, reminder_time) VALUES (?, ?, ?)", (log_date, reminder_type, reminder_time))
    conn.commit()
    conn.close()

def ack_reminder(log_date, reminder_type, reminder_time):
    conn = get_conn()
    conn.execute("INSERT OR IGNORE INTO reminder_log (log_date, reminder_type, reminder_time, acknowledged) VALUES (?, ?, ?, 0)", (log_date, reminder_type, reminder_time))
    conn.execute("UPDATE reminder_log SET acknowledged = 1, ack_at = datetime('now') WHERE log_date = ? AND reminder_type = ? AND reminder_time = ?", (log_date, reminder_type, reminder_time))
    conn.commit()
    conn.close()

def was_reminder_sent(log_date, reminder_type, reminder_time):
    conn = get_conn()
    row = conn.execute("SELECT COUNT(*) as cnt FROM reminder_log WHERE log_date = ? AND reminder_type = ? AND reminder_time = ?", (log_date, reminder_type, reminder_time)).fetchone()
    conn.close()
    return row["cnt"] > 0


# ── Meal History ─────────────────────────────────

def log_meal_to_history(dish_name, plan_date, meal_slot):
    conn = get_conn()
    conn.execute("INSERT INTO meal_history (dish_name, plan_date, meal_slot) VALUES (?, ?, ?)", (dish_name, plan_date, meal_slot))
    conn.commit()
    conn.close()

def get_recent_dishes(days=14):
    conn = get_conn()
    rows = conn.execute("SELECT DISTINCT dish_name FROM meal_history WHERE plan_date >= date('now', ? || ' days') ORDER BY plan_date DESC", (f"-{days}",)).fetchall()
    conn.close()
    return [r["dish_name"] for r in rows]


# ── Weekly Protein ───────────────────────────────

def was_weekly_protein_used(week_start, protein_type):
    conn = get_conn()
    row = conn.execute("SELECT COUNT(*) as cnt FROM weekly_protein_log WHERE week_start = ? AND protein_type = ?", (week_start, protein_type)).fetchone()
    conn.close()
    return row["cnt"] > 0

def mark_weekly_protein_used(week_start, protein_type, used_date):
    conn = get_conn()
    conn.execute("INSERT OR IGNORE INTO weekly_protein_log (week_start, protein_type, used_date) VALUES (?, ?, ?)", (week_start, protein_type, used_date))
    conn.commit()
    conn.close()


# ── Purchase Log ─────────────────────────────────

def log_purchases_bulk(items, store=""):
    conn = get_conn()
    for item in items:
        conn.execute(
            "INSERT INTO purchase_log (item_name, quantity, unit_price, store) VALUES (?, ?, ?, ?)",
            (item.get("name", "").strip().lower(), item.get("quantity", ""), item.get("price", ""), store),
        )
    conn.commit()
    conn.close()

def get_purchase_frequency(limit=20):
    conn = get_conn()
    rows = conn.execute(
        """SELECT item_name, COUNT(*) as times_bought, MAX(purchased_at) as last_bought
           FROM purchase_log GROUP BY item_name ORDER BY times_bought DESC LIMIT ?""", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Grocery ─────────────────────────────────────

def save_grocery_list(week_start, items):
    conn = get_conn()
    conn.execute("INSERT OR REPLACE INTO grocery_lists (week_start, items_json, generated_at) VALUES (?, ?, datetime('now'))", (week_start, json.dumps(items)))
    conn.commit()
    conn.close()

def get_grocery_list(week_start):
    conn = get_conn()
    row = conn.execute("SELECT items_json FROM grocery_lists WHERE week_start = ?", (week_start,)).fetchone()
    conn.close()
    return json.loads(row["items_json"]) if row else None


# ── Chat History ─────────────────────────────────

def save_chat_message(user_id, role, content, chat_date):
    conn = get_conn()
    conn.execute("INSERT INTO chat_history (user_id, role, content, chat_date) VALUES (?, ?, ?, ?)", (user_id, role, content, chat_date))
    conn.commit()
    conn.close()

def get_chat_history(user_id, chat_date, limit=20):
    conn = get_conn()
    rows = conn.execute(
        "SELECT role, content, created_at FROM chat_history WHERE user_id = ? AND chat_date = ? ORDER BY created_at ASC LIMIT ?",
        (user_id, chat_date, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Users ───────────────────────────────────────

def create_or_update_user(google_id, email, name, picture=""):
    conn = get_conn()
    existing = conn.execute("SELECT google_id FROM users WHERE google_id = ?", (google_id,)).fetchone()
    if existing:
        conn.execute(
            "UPDATE users SET email = ?, name = ?, picture = ?, updated_at = datetime('now') WHERE google_id = ?",
            (email, name, picture, google_id),
        )
    else:
        conn.execute(
            "INSERT INTO users (google_id, email, name, picture) VALUES (?, ?, ?, ?)",
            (google_id, email, name, picture),
        )
    conn.commit()
    conn.close()

def get_user(google_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE google_id = ?", (google_id,)).fetchone()
    conn.close()
    if not row:
        return None
    return dict(row)

def update_user_targets(google_id, calories_max, protein_target, fiber_target):
    conn = get_conn()
    conn.execute(
        "UPDATE users SET calories_max = ?, protein_target = ?, fiber_target = ?, updated_at = datetime('now') WHERE google_id = ?",
        (calories_max, protein_target, fiber_target, google_id),
    )
    conn.commit()
    conn.close()

def update_user_timezone(google_id, timezone):
    conn = get_conn()
    conn.execute(
        "UPDATE users SET timezone = ?, updated_at = datetime('now') WHERE google_id = ?",
        (timezone, google_id),
    )
    conn.commit()
    conn.close()
