class MemoryStore:
    """
    Persistent per-user SQLite memory.

    Stores:
      - every user/assistant turn;
      - active conversation session;
      - rolling long-term summary.

    SQLite uses WAL and a process lock so normal Telegram concurrency is safe.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(
            str(self.path),
            timeout=30,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self):
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS user_state (
                    user_id INTEGER PRIMARY KEY,
                    active_session_id INTEGER NOT NULL DEFAULT 1,
                    long_term_summary TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    session_id INTEGER NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('user','assistant')),
                    content TEXT NOT NULL,
                    summarized INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_messages_user_session_id
                ON messages(user_id, session_id, id);

                CREATE INDEX IF NOT EXISTS idx_messages_user_summarized
                ON messages(user_id, summarized, id);
                """
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def ensure_user(self, user_id: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO user_state(
                    user_id, active_session_id, long_term_summary, updated_at
                ) VALUES (?, 1, '', ?)
                """,
                (user_id, self._now()),
            )

    def get_active_session(self, user_id: int) -> int:
        self.ensure_user(user_id)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT active_session_id FROM user_state WHERE user_id=?",
                (user_id,),
            ).fetchone()
            return int(row["active_session_id"])

    def get_long_term_summary(self, user_id: int) -> str:
        self.ensure_user(user_id)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT long_term_summary FROM user_state WHERE user_id=?",
                (user_id,),
            ).fetchone()
            return (row["long_term_summary"] or "").strip()

    def set_long_term_summary(self, user_id: int, summary: str) -> None:
        self.ensure_user(user_id)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE user_state
                SET long_term_summary=?, updated_at=?
                WHERE user_id=?
                """,
                (summary.strip(), self._now(), user_id),
            )

    def add_message(
        self,
        user_id: int,
        role: str,
        content: str,
        session_id: int | None = None,
    ) -> int:
        self.ensure_user(user_id)
        if session_id is None:
            session_id = self.get_active_session(user_id)

        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO messages(
                    user_id, session_id, role, content, summarized, created_at
                ) VALUES (?, ?, ?, ?, 0, ?)
                """,
                (user_id, session_id, role, content.strip(), self._now()),
            )
            return int(cur.lastrowid)

    def get_recent_messages(
        self,
        user_id: int,
        limit: int = MEMORY_RECENT_MESSAGES,
        session_id: int | None = None,
    ) -> list[dict[str, str]]:
        self.ensure_user(user_id)
        if session_id is None:
            session_id = self.get_active_session(user_id)

        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT role, content
                FROM messages
                WHERE user_id=? AND session_id=?
                ORDER BY id DESC
                LIMIT ?
                """,
                (user_id, session_id, int(limit)),
            ).fetchall()

        rows = list(reversed(rows))
        return [
            {"role": row["role"], "content": row["content"]}
            for row in rows
        ]

    def current_session_count(self, user_id: int) -> int:
        session_id = self.get_active_session(user_id)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM messages
                WHERE user_id=? AND session_id=?
                """,
                (user_id, session_id),
            ).fetchone()
            return int(row["c"])

    def total_message_count(self, user_id: int) -> int:
        self.ensure_user(user_id)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE user_id=?",
                (user_id,),
            ).fetchone()
            return int(row["c"])

    def get_unsummarized_older_messages(
        self,
        user_id: int,
        keep_latest: int = MEMORY_KEEP_UNSUMMARIZED,
    ) -> list[sqlite3.Row]:
        self.ensure_user(user_id)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, session_id, role, content, created_at
                FROM messages
                WHERE user_id=? AND summarized=0
                ORDER BY id ASC
                """,
                (user_id,),
            ).fetchall()

        if len(rows) <= keep_latest:
            return []

        return rows[:-keep_latest]

    def mark_summarized(self, message_ids: list[int]) -> None:
        if not message_ids:
            return

        placeholders = ",".join("?" for _ in message_ids)
        with self._lock, self._connect() as conn:
            conn.execute(
                f"UPDATE messages SET summarized=1 WHERE id IN ({placeholders})",
                tuple(int(i) for i in message_ids),
            )

    def new_session(self, user_id: int) -> int:
        self.ensure_user(user_id)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT active_session_id FROM user_state WHERE user_id=?",
                (user_id,),
            ).fetchone()
            new_id = int(row["active_session_id"]) + 1
            conn.execute(
                """
                UPDATE user_state
                SET active_session_id=?, updated_at=?
                WHERE user_id=?
                """,
                (new_id, self._now(), user_id),
            )
            return new_id

    def forget_user(self, user_id: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM messages WHERE user_id=?",
                (user_id,),
            )
            conn.execute(
                "DELETE FROM user_state WHERE user_id=?",
                (user_id,),
            )
        self.ensure_user(user_id)


memory_store = MemoryStore(MEMORY_DB_PATH)

# Per-user output mode:
#   pc_auto -> answer text + Telegram voice + immediate playback on THIS Windows PC
#   manual  -> answer text + "Проиграть ответ" button in Telegram
user_output_mode: dict[int, str] = {}

# Short-lived in-memory answer store for manual-play buttons.
pending_voice_answers: dict[str, tuple[int, str]] = {}
