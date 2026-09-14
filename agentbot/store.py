import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path.cwd() / "agentbot.db"


def _connect():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = _connect()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS bindings (
            chat_id         INTEGER PRIMARY KEY,
            session_id      TEXT NOT NULL,
            cwd             TEXT NOT NULL,
            model           TEXT,
            permission_mode TEXT,
            effort          TEXT,
            verbose         INTEGER DEFAULT 1,
            name            TEXT,
            created_at      TEXT NOT NULL,
            last_used       TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS usage (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id         INTEGER NOT NULL,
            session_id      TEXT NOT NULL,
            ts              TEXT NOT NULL,
            cost_usd        REAL,
            input_tokens    INTEGER,
            output_tokens   INTEGER,
            cache_read      INTEGER,
            cache_creation  INTEGER,
            num_turns       INTEGER,
            duration_ms     INTEGER
        );
    """)
    conn.commit()
    conn.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def get_binding(chat_id: int) -> dict | None:
    conn = _connect()
    row = conn.execute("SELECT * FROM bindings WHERE chat_id = ?", (chat_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def set_binding(chat_id: int, session_id: str, cwd: str, model: str,
                permission_mode: str, effort: str = None, name: str = None):
    conn = _connect()
    now = now_iso()
    conn.execute("""
        INSERT INTO bindings (chat_id, session_id, cwd, model, permission_mode,
                              effort, name, created_at, last_used)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            session_id = excluded.session_id,
            cwd = excluded.cwd,
            model = excluded.model,
            permission_mode = excluded.permission_mode,
            effort = excluded.effort,
            name = excluded.name,
            last_used = excluded.last_used
    """, (chat_id, session_id, cwd, model, permission_mode, effort, name, now, now))
    conn.commit()
    conn.close()


def touch_binding(chat_id: int):
    conn = _connect()
    conn.execute("UPDATE bindings SET last_used = ? WHERE chat_id = ?",
                 (now_iso(), chat_id))
    conn.commit()
    conn.close()


def update_binding(chat_id: int, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [now_iso(), chat_id]
    conn = _connect()
    conn.execute(f"UPDATE bindings SET {cols}, last_used = ? WHERE chat_id = ?", vals)
    conn.commit()
    conn.close()


def delete_binding(chat_id: int):
    conn = _connect()
    conn.execute("DELETE FROM bindings WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()


def all_bindings() -> list[dict]:
    conn = _connect()
    rows = conn.execute("SELECT * FROM bindings ORDER BY last_used DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def record_usage(chat_id: int, session_id: str, cost_usd: float = None,
                 input_tokens: int = None, output_tokens: int = None,
                 cache_read: int = None, cache_creation: int = None,
                 num_turns: int = None, duration_ms: int = None):
    conn = _connect()
    conn.execute("""
        INSERT INTO usage (chat_id, session_id, ts, cost_usd, input_tokens,
                           output_tokens, cache_read, cache_creation,
                           num_turns, duration_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (chat_id, session_id, now_iso(), cost_usd, input_tokens,
          output_tokens, cache_read, cache_creation, num_turns, duration_ms))
    conn.commit()
    conn.close()


def session_usage(session_id: str) -> dict:
    conn = _connect()
    row = conn.execute("""
        SELECT COUNT(*) as turns, SUM(cost_usd) as total_cost,
               SUM(input_tokens) as total_input, SUM(output_tokens) as total_output,
               SUM(duration_ms) as total_duration
        FROM usage WHERE session_id = ?
    """, (session_id,)).fetchone()
    conn.close()
    return dict(row)


def all_usage() -> dict:
    conn = _connect()
    row = conn.execute("""
        SELECT COUNT(*) as turns, SUM(cost_usd) as total_cost,
               SUM(input_tokens) as total_input, SUM(output_tokens) as total_output
        FROM usage
    """).fetchone()
    conn.close()
    return dict(row)


def week_usage() -> dict:
    conn = _connect()
    row = conn.execute("""
        SELECT COUNT(*) as turns,
               COUNT(DISTINCT session_id) as sessions,
               SUM(input_tokens) as total_input,
               SUM(output_tokens) as total_output,
               SUM(duration_ms) as total_duration
        FROM usage
        WHERE ts >= datetime('now', '-7 days')
    """).fetchone()
    conn.close()
    return dict(row)


def today_usage() -> dict:
    conn = _connect()
    row = conn.execute("""
        SELECT COUNT(*) as turns,
               COUNT(DISTINCT session_id) as sessions,
               SUM(input_tokens) as total_input,
               SUM(output_tokens) as total_output,
               SUM(duration_ms) as total_duration
        FROM usage
        WHERE ts >= datetime('now', 'start of day')
    """).fetchone()
    conn.close()
    return dict(row)
