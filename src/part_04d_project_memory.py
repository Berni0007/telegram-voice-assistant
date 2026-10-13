# Isolated long-term project contexts.
# A project has its own recent history and compact durable summary, so unrelated
# topics do not leak into each other (for example ZARUBA vs relocation vs audio).

PROJECT_RECENT_MESSAGES = max(6, int(os.getenv("PROJECT_RECENT_MESSAGES", "28")))
PROJECT_SUMMARY_TRIGGER = max(6, int(os.getenv("PROJECT_SUMMARY_TRIGGER", "12")))
PROJECT_SUMMARY_MAX_CHARS = max(1000, int(os.getenv("PROJECT_SUMMARY_MAX_CHARS", "5000")))


def _project_db() -> sqlite3.Connection:
    conn = sqlite3.connect(MEMORY_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            name_key TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            last_summarized_message_id INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, name_key)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS project_state (
            user_id INTEGER PRIMARY KEY,
            active_project_id INTEGER,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS project_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_project_messages_project_id ON project_messages(project_id, id)"
    )
    conn.commit()
    return conn


def _project_name_key(name: str) -> str:
    value = re.sub(r"\s+", " ", (name or "")).strip().casefold()
    return value[:120]


def project_get(project_id: int) -> dict | None:
    with _project_db() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    return dict(row) if row else None


def project_get_active(user_id: int) -> dict | None:
    with _project_db() as conn:
        row = conn.execute(
            """
            SELECT p.*
            FROM project_state s
            JOIN projects p ON p.id=s.active_project_id
            WHERE s.user_id=?
            """,
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


def project_list(user_id: int) -> list[dict]:
    active = project_get_active(user_id)
    active_id = int(active["id"]) if active else None
    with _project_db() as conn:
        rows = conn.execute(
            "SELECT * FROM projects WHERE user_id=? ORDER BY updated_at DESC, id DESC",
            (user_id,),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["active"] = int(item["id"]) == active_id
        result.append(item)
    return result


def project_get_or_create(user_id: int, name: str) -> dict:
    clean_name = re.sub(r"\s+", " ", (name or "")).strip()[:120]
    if not clean_name:
        raise ValueError("Project name is empty")
    key = _project_name_key(clean_name)
    now = datetime.now(timezone.utc).isoformat()
    with _project_db() as conn:
        row = conn.execute(
            "SELECT * FROM projects WHERE user_id=? AND name_key=?",
            (user_id, key),
        ).fetchone()
        if row:
            return dict(row)
        cur = conn.execute(
            """
            INSERT INTO projects(user_id,name,name_key,summary,last_summarized_message_id,created_at,updated_at)
            VALUES(?,?,?,?,0,?,?)
            """,
            (user_id, clean_name, key, "", now, now),
        )
        conn.commit()
        project_id = int(cur.lastrowid)
    return project_get(project_id) or {"id": project_id, "user_id": user_id, "name": clean_name, "summary": ""}


def project_set_active(user_id: int, project_id: int | None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _project_db() as conn:
        conn.execute(
            """
            INSERT INTO project_state(user_id,active_project_id,updated_at)
            VALUES(?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET active_project_id=excluded.active_project_id, updated_at=excluded.updated_at
            """,
            (user_id, project_id, now),
        )
        conn.commit()


def project_add_message(user_id: int, role: str, content: str) -> None:
    active = project_get_active(user_id)
    if not active:
        return
    clean = (content or "").strip()
    if not clean:
        return
    now = datetime.now(timezone.utc).isoformat()
    with _project_db() as conn:
        conn.execute(
            "INSERT INTO project_messages(project_id,user_id,role,content,created_at) VALUES(?,?,?,?,?)",
            (int(active["id"]), user_id, role, clean, now),
        )
        conn.execute("UPDATE projects SET updated_at=? WHERE id=?", (now, int(active["id"])))
        conn.commit()


def project_recent_messages(user_id: int, limit: int = PROJECT_RECENT_MESSAGES) -> list[dict]:
    active = project_get_active(user_id)
    if not active:
        return []
    with _project_db() as conn:
        rows = conn.execute(
            """
            SELECT role, content FROM project_messages
            WHERE project_id=? ORDER BY id DESC LIMIT ?
            """,
            (int(active["id"]), max(1, int(limit))),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def _project_unsummarized(project_id: int, after_id: int, limit: int = 80) -> list[dict]:
    with _project_db() as conn:
        rows = conn.execute(
            """
            SELECT id,role,content FROM project_messages
            WHERE project_id=? AND id>? ORDER BY id ASC LIMIT ?
            """,
            (project_id, after_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def project_refresh_summary(user_id: int, force: bool = False) -> str:
    active = project_get_active(user_id)
    if not active:
        return ""
    project_id = int(active["id"])
    old_summary = str(active.get("summary") or "")
    after_id = int(active.get("last_summarized_message_id") or 0)
    rows = _project_unsummarized(project_id, after_id)
    if not rows:
        return old_summary

    user_turns = sum(1 for r in rows if r["role"] == "user")
    if not force and user_turns < PROJECT_SUMMARY_TRIGGER:
        return old_summary

    transcript = "\n".join(
        ("Пользователь" if r["role"] == "user" else "Ассистент") + ": " + r["content"]
        for r in rows
    )
    prompt = (
        f"Проект: {active['name']}\n\n"
        "Обнови долговременную память ТОЛЬКО этого проекта. Сохраняй решения, факты, настройки, "
        "важные результаты, предпочтения внутри проекта, открытые задачи и что уже было сделано. "
        "Не добавляй сведения из других проектов и не придумывай факты. Пиши компактно и структурированно.\n\n"
        f"Предыдущее резюме проекта:\n{old_summary or '(пусто)'}\n\n"
        f"Новые сообщения:\n{transcript}"
    )
    response = llm_client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": "Ты модуль памяти проекта. Верни только обновлённое резюме без рассуждений."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.15,
        max_tokens=950,
        stream=False,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    summary = clean_llm_answer(response.choices[0].message.content or "").strip()
    if not summary:
        return old_summary
    summary = summary[:PROJECT_SUMMARY_MAX_CHARS]
    last_id = max(int(r["id"]) for r in rows)
    now = datetime.now(timezone.utc).isoformat()
    with _project_db() as conn:
        conn.execute(
            "UPDATE projects SET summary=?,last_summarized_message_id=?,updated_at=? WHERE id=?",
            (summary, last_id, now, project_id),
        )
        conn.commit()
    logger.info("Project summary updated: user=%s project=%s through=%s", user_id, active["name"], last_id)
    return summary


def project_switch(user_id: int, name: str | None) -> dict | None:
    current = project_get_active(user_id)
    if current:
        try:
            project_refresh_summary(user_id, force=True)
        except Exception:
            logger.exception("Project summary on switch failed")

    if name is None:
        project_set_active(user_id, None)
        return None

    project = project_get_or_create(user_id, name)
    project_set_active(user_id, int(project["id"]))
    return project_get(int(project["id"]))


def build_project_memory_context(user_id: int) -> str:
    active = project_get_active(user_id)
    if not active:
        return ""
    summary = str(active.get("summary") or "").strip()
    return (
        "АКТИВНЫЙ ПРОЕКТ:\n"
        f"Название: {active['name']}\n"
        "Это изолированный контекст. Не подтягивай факты, решения и открытые задачи из других проектов.\n\n"
        "Долговременная память проекта:\n"
        + (summary if summary else "(пока пусто — опирайся на историю этого проекта)")
    )


async def projects_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    rows = await asyncio.to_thread(project_list, update.effective_user.id)
    if not rows:
        await update.message.reply_text("Проектов пока нет. Создать/открыть: /project ZARUBA")
        return
    lines = ["Проекты:"]
    for item in rows[:30]:
        mark = "●" if item.get("active") else "○"
        lines.append(f"{mark} {item['name']}")
    lines.append("\nПереключить: /project ИМЯ\nОбщий режим: /project off")
    await update.message.reply_text("\n".join(lines))


async def project_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    user_id = update.effective_user.id
    name = " ".join(context.args).strip() if context.args else ""
    if not name:
        active = await asyncio.to_thread(project_get_active, user_id)
        if active:
            await update.message.reply_text(f"Активный проект: {active['name']}\nСменить: /project ИМЯ\nВыйти: /project off")
        else:
            await update.message.reply_text("Сейчас общий режим. Открыть проект: /project ZARUBA")
        return
    if name.casefold() in {"off", "none", "общий", "общая", "выкл", "выключить"}:
        await asyncio.to_thread(project_switch, user_id, None)
        await update.message.reply_text("Проектный режим выключен. Теперь используется общая память.")
        return
    project = await asyncio.to_thread(project_switch, user_id, name)
    await update.message.reply_text(f"Активный проект: {project['name']}\nТеперь его контекст хранится отдельно.")


async def project_info_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    active = await asyncio.to_thread(project_get_active, update.effective_user.id)
    if not active:
        await update.message.reply_text("Активного проекта нет.")
        return
    summary = str(active.get("summary") or "(резюме пока не сформировано)")
    if len(summary) > 3000:
        summary = summary[:3000] + "…"
    await update.message.reply_text(f"Проект: {active['name']}\n\nПамять проекта:\n{summary}")


async def maybe_handle_project_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    if not update.message or not update.effective_user:
        return False
    raw = re.sub(r"\s+", " ", (text or "")).strip()
    low = raw.casefold()

    if re.fullmatch(r"(?:выйди|выйти) из проекта|(?:выключи|отключи) проект(?:ный режим)?|общий режим", low):
        await asyncio.to_thread(project_switch, update.effective_user.id, None)
        await update.message.reply_text("Перешёл в общий режим памяти.")
        return True

    match = re.match(
        r"(?i)^(?:переключись на|перейди в|открой|создай)(?:\s+проект)?\s+(.+?)\s*$",
        raw,
    )
    if not match:
        match = re.match(r"(?i)^проект\s*[:=-]?\s*(.+?)\s*$", raw)
    if not match:
        return False

    name = match.group(1).strip(" .,-")
    if not name or len(name) > 120:
        return False
    project = await asyncio.to_thread(project_switch, update.effective_user.id, name)
    await update.message.reply_text(f"Активный проект: {project['name']}. Контекст теперь изолирован.")
    return True
