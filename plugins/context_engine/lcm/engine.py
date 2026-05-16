from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from agent.context_engine import ContextEngine
from hermes_constants import get_config_path, get_hermes_home


class LCMContextEngine(ContextEngine):
    """Lossless context engine with DAG summary nodes + FTS recall.

    Phase-2 upgrades:
    - Summary nodes are hierarchical (parent_summary_id + depth)
    - `lcm_grep` uses SQLite FTS5 when available, with LIKE fallback
    - Session stats expose DAG depth and latest parent linkage
    """

    def __init__(self, context_length: int = 200_000, threshold_percent: float = 0.75):
        cfg = self._read_lcm_config()

        env_threshold = os.getenv("HERMES_LCM_THRESHOLD_PERCENT")
        if env_threshold:
            try:
                threshold_percent = float(env_threshold)
            except ValueError:
                pass
        elif cfg.get("threshold_percent") is not None:
            try:
                threshold_percent = float(cfg.get("threshold_percent"))
            except (TypeError, ValueError):
                pass
        threshold_percent = max(0.1, min(0.95, threshold_percent))

        self.context_length = context_length
        self.threshold_percent = threshold_percent
        self.threshold_tokens = int(context_length * threshold_percent)

        head_keep = os.getenv("HERMES_LCM_HEAD_KEEP")
        tail_keep = os.getenv("HERMES_LCM_TAIL_KEEP")
        self._head_keep = int(head_keep or cfg.get("head_keep", 2) or 2)
        self._tail_keep = int(tail_keep or cfg.get("tail_keep", 16) or 16)
        self._head_keep = max(1, min(20, self._head_keep))
        self._tail_keep = max(4, min(128, self._tail_keep))

        self.compression_count = 0
        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0
        self.last_total_tokens = 0
        self._session_id = "unknown"
        self._db_path = self._resolve_db_path()
        self._fts_enabled = False
        self._init_db()

    @property
    def name(self) -> str:
        return "lcm"

    def _resolve_db_path(self) -> Path:
        env_db = os.getenv("HERMES_LCM_DB_PATH", "").strip()
        if env_db:
            db_path = Path(env_db).expanduser()
            db_path.parent.mkdir(parents=True, exist_ok=True)
            return db_path

        cfg = self._read_lcm_config()
        cfg_db = str(cfg.get("db_path", "") or "").strip()
        if cfg_db:
            db_path = Path(cfg_db).expanduser()
            db_path.parent.mkdir(parents=True, exist_ok=True)
            return db_path

        base = Path(get_hermes_home())
        db_dir = base / "context" / "lcm"
        db_dir.mkdir(parents=True, exist_ok=True)
        return db_dir / "lcm.db"

    def _read_lcm_config(self) -> Dict[str, Any]:
        """Best-effort read of context.lcm config map from config.yaml."""
        path = get_config_path()
        if not path.exists():
            return {}
        try:
            import yaml

            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            context_cfg = data.get("context", {}) if isinstance(data, dict) else {}
            lcm_cfg = context_cfg.get("lcm", {}) if isinstance(context_cfg, dict) else {}
            return lcm_cfg if isinstance(lcm_cfg, dict) else {}
        except Exception:
            return {}

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        # Production-friendly defaults: WAL for concurrent readers, bounded sync cost.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @staticmethod
    def _parse_int(value: Any, default: int, min_v: int, max_v: int, field: str) -> tuple[int | None, str | None]:
        """Return (value, error)."""
        if value is None:
            parsed = default
        else:
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                return None, f"{field} must be an integer"
        parsed = max(min_v, min(max_v, parsed))
        return parsed, None

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  session_id TEXT NOT NULL,
                  role TEXT NOT NULL,
                  content TEXT NOT NULL,
                  message_hash TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  UNIQUE(session_id, message_hash)
                )
                """
            )

            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()
            }
            if "message_hash" not in columns:
                conn.execute("ALTER TABLE messages ADD COLUMN message_hash TEXT")
                conn.execute(
                    "UPDATE messages SET message_hash = hex(randomblob(16)) WHERE message_hash IS NULL OR message_hash = ''"
                )

            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_session_hash_unique ON messages(session_id, message_hash)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id)"
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS summaries (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  session_id TEXT NOT NULL,
                  source_start_id INTEGER,
                  source_end_id INTEGER,
                  covered_message_count INTEGER NOT NULL DEFAULT 0,
                  parent_summary_id INTEGER,
                  depth INTEGER NOT NULL DEFAULT 0,
                  summary_text TEXT NOT NULL,
                  created_at TEXT NOT NULL
                )
                """
            )
            summary_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(summaries)").fetchall()
            }
            if "covered_message_count" not in summary_columns:
                conn.execute("ALTER TABLE summaries ADD COLUMN covered_message_count INTEGER NOT NULL DEFAULT 0")
            if "parent_summary_id" not in summary_columns:
                conn.execute("ALTER TABLE summaries ADD COLUMN parent_summary_id INTEGER")
            if "depth" not in summary_columns:
                conn.execute("ALTER TABLE summaries ADD COLUMN depth INTEGER NOT NULL DEFAULT 0")

            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_summaries_session ON summaries(session_id, id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_summaries_parent ON summaries(session_id, parent_summary_id)"
            )

            self._setup_fts(conn)

    def _setup_fts(self, conn: sqlite3.Connection) -> None:
        """Enable FTS5 table + triggers when SQLite supports it."""
        try:
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
                USING fts5(content, role, session_id UNINDEXED, message_id UNINDEXED)
                """
            )
            conn.execute(
                """
                INSERT INTO messages_fts(rowid, content, role, session_id, message_id)
                SELECT id, content, role, session_id, id
                FROM messages
                WHERE id NOT IN (SELECT rowid FROM messages_fts)
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
                  INSERT OR REPLACE INTO messages_fts(rowid, content, role, session_id, message_id)
                  VALUES (new.id, new.content, new.role, new.session_id, new.id);
                END
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
                  DELETE FROM messages_fts WHERE rowid = old.id;
                END
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE OF content, role, session_id ON messages BEGIN
                  INSERT OR REPLACE INTO messages_fts(rowid, content, role, session_id, message_id)
                  VALUES (new.id, new.content, new.role, new.session_id, new.id);
                END
                """
            )
            self._fts_enabled = True
        except sqlite3.Error:
            self._fts_enabled = False

    def on_session_start(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id or "unknown"

    def update_from_response(self, usage: Dict[str, Any]) -> None:
        self.last_prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        self.last_completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        total = usage.get("total_tokens", None)
        if total is None:
            total = self.last_prompt_tokens + self.last_completion_tokens
        self.last_total_tokens = int(total or 0)

    def should_compress(self, prompt_tokens: int = None) -> bool:
        tokens = prompt_tokens if prompt_tokens is not None else self.last_prompt_tokens
        return int(tokens or 0) >= self.threshold_tokens

    def ingest_messages(self, session_id: str, messages: List[Dict[str, Any]]) -> int:
        rows = []
        now = datetime.now(timezone.utc).isoformat()
        for m in messages:
            role = str(m.get("role", "unknown"))
            content = self._extract_content(m)
            if not content:
                continue
            message_hash = self._message_hash(role=role, content=content)
            rows.append((session_id, role, content, message_hash, now))
        if not rows:
            return 0
        with self._connect() as conn:
            before = conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()["c"]
            conn.executemany(
                "INSERT OR IGNORE INTO messages(session_id, role, content, message_hash, created_at) VALUES(?,?,?,?,?)",
                rows,
            )
            after = conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()["c"]
            return int(after) - int(before)

    def _extract_content(self, m: Dict[str, Any]) -> str:
        c = m.get("content", "")
        if isinstance(c, str):
            return c.strip()
        if isinstance(c, list):
            parts = []
            for item in c:
                if isinstance(item, dict):
                    text = item.get("text") or item.get("content")
                    if isinstance(text, str) and text.strip():
                        parts.append(text.strip())
                elif isinstance(item, str) and item.strip():
                    parts.append(item.strip())
            return "\n".join(parts)
        return ""

    def _message_hash(self, role: str, content: str) -> str:
        digest = hashlib.sha256()
        digest.update(role.encode("utf-8", errors="ignore"))
        digest.update(b"\0")
        digest.update(content.encode("utf-8", errors="ignore"))
        return digest.hexdigest()

    def _ingest_message_snapshot(self, messages: List[Dict[str, Any]]) -> int:
        return self.ingest_messages(self._session_id, messages)

    def _write_summary_node(self, session_id: str, source_messages: List[Dict[str, Any]]) -> int | None:
        if not source_messages:
            return None

        hashes = []
        condensed_lines: List[str] = []
        for m in source_messages:
            role = str(m.get("role", "msg"))
            content = self._extract_content(m)
            if not content:
                continue
            hashes.append(self._message_hash(role=role, content=content))
            if len(condensed_lines) < 24:
                snippet = content.replace("\n", " ").strip()[:180]
                condensed_lines.append(f"{role}: {snippet}")

        if not condensed_lines:
            return None

        summary_text = "\n".join(condensed_lines)
        now = datetime.now(timezone.utc).isoformat()

        with self._connect() as conn:
            parent = conn.execute(
                """
                SELECT id, depth
                FROM summaries
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            parent_id = int(parent["id"]) if parent else None
            depth = (int(parent["depth"]) + 1) if parent else 0

            source_start_id = None
            source_end_id = None
            if hashes:
                placeholders = ",".join(["?"] * len(hashes))
                rows = conn.execute(
                    f"""
                    SELECT id
                    FROM messages
                    WHERE session_id = ?
                      AND message_hash IN ({placeholders})
                    ORDER BY id ASC
                    """,
                    [session_id, *hashes],
                ).fetchall()
                if rows:
                    source_start_id = int(rows[0]["id"])
                    source_end_id = int(rows[-1]["id"])

            cur = conn.execute(
                """
                INSERT INTO summaries(
                  session_id, source_start_id, source_end_id,
                  covered_message_count, parent_summary_id, depth,
                  summary_text, created_at
                )
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    session_id,
                    source_start_id,
                    source_end_id,
                    len(hashes),
                    parent_id,
                    depth,
                    summary_text,
                    now,
                ),
            )
            return int(cur.lastrowid)

    def should_compress_preflight(self, messages: List[Dict[str, Any]]) -> bool:
        self._ingest_message_snapshot(messages)
        return False

    def compress(
        self,
        messages: List[Dict[str, Any]],
        current_tokens: int = None,
        focus_topic: str = None,
    ) -> List[Dict[str, Any]]:
        self.ingest_messages(self._session_id, messages)

        if len(messages) <= (self._head_keep + self._tail_keep + 6):
            return messages

        head = messages[: self._head_keep]
        tail = messages[-self._tail_keep :]
        middle = messages[self._head_keep : -self._tail_keep]
        summary_id = self._write_summary_node(self._session_id, middle)
        marker = {
            "role": "system",
            "content": (
                "[LCM] Earlier conversation was persisted losslessly to SQLite. "
                "Use lcm_grep/lcm_describe/lcm_expand to recall specifics."
                + (f" Summary node: {summary_id}." if summary_id is not None else "")
                + (f" Focus hint: {focus_topic}." if focus_topic else "")
            ),
        }

        self.compression_count += 1
        return head + [marker] + tail

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "lcm_grep",
                "description": "Search persisted conversation history for text",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Text to search"},
                        "limit": {"type": "integer", "description": "Max hits (default 8)"},
                        "session_id": {
                            "type": "string",
                            "description": "Optional session_id override (defaults to current session)",
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "lcm_describe",
                "description": "Fetch a persisted message by id (or latest match) from LCM storage",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer", "description": "Message id from lcm_grep hit"},
                        "query": {"type": "string", "description": "Optional fallback query when id is not provided"},
                        "session_id": {
                            "type": "string",
                            "description": "Optional session_id override (defaults to current session)",
                        },
                    },
                },
            },
            {
                "name": "lcm_expand",
                "description": "Return surrounding conversation window around a matched message",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer", "description": "Anchor message id"},
                        "query": {"type": "string", "description": "Optional query when id is omitted"},
                        "before": {"type": "integer", "description": "Messages before anchor (default 3)"},
                        "after": {"type": "integer", "description": "Messages after anchor (default 3)"},
                        "session_id": {"type": "string", "description": "Optional session id override"},
                    },
                },
            },
            {
                "name": "lcm_stats",
                "description": "Show persisted message/summary stats for current or requested session",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string", "description": "Optional session id override"}
                    },
                },
            },
            {
                "name": "lcm_health",
                "description": "Operational health snapshot for the LCM store (db, fts, pragmas, cardinality)",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string", "description": "Optional session id override"}
                    },
                },
            },
        ]

    def _grep_like(self, conn: sqlite3.Connection, session_id: str, query: str, limit: int) -> List[sqlite3.Row]:
        return conn.execute(
            """
            SELECT id, role, content, created_at
            FROM messages
            WHERE session_id = ?
              AND content LIKE ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (session_id, f"%{query}%", limit),
        ).fetchall()

    def _grep_fts(self, conn: sqlite3.Connection, session_id: str, query: str, limit: int) -> List[sqlite3.Row]:
        return conn.execute(
            """
            SELECT m.id, m.role, m.content, m.created_at
            FROM messages_fts f
            JOIN messages m ON m.id = f.rowid
            WHERE f.session_id = ?
              AND f.content MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (session_id, query, limit),
        ).fetchall()

    def handle_tool_call(self, name: str, args: Dict[str, Any], **kwargs) -> str:
        live_messages = kwargs.get("messages")
        if isinstance(live_messages, list):
            self._ingest_message_snapshot(live_messages)

        if name == "lcm_grep":
            query = str(args.get("query", "")).strip()
            if not query:
                return json.dumps({"error": "query is required"})

            parsed_limit, err = self._parse_int(args.get("limit"), default=8, min_v=1, max_v=50, field="limit")
            if err:
                return json.dumps({"error": err})
            limit = int(parsed_limit)
            session_id = str(args.get("session_id") or self._session_id)

            with self._connect() as conn:
                search_mode = "like"
                rows: List[sqlite3.Row]
                if self._fts_enabled:
                    try:
                        rows = self._grep_fts(conn, session_id, query, limit)
                        search_mode = "fts5"
                    except sqlite3.Error:
                        rows = self._grep_like(conn, session_id, query, limit)
                else:
                    rows = self._grep_like(conn, session_id, query, limit)

            hits = [
                {
                    "id": int(r["id"]),
                    "role": r["role"],
                    "created_at": r["created_at"],
                    "snippet": (r["content"][:280] + "…") if len(r["content"]) > 280 else r["content"],
                }
                for r in rows
            ]

            return json.dumps(
                {
                    "ok": True,
                    "session_id": session_id,
                    "query": query,
                    "count": len(hits),
                    "hits": hits,
                    "search_mode": search_mode,
                    "db_path": str(self._db_path),
                }
            )

        if name == "lcm_describe":
            session_id = str(args.get("session_id") or self._session_id)
            msg_id = args.get("id")
            query = str(args.get("query", "")).strip()

            with self._connect() as conn:
                if msg_id is not None:
                    try:
                        msg_id_int = int(msg_id)
                    except (TypeError, ValueError):
                        return json.dumps({"error": "id must be an integer"})
                    row = conn.execute(
                        "SELECT id, role, content, created_at FROM messages WHERE session_id = ? AND id = ?",
                        (session_id, msg_id_int),
                    ).fetchone()
                elif query:
                    row = conn.execute(
                        """
                        SELECT id, role, content, created_at
                        FROM messages
                        WHERE session_id = ? AND content LIKE ?
                        ORDER BY id DESC
                        LIMIT 1
                        """,
                        (session_id, f"%{query}%"),
                    ).fetchone()
                else:
                    return json.dumps({"error": "id or query is required"})

            if row is None:
                return json.dumps(
                    {
                        "ok": False,
                        "error": "not found",
                        "session_id": session_id,
                        "id": msg_id,
                        "query": query,
                    }
                )

            return json.dumps(
                {
                    "ok": True,
                    "session_id": session_id,
                    "message": {
                        "id": int(row["id"]),
                        "role": row["role"],
                        "created_at": row["created_at"],
                        "content": row["content"],
                    },
                    "db_path": str(self._db_path),
                }
            )

        if name == "lcm_expand":
            session_id = str(args.get("session_id") or self._session_id)
            msg_id = args.get("id")
            query = str(args.get("query", "")).strip()
            before_parsed, before_err = self._parse_int(args.get("before"), default=3, min_v=0, max_v=20, field="before")
            if before_err:
                return json.dumps({"error": before_err})
            after_parsed, after_err = self._parse_int(args.get("after"), default=3, min_v=0, max_v=20, field="after")
            if after_err:
                return json.dumps({"error": after_err})
            before = int(before_parsed)
            after = int(after_parsed)

            with self._connect() as conn:
                if msg_id is not None:
                    try:
                        msg_id_int = int(msg_id)
                    except (TypeError, ValueError):
                        return json.dumps({"error": "id must be an integer"})
                    anchor = conn.execute(
                        "SELECT id, role, content, created_at FROM messages WHERE session_id = ? AND id = ?",
                        (session_id, msg_id_int),
                    ).fetchone()
                elif query:
                    anchor = conn.execute(
                        """
                        SELECT id, role, content, created_at
                        FROM messages
                        WHERE session_id = ? AND content LIKE ?
                        ORDER BY id DESC
                        LIMIT 1
                        """,
                        (session_id, f"%{query}%"),
                    ).fetchone()
                else:
                    return json.dumps({"error": "id or query is required"})

                if anchor is None:
                    return json.dumps({"ok": False, "error": "not found", "session_id": session_id})

                rows = conn.execute(
                    """
                    SELECT id, role, content, created_at
                    FROM messages
                    WHERE session_id = ?
                      AND id BETWEEN ? AND ?
                    ORDER BY id ASC
                    """,
                    (session_id, int(anchor["id"]) - before, int(anchor["id"]) + after),
                ).fetchall()

            window = [
                {
                    "id": int(r["id"]),
                    "role": r["role"],
                    "created_at": r["created_at"],
                    "content": r["content"],
                    "is_anchor": int(r["id"]) == int(anchor["id"]),
                }
                for r in rows
            ]
            return json.dumps(
                {
                    "ok": True,
                    "session_id": session_id,
                    "anchor_id": int(anchor["id"]),
                    "before": before,
                    "after": after,
                    "count": len(window),
                    "window": window,
                    "db_path": str(self._db_path),
                }
            )

        if name == "lcm_stats":
            session_id = str(args.get("session_id") or self._session_id)
            with self._connect() as conn:
                msg_count = conn.execute(
                    "SELECT COUNT(*) AS c FROM messages WHERE session_id = ?",
                    (session_id,),
                ).fetchone()["c"]
                summary_count = conn.execute(
                    "SELECT COUNT(*) AS c FROM summaries WHERE session_id = ?",
                    (session_id,),
                ).fetchone()["c"]
                latest_summary = conn.execute(
                    """
                    SELECT id, created_at, depth, parent_summary_id, covered_message_count
                    FROM summaries
                    WHERE session_id = ?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (session_id,),
                ).fetchone()
                max_depth_row = conn.execute(
                    "SELECT COALESCE(MAX(depth), 0) AS d FROM summaries WHERE session_id = ?",
                    (session_id,),
                ).fetchone()

            return json.dumps(
                {
                    "ok": True,
                    "session_id": session_id,
                    "messages": int(msg_count or 0),
                    "summaries": int(summary_count or 0),
                    "max_summary_depth": int(max_depth_row["d"] or 0),
                    "latest_summary_id": int(latest_summary["id"]) if latest_summary else None,
                    "latest_summary_at": latest_summary["created_at"] if latest_summary else None,
                    "latest_summary_depth": int(latest_summary["depth"]) if latest_summary else None,
                    "latest_summary_parent_id": int(latest_summary["parent_summary_id"]) if latest_summary and latest_summary["parent_summary_id"] is not None else None,
                    "latest_summary_covered_messages": int(latest_summary["covered_message_count"]) if latest_summary else None,
                    "db_path": str(self._db_path),
                }
            )

        if name == "lcm_health":
            session_id = str(args.get("session_id") or self._session_id)
            with self._connect() as conn:
                msg_count = conn.execute(
                    "SELECT COUNT(*) AS c FROM messages WHERE session_id = ?",
                    (session_id,),
                ).fetchone()["c"]
                summary_count = conn.execute(
                    "SELECT COUNT(*) AS c FROM summaries WHERE session_id = ?",
                    (session_id,),
                ).fetchone()["c"]
                db_size = self._db_path.stat().st_size if self._db_path.exists() else 0
                wal_path = Path(str(self._db_path) + "-wal")
                wal_size = wal_path.stat().st_size if wal_path.exists() else 0
                journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
                synchronous = conn.execute("PRAGMA synchronous").fetchone()[0]
                fts_table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'messages_fts'"
                ).fetchone()

            return json.dumps(
                {
                    "ok": True,
                    "session_id": session_id,
                    "db_path": str(self._db_path),
                    "db_size_bytes": int(db_size),
                    "wal_size_bytes": int(wal_size),
                    "journal_mode": str(journal_mode),
                    "synchronous": int(synchronous),
                    "fts_enabled": bool(self._fts_enabled and fts_table is not None),
                    "messages": int(msg_count or 0),
                    "summaries": int(summary_count or 0),
                    "status": "healthy",
                }
            )

        return json.dumps({"error": f"Unknown tool: {name}"})

    def get_status(self) -> Dict[str, Any]:
        base = super().get_status()
        session_id = self._session_id
        with self._connect() as conn:
            msg_count = conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()["c"]
            summary_count = conn.execute(
                "SELECT COUNT(*) AS c FROM summaries WHERE session_id = ?",
                (session_id,),
            ).fetchone()["c"]
            max_depth = conn.execute(
                "SELECT COALESCE(MAX(depth), 0) AS d FROM summaries WHERE session_id = ?",
                (session_id,),
            ).fetchone()["d"]

        base.update(
            {
                "db_path": str(self._db_path),
                "engine": "lcm",
                "session_id": session_id,
                "messages": int(msg_count or 0),
                "summaries": int(summary_count or 0),
                "max_summary_depth": int(max_depth or 0),
                "head_keep": self._head_keep,
                "tail_keep": self._tail_keep,
                "threshold_percent": self.threshold_percent,
                "fts_enabled": self._fts_enabled,
            }
        )
        return base
