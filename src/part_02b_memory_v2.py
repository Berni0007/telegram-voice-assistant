import json
import math

MEMORY_STRUCTURED_ENABLED = (
    os.getenv("MEMORY_STRUCTURED_ENABLED", "1").strip().lower()
    not in {"0", "false", "no", "off"}
)
MEMORY_EMBEDDING_MODEL = os.getenv("MEMORY_EMBEDDING_MODEL", "baai/bge-m3").strip() or "baai/bge-m3"
MEMORY_SEMANTIC_TOP_K = int(os.getenv("MEMORY_SEMANTIC_TOP_K", "6"))
MEMORY_MIN_SIMILARITY = float(os.getenv("MEMORY_MIN_SIMILARITY", "0.34"))
MEMORY_EXTRACT_INTERVAL = int(os.getenv("MEMORY_EXTRACT_INTERVAL", "2"))
MEMORY_MAX_ITEMS = int(os.getenv("MEMORY_MAX_ITEMS", "120"))
MEMORY_KINDS = {"preference", "style", "fact", "project", "decision", "open_topic"}


def _memory_v2_init() -> None:
    with memory_store._lock, memory_store._connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS memory_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                content TEXT NOT NULL,
                embedding TEXT NOT NULL DEFAULT '',
                importance INTEGER NOT NULL DEFAULT 3,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_memory_items_user_status
            ON memory_items(user_id, status, kind, id);
            """
        )
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(user_state)").fetchall()}
        if "last_memory_extract_message_id" not in columns:
            conn.execute(
                "ALTER TABLE user_state ADD COLUMN last_memory_extract_message_id INTEGER NOT NULL DEFAULT 0"
            )


_memory_v2_init()


def memory_last_message_id(user_id: int) -> int:
    memory_store.ensure_user(user_id)
    with memory_store._lock, memory_store._connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS max_id FROM messages WHERE user_id=?",
            (user_id,),
        ).fetchone()
    return int(row["max_id"] or 0)


def memory_messages_after(user_id: int, after_id: int, limit: int = 20) -> list[dict]:
    memory_store.ensure_user(user_id)
    with memory_store._lock, memory_store._connect() as conn:
        rows = conn.execute(
            """
            SELECT id, session_id, role, content, created_at
            FROM messages
            WHERE user_id=? AND id>?
            ORDER BY id ASC
            LIMIT ?
            """,
            (user_id, int(after_id), int(limit)),
        ).fetchall()
    return [dict(row) for row in rows]


def memory_get_last_extract_message_id(user_id: int) -> int:
    memory_store.ensure_user(user_id)
    with memory_store._lock, memory_store._connect() as conn:
        row = conn.execute(
            "SELECT last_memory_extract_message_id FROM user_state WHERE user_id=?",
            (user_id,),
        ).fetchone()
    return int(row["last_memory_extract_message_id"] or 0)


def memory_set_last_extract_message_id(user_id: int, message_id: int) -> None:
    memory_store.ensure_user(user_id)
    with memory_store._lock, memory_store._connect() as conn:
        conn.execute(
            """
            UPDATE user_state
            SET last_memory_extract_message_id=?, updated_at=?
            WHERE user_id=?
            """,
            (int(message_id), memory_store._now(), user_id),
        )


def memory_list_items(
    user_id: int,
    status: str | None = "active",
    kinds: tuple[str, ...] | list[str] | None = None,
    limit: int = 200,
) -> list[dict]:
    memory_store.ensure_user(user_id)
    clauses = ["user_id=?"]
    params: list = [user_id]
    if status is not None:
        clauses.append("status=?")
        params.append(status)
    if kinds:
        placeholders = ",".join("?" for _ in kinds)
        clauses.append(f"kind IN ({placeholders})")
        params.extend(kinds)
    params.append(int(limit))
    sql = f"""
        SELECT id, kind, content, embedding, importance, status, created_at, updated_at
        FROM memory_items
        WHERE {' AND '.join(clauses)}
        ORDER BY importance DESC, updated_at DESC, id DESC
        LIMIT ?
    """
    with memory_store._lock, memory_store._connect() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [dict(row) for row in rows]


def memory_count_items(user_id: int, status: str = "active") -> int:
    with memory_store._lock, memory_store._connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM memory_items WHERE user_id=? AND status=?",
            (user_id, status),
        ).fetchone()
    return int(row["c"])


def _normalize_memory_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def memory_upsert_item(
    user_id: int,
    kind: str,
    content: str,
    importance: int = 3,
    embedding: list[float] | None = None,
) -> int:
    kind = (kind or "").strip()
    content = re.sub(r"\s+", " ", (content or "")).strip()
    if kind not in MEMORY_KINDS or not content:
        raise ValueError("Invalid memory item")
    importance = max(1, min(5, int(importance)))
    emb = json.dumps(embedding or [], separators=(",", ":"))
    now = memory_store._now()
    normalized = _normalize_memory_text(content)

    with memory_store._lock, memory_store._connect() as conn:
        rows = conn.execute(
            "SELECT id, content FROM memory_items WHERE user_id=? AND kind=? AND status='active'",
            (user_id, kind),
        ).fetchall()
        for row in rows:
            if _normalize_memory_text(row["content"]) == normalized:
                conn.execute(
                    """
                    UPDATE memory_items
                    SET importance=?, embedding=CASE WHEN ?!='[]' THEN ? ELSE embedding END, updated_at=?
                    WHERE id=?
                    """,
                    (importance, emb, emb, now, int(row["id"])),
                )
                return int(row["id"])

        cur = conn.execute(
            """
            INSERT INTO memory_items(user_id, kind, content, embedding, importance, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
            """,
            (user_id, kind, content, emb, importance, now, now),
        )
        item_id = int(cur.lastrowid)

        count = int(conn.execute(
            "SELECT COUNT(*) AS c FROM memory_items WHERE user_id=? AND status='active'",
            (user_id,),
        ).fetchone()["c"])
        overflow = count - MEMORY_MAX_ITEMS
        if overflow > 0:
            old_rows = conn.execute(
                """
                SELECT id FROM memory_items
                WHERE user_id=? AND status='active' AND kind NOT IN ('style','open_topic')
                ORDER BY importance ASC, updated_at ASC, id ASC
                LIMIT ?
                """,
                (user_id, overflow),
            ).fetchall()
            for old in old_rows:
                conn.execute(
                    "UPDATE memory_items SET status='archived', updated_at=? WHERE id=?",
                    (now, int(old["id"])),
                )
    return item_id


def memory_update_item(
    item_id: int,
    content: str | None = None,
    importance: int | None = None,
    embedding: list[float] | None = None,
    status: str | None = None,
) -> None:
    fields, params = [], []
    if content is not None:
        fields.append("content=?")
        params.append(re.sub(r"\s+", " ", content).strip())
    if importance is not None:
        fields.append("importance=?")
        params.append(max(1, min(5, int(importance))))
    if embedding is not None:
        fields.append("embedding=?")
        params.append(json.dumps(embedding, separators=(",", ":")))
    if status is not None:
        fields.append("status=?")
        params.append(status)
    if not fields:
        return
    fields.append("updated_at=?")
    params.extend([memory_store._now(), int(item_id)])
    with memory_store._lock, memory_store._connect() as conn:
        conn.execute(f"UPDATE memory_items SET {', '.join(fields)} WHERE id=?", tuple(params))


def memory_get_open_topics(user_id: int, limit: int = 8) -> list[dict]:
    return memory_list_items(user_id, "active", ("open_topic",), limit)


def memory_get_style_items(user_id: int, limit: int = 8) -> list[dict]:
    return memory_list_items(user_id, "active", ("style", "preference"), limit)


# v5.1 edge case fix: rows[:-0] returned an empty list during /new.
def _fixed_get_unsummarized_older_messages(
    user_id: int,
    keep_latest: int = MEMORY_KEEP_UNSUMMARIZED,
) -> list[sqlite3.Row]:
    memory_store.ensure_user(user_id)
    with memory_store._lock, memory_store._connect() as conn:
        rows = conn.execute(
            """
            SELECT id, session_id, role, content, created_at
            FROM messages
            WHERE user_id=? AND summarized=0
            ORDER BY id ASC
            """,
            (user_id,),
        ).fetchall()
    if keep_latest <= 0:
        return list(rows)
    if len(rows) <= keep_latest:
        return []
    return list(rows[:-keep_latest])


memory_store.get_unsummarized_older_messages = _fixed_get_unsummarized_older_messages

_original_forget_user_v51 = memory_store.forget_user


def _forget_user_v52(user_id: int) -> None:
    with memory_store._lock, memory_store._connect() as conn:
        conn.execute("DELETE FROM memory_items WHERE user_id=?", (user_id,))
    _original_forget_user_v51(user_id)


memory_store.forget_user = _forget_user_v52
