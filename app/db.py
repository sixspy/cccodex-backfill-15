import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import Config


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None：事务边界由 get_db(immediate=True) 显式掌握，
    # 重放落库与写证必须落在同一个 BEGIN IMMEDIATE ... COMMIT 里。
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def get_db(db_path: str | None = None, immediate: bool = False):
    conn = connect(db_path)
    try:
        if immediate:
            # 排他写锁：两名对账员同时抢重放/出证时，后到者等锁后再读，
            # 不会各自写出一条凭证行。
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        if conn.in_transaction:
            conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS backfill_windows (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('草稿','已计划','执行中','已完成')),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS backfill_slices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  window_id INTEGER NOT NULL REFERENCES backfill_windows(id),
  slice_date TEXT NOT NULL,
  slice_key TEXT,
  status TEXT NOT NULL CHECK (status IN ('待重放','已落库','失败')),
  row_count INTEGER NOT NULL DEFAULT 0,
  UNIQUE(window_id, slice_date)
);

-- 审计凭证：一片一证，slice_id 唯一即排他，杜绝双写凭证行。
-- 凭证只随落库同事务产生，不设事后补证通路。
CREATE TABLE IF NOT EXISTS audit_credentials (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  credential_no TEXT NOT NULL UNIQUE,
  window_id INTEGER NOT NULL REFERENCES backfill_windows(id),
  slice_id INTEGER NOT NULL UNIQUE REFERENCES backfill_slices(id),
  slice_key TEXT NOT NULL,
  operator TEXT NOT NULL,
  row_count INTEGER NOT NULL,
  digest TEXT NOT NULL,
  issued_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS audit_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  window_id INTEGER,
  slice_id INTEGER,
  action TEXT NOT NULL,
  outcome TEXT NOT NULL,
  credential_no TEXT,
  operator TEXT NOT NULL,
  detail TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS coverage_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  window_id INTEGER NOT NULL,
  planned INTEGER NOT NULL,
  loaded INTEGER NOT NULL,
  certified INTEGER NOT NULL DEFAULT 0,
  uncertified INTEGER NOT NULL DEFAULT 0,
  missing INTEGER NOT NULL,
  summary TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ledger_slice ON audit_ledger(slice_id);
CREATE INDEX IF NOT EXISTS idx_ledger_window ON audit_ledger(window_id);
CREATE INDEX IF NOT EXISTS idx_credentials_window ON audit_credentials(window_id);
"""


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn: sqlite3.Connection) -> None:
    """为旧库补列；新库由 SCHEMA 直接建好。"""
    if "slice_key" not in _columns(conn, "backfill_slices"):
        conn.execute("ALTER TABLE backfill_slices ADD COLUMN slice_key TEXT")

    report_cols = _columns(conn, "coverage_reports")
    if "certified" not in report_cols:
        conn.execute(
            "ALTER TABLE coverage_reports ADD COLUMN certified INTEGER NOT NULL DEFAULT 0"
        )
    if "uncertified" not in report_cols:
        conn.execute(
            "ALTER TABLE coverage_reports ADD COLUMN uncertified INTEGER NOT NULL DEFAULT 0"
        )

    # 旧切片的片键按 表名#日期 回填，保证台账/详情/目录读得到同一个键。
    conn.execute(
        "UPDATE backfill_slices SET slice_key = ("
        "  SELECT w.table_name || '#' || backfill_slices.slice_date "
        "  FROM backfill_windows w WHERE w.id = backfill_slices.window_id"
        ") WHERE slice_key IS NULL"
    )


def init_db(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        conn.executescript(SCHEMA)
        conn.execute("BEGIN IMMEDIATE")
        _migrate(conn)
        if conn.execute("SELECT value FROM meta WHERE key='seeded'").fetchone():
            return
        cur = conn.execute(
            "INSERT INTO backfill_windows(table_name, start_date, end_date, status) "
            "VALUES ('dw_orders_d','2026-11-10','2026-11-12','草稿')"
        )
        wid = cur.lastrowid
        conn.execute(
            "INSERT INTO backfill_windows(table_name, start_date, end_date, status) "
            "VALUES ('dw_payments_d','2026-11-11','2026-11-11','草稿')"
        )
        # placeholder so list is non-empty; slices created by plan
        _ = wid
        conn.execute("INSERT INTO meta(key, value) VALUES ('seeded','1')")
