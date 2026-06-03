"""Search Hermes ~/.hermes/state.db session history (FTS5)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SessionHit:
    session_id: str
    role: str
    content: str
    created_at: float | None = None


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {r[1] for r in rows}
    except sqlite3.Error:
        return set()


def _detect_schema(conn: sqlite3.Connection) -> dict:
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    info: dict = {"tables": tables}

    for name in ("messages", "session_messages", "chat_messages"):
        if name in tables:
            info["messages_table"] = name
            info["messages_cols"] = _table_columns(conn, name)
            break

    for name in ("messages_fts", "session_messages_fts", "fts_messages"):
        if name in tables:
            info["fts_table"] = name
            break

    return info


def search_hermes_sessions(
    db_path: Path,
    query: str,
    *,
    limit: int = 12,
) -> list[SessionHit]:
    if not db_path.is_file() or not query.strip():
        return []

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        schema = _detect_schema(conn)
        hits: list[SessionHit] = []

        fts = schema.get("fts_table")
        msg_table = schema.get("messages_table")
        if fts:
            try:
                rows = conn.execute(
                    f"""
                    SELECT rowid, content FROM {fts}
                    WHERE {fts} MATCH ?
                    LIMIT ?
                    """,
                    (query, limit),
                ).fetchall()
                for row in rows:
                    hits.append(
                        SessionHit(
                            session_id=str(row[0]),
                            role="unknown",
                            content=str(row[1] if len(row) > 1 else row[0]),
                        )
                    )
                if hits:
                    return hits
            except sqlite3.Error:
                pass

        if msg_table:
            cols = schema.get("messages_cols") or set()
            content_col = next(
                (c for c in ("content", "text", "message", "body") if c in cols),
                None,
            )
            if content_col:
                session_col = next(
                    (c for c in ("session_id", "conversation_id", "chat_id") if c in cols),
                    None,
                )
                role_col = "role" if "role" in cols else None
                time_col = next(
                    (c for c in ("created_at", "timestamp", "ts") if c in cols),
                    None,
                )
                sql = f"""
                    SELECT * FROM {msg_table}
                    WHERE {content_col} LIKE ?
                    ORDER BY {time_col or 'rowid'} DESC
                    LIMIT ?
                """
                rows = conn.execute(sql, (f"%{query}%", limit)).fetchall()
                for row in rows:
                    hits.append(
                        SessionHit(
                            session_id=str(row[session_col]) if session_col else "",
                            role=str(row[role_col]) if role_col else "unknown",
                            content=str(row[content_col]),
                            created_at=float(row[time_col])
                            if time_col and row[time_col] is not None
                            else None,
                        )
                    )
        return hits
    finally:
        conn.close()


def format_hits_for_prompt(hits: list[SessionHit], max_chars: int = 4000) -> str:
    if not hits:
        return ""
    lines: list[str] = []
    total = 0
    for h in hits:
        line = f"[{h.role}] {h.content[:500]}"
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines)
