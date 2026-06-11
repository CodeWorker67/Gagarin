"""
Adds multi-device subscription columns to users and gifts.device_slots if missing.

SQLite ALTER TABLE applies to all rows at once; nullable columns are NULL for
existing users.

Run from project root on VPS:
  python migrate_add_subscription_3_10_columns.py
"""
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "config_bd" / "regionvpn.db"

USERS_MIGRATIONS = [
    ("subscription_3_end_date", "DATETIME"),
    ("subscription_10_end_date", "DATETIME"),
    ("subscribtion_3", "TEXT"),
    ("subscribtion_10", "TEXT"),
]


def _add_column_if_missing(conn: sqlite3.Connection, table: str, name: str, coldef: str) -> None:
    cur = conn.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cur.fetchall()}
    if name in existing:
        print(f"skip (exists): {table}.{name}")
        return
    sql = f'ALTER TABLE "{table}" ADD COLUMN "{name}" {coldef}'
    conn.execute(sql)
    print(f"ok: {table}.{name}")


def main() -> None:
    if not DB_PATH.is_file():
        raise SystemExit(f"Database not found: {DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    try:
        for name, coldef in USERS_MIGRATIONS:
            _add_column_if_missing(conn, "users", name, coldef)
        _add_column_if_missing(conn, "gifts", "device_slots", "INTEGER DEFAULT 5")
        conn.commit()
    finally:
        conn.close()

    print("Done.")


if __name__ == "__main__":
    main()
