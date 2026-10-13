# Persistent reminders and scheduled Telegram tasks.
# Loaded after memory/continuity modules and before Telegram handlers.

from zoneinfo import ZoneInfo
from datetime import datetime, timedelta

REMINDER_TIMEZONE = os.getenv("REMINDER_TIMEZONE", os.getenv("USER_TIMEZONE", "Europe/Moscow")).strip() or "Europe/Moscow"
REMINDER_TZ = ZoneInfo(REMINDER_TIMEZONE)


def _reminder_db() -> sqlite3.Connection:
    conn = sqlite3.connect(MEMORY_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            run_at TEXT NOT NULL,
            repeat_kind TEXT NOT NULL DEFAULT 'once',
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            task_kind TEXT NOT NULL DEFAULT 'reminder'
        )
        """
    )
    # Migration for databases created before scheduled research tasks existed.
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(reminders)").fetchall()}
    if "task_kind" not in columns:
        conn.execute("ALTER TABLE reminders ADD COLUMN task_kind TEXT NOT NULL DEFAULT 'reminder'")
    conn.commit()
    return conn


def reminder_add(
    user_id: int,
    chat_id: int,
    text: str,
    run_at: datetime,
    repeat_kind: str = "once",
    task_kind: str = "reminder",
) -> int:
    run_at = run_at.astimezone(REMINDER_TZ)
    with _reminder_db() as conn:
        cur = conn.execute(
            "INSERT INTO reminders(user_id,chat_id,text,run_at,repeat_kind,enabled,created_at,task_kind) VALUES(?,?,?,?,?,1,?,?)",
            (
                user_id,
                chat_id,
                text.strip(),
                run_at.isoformat(),
                repeat_kind,
                datetime.now(REMINDER_TZ).isoformat(),
                task_kind,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def reminder_list(user_id: int) -> list[dict]:
    with _reminder_db() as conn:
        rows = conn.execute(
            "SELECT * FROM reminders WHERE user_id=? AND enabled=1 ORDER BY run_at",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def reminder_get(reminder_id: int) -> dict | None:
    with _reminder_db() as conn:
        row = conn.execute("SELECT * FROM reminders WHERE id=?", (reminder_id,)).fetchone()
    return dict(row) if row else None


def reminder_disable(reminder_id: int, user_id: int | None = None) -> bool:
    with _reminder_db() as conn:
        if user_id is None:
            cur = conn.execute("UPDATE reminders SET enabled=0 WHERE id=?", (reminder_id,))
        else:
            cur = conn.execute("UPDATE reminders SET enabled=0 WHERE id=? AND user_id=?", (reminder_id, user_id))
        conn.commit()
        return cur.rowcount > 0


def reminder_update_run_at(reminder_id: int, run_at: datetime) -> None:
    with _reminder_db() as conn:
        conn.execute("UPDATE reminders SET run_at=? WHERE id=?", (run_at.astimezone(REMINDER_TZ).isoformat(), reminder_id))
        conn.commit()


def _next_repeat_time(current: datetime, repeat_kind: str) -> datetime | None:
    if repeat_kind == "daily":
        return current + timedelta(days=1)
    if repeat_kind == "weekly":
        return current + timedelta(days=7)
    return None


def _looks_like_scheduled_research(text: str) -> bool:
    low = (text or "").lower()
    markers = (
        "найди ", "поищи ", "посмотри ", "проверь ", "собери ",
        "новости", "сводк", "актуальн", "последн", "свеж",
        "в интернете", "в сети", "цены", "вакансии", "погоду",
        "курс ", "расписание", "патч", "релиз", "обновлен", "обновлён",
    )
    return any(marker in low for marker in markers)


def _scheduled_task_prompt(task_text: str) -> str:
    text = re.sub(r"\s+", " ", (task_text or "")).strip()
    if not should_use_web_search(text):
        text = "Найди в интернете актуальную информацию: " + text
    return text


def generate_scheduled_research(task_text: str) -> str:
    """Run a fresh web-backed task without adding it to conversation memory."""
    prompt = _scheduled_task_prompt(task_text)
    web_context, sources = build_web_context(prompt)

    if not web_context:
        return (
            "Не удалось получить свежую информацию из интернета по запланированной задаче. "
            "Попробую снова при следующем запуске, если задача повторяется."
        )

    messages = [
        {
            "role": "system",
            "content": (
                LLM_SYSTEM_PROMPT
                + "\n\nТы выполняешь запланированную задачу пользователя. "
                "Сейчас обязательно опирайся на свежие данные из интернет-контекста ниже. "
                "Сделай готовую полезную выжимку для Telegram: кратко, конкретно, без внутренних рассуждений. "
                "Если данные противоречат друг другу или чего-то не удалось подтвердить — скажи это прямо.\n\n"
                + web_context
            ),
        },
        {"role": "user", "content": task_text},
    ]

    response = llm_client.chat.completions.create(
        model=LLM_MODEL,
        messages=messages,
        temperature=0.35,
        max_tokens=900,
        stream=False,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    answer = clean_llm_answer(response.choices[0].message.content or "").strip()
    if not answer:
        answer = "Не удалось сформировать результат по запланированной задаче."
    return append_web_sources(answer, sources)


async def _reminder_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    reminder_id = int(context.job.data["reminder_id"])
    item = reminder_get(reminder_id)
    if not item or not item.get("enabled"):
        return

    task_kind = item.get("task_kind", "reminder")
    try:
        if task_kind == "research":
            result = await asyncio.to_thread(generate_scheduled_research, item["text"])
            message = f"🔎 Запланированная сводка\n\n{result}"
        else:
            message = f"⏰ Напоминание\n{item['text']}"

        # Telegram text limit safety.
        if len(message) <= 4000:
            await context.bot.send_message(chat_id=int(item["chat_id"]), text=message)
        else:
            chunks = [message[i:i + 3900] for i in range(0, len(message), 3900)]
            for chunk in chunks:
                await context.bot.send_message(chat_id=int(item["chat_id"]), text=chunk)
    except Exception as exc:
        logger.exception("Failed to run scheduled task id=%s", reminder_id)
        try:
            await context.bot.send_message(
                chat_id=int(item["chat_id"]),
                text=f"⚠️ Не удалось выполнить запланированную задачу #{reminder_id}: {type(exc).__name__}",
            )
        except Exception:
            logger.exception("Failed to report scheduled task error id=%s", reminder_id)

    repeat_kind = item.get("repeat_kind", "once")
    current = datetime.fromisoformat(item["run_at"]).astimezone(REMINDER_TZ)
    next_run = _next_repeat_time(current, repeat_kind)
    if next_run is None:
        reminder_disable(reminder_id)
        return

    now = datetime.now(REMINDER_TZ)
    while next_run <= now:
        next_run = _next_repeat_time(next_run, repeat_kind)
        if next_run is None:
            break
    if next_run is None:
        reminder_disable(reminder_id)
        return

    reminder_update_run_at(reminder_id, next_run)
    context.job_queue.run_once(
        _reminder_job,
        when=next_run,
        data={"reminder_id": reminder_id},
        name=f"reminder:{reminder_id}",
    )


def schedule_reminder_job(job_queue, reminder_id: int, run_at: datetime) -> None:
    if job_queue is None:
        logger.warning("JobQueue is unavailable; reminder %s stays in DB only", reminder_id)
        return
    for job in job_queue.get_jobs_by_name(f"reminder:{reminder_id}"):
        job.schedule_removal()
    job_queue.run_once(
        _reminder_job,
        when=run_at,
        data={"reminder_id": reminder_id},
        name=f"reminder:{reminder_id}",
    )


async def restore_reminders(application) -> None:
    job_queue = application.job_queue
    now = datetime.now(REMINDER_TZ)
    with _reminder_db() as conn:
        rows = conn.execute("SELECT * FROM reminders WHERE enabled=1 ORDER BY run_at").fetchall()

    restored = 0
    for row in rows:
        item = dict(row)
        run_at = datetime.fromisoformat(item["run_at"]).astimezone(REMINDER_TZ)
        repeat_kind = item.get("repeat_kind", "once")

        if run_at <= now:
            if repeat_kind == "once":
                run_at = now + timedelta(seconds=5)
            else:
                while run_at <= now:
                    nxt = _next_repeat_time(run_at, repeat_kind)
                    if nxt is None:
                        break
                    run_at = nxt
                reminder_update_run_at(int(item["id"]), run_at)

        schedule_reminder_job(job_queue, int(item["id"]), run_at)
        restored += 1

    logger.info("Restored reminders/tasks: %d", restored)


def _parse_clock(hour: str, minute: str | None) -> tuple[int, int] | None:
    try:
        h = int(hour)
        m = int(minute or 0)
    except Exception:
        return None
    if 0 <= h <= 23 and 0 <= m <= 59:
        return h, m
    return None


def parse_reminder_request(text: str, now: datetime | None = None) -> dict | None:
    """Parse common Russian reminder/task phrases without another LLM call."""
    raw = re.sub(r"\s+", " ", (text or "")).strip()
    low = raw.lower()
    scheduling_markers = (
        "напомни", "напоминание", "пришли мне", "напиши мне",
        "каждый день", "каждую неделю", "ежедневно", "еженедельно",
    )
    if not any(x in low for x in scheduling_markers):
        return None

    now = (now or datetime.now(REMINDER_TZ)).astimezone(REMINDER_TZ)
    repeat_kind = "once"
    if re.search(r"\bкажд(ый|ую|ое) день\b|\bежедневно\b", low):
        repeat_kind = "daily"
    elif re.search(r"\bкажд(ую|ый) недел", low) or "еженедельно" in low:
        repeat_kind = "weekly"

    run_at: datetime | None = None

    rel = re.search(r"\bчерез\s+(\d+)\s*(минут\w*|час\w*|дн\w*)", low)
    if rel:
        amount = int(rel.group(1))
        unit = rel.group(2)
        if unit.startswith("минут"):
            run_at = now + timedelta(minutes=amount)
        elif unit.startswith("час"):
            run_at = now + timedelta(hours=amount)
        else:
            run_at = now + timedelta(days=amount)
    else:
        time_match = re.search(r"(?:\bв\s+)?([01]?\d|2[0-3])[:.]([0-5]\d)\b", low)
        if not time_match:
            time_match = re.search(r"\bв\s+([01]?\d|2[0-3])\s*(?:час(?:а|ов)?)?\b", low)

        hm = None
        if time_match:
            hm = _parse_clock(time_match.group(1), time_match.group(2) if time_match.lastindex and time_match.lastindex >= 2 else None)

        if hm:
            h, m = hm
            base = now
            if "послезавтра" in low:
                base = now + timedelta(days=2)
            elif "завтра" in low:
                base = now + timedelta(days=1)
            run_at = base.replace(hour=h, minute=m, second=0, microsecond=0)
            if repeat_kind == "once" and "завтра" not in low and "послезавтра" not in low and run_at <= now:
                run_at += timedelta(days=1)
            if repeat_kind in {"daily", "weekly"} and run_at <= now:
                run_at += timedelta(days=1 if repeat_kind == "daily" else 7)

    if run_at is None:
        return None

    message = raw
    message = re.sub(r"(?i)^\s*(напомни(?:\s+мне)?|напоминание|пришли\s+мне|напиши\s+мне)\s*", "", message).strip(" ,.-")
    message = re.sub(r"(?i)\bчерез\s+\d+\s*(?:минут\w*|час\w*|дн\w*)\b", "", message)
    message = re.sub(r"(?i)\b(?:сегодня|завтра|послезавтра)\b", "", message)
    message = re.sub(r"(?i)\b(?:каждый день|каждую неделю|ежедневно|еженедельно)\b", "", message)
    message = re.sub(r"(?i)\bв\s+([01]?\d|2[0-3])(?:[:.]([0-5]\d))?(?:\s*час(?:а|ов)?)?\b", "", message)
    message = re.sub(r"\s+", " ", message).strip(" ,.-")
    if not message:
        message = "Напоминание"

    task_kind = "research" if _looks_like_scheduled_research(message) else "reminder"
    return {"run_at": run_at, "repeat_kind": repeat_kind, "text": message, "task_kind": task_kind}


def reminder_human_time(dt: datetime) -> str:
    return dt.astimezone(REMINDER_TZ).strftime("%d.%m.%Y %H:%M")


async def reminders_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    rows = reminder_list(update.effective_user.id)
    if not rows:
        await update.message.reply_text("Активных напоминаний и задач нет.")
        return
    lines = ["⏰ Активные напоминания и задачи:"]
    repeat_names = {"once": "один раз", "daily": "каждый день", "weekly": "каждую неделю"}
    for item in rows[:30]:
        dt = datetime.fromisoformat(item["run_at"])
        kind = "🔎 поиск" if item.get("task_kind") == "research" else "⏰ напоминание"
        lines.append(f"#{item['id']} — {kind} — {reminder_human_time(dt)} ({repeat_names.get(item['repeat_kind'], item['repeat_kind'])}) — {item['text']}")
    lines.append("\nУдалить: /delreminder ID")
    await update.message.reply_text("\n".join(lines))


async def delete_reminder_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Использование: /delreminder ID")
        return
    reminder_id = int(context.args[0])
    if not reminder_disable(reminder_id, update.effective_user.id):
        await update.message.reply_text("Такого активного напоминания/задания нет.")
        return
    if context.job_queue:
        for job in context.job_queue.get_jobs_by_name(f"reminder:{reminder_id}"):
            job.schedule_removal()
    await update.message.reply_text(f"Задача #{reminder_id} удалена.")


async def maybe_handle_reminder_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    if not update.effective_user or not update.effective_chat or not update.message:
        return False
    parsed = parse_reminder_request(text)
    if not parsed:
        return False

    reminder_id = reminder_add(
        update.effective_user.id,
        update.effective_chat.id,
        parsed["text"],
        parsed["run_at"],
        parsed["repeat_kind"],
        parsed["task_kind"],
    )
    schedule_reminder_job(context.job_queue, reminder_id, parsed["run_at"])
    repeat_names = {"once": "", "daily": " · каждый день", "weekly": " · каждую неделю"}
    kind_line = "🔎 Выполню интернет-поиск и пришлю готовую сводку." if parsed["task_kind"] == "research" else "⏰ Пришлю это сообщение как напоминание."
    await update.message.reply_text(
        f"Готово. #{reminder_id}\n{reminder_human_time(parsed['run_at'])}{repeat_names.get(parsed['repeat_kind'], '')}\n{kind_line}\n{parsed['text']}"
    )
    return True
