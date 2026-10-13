# Lightweight conditional monitoring + anti-duplicate delivery.
# Heavy work stays remote: the host stores only small SQLite state and search fingerprints.

import hashlib

WATCH_ENABLED = os.getenv("WATCH_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
WATCH_INTERVAL_MINUTES = max(60, int(os.getenv("WATCH_INTERVAL_MINUTES", "180")))
WATCH_MAX_SEEN = max(50, int(os.getenv("WATCH_MAX_SEEN", "300")))


def _watch_db() -> sqlite3.Connection:
    conn = sqlite3.connect(MEMORY_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS web_watches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            query TEXT NOT NULL,
            interval_minutes INTEGER NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            last_checked_at TEXT,
            last_alert_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS watch_seen (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            watch_id INTEGER NOT NULL,
            fingerprint TEXT NOT NULL,
            url TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            first_seen_at TEXT NOT NULL,
            UNIQUE(watch_id, fingerprint)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scheduled_delivery_state (
            reminder_id INTEGER PRIMARY KEY,
            last_digest TEXT NOT NULL DEFAULT '',
            last_sent_at TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_watch_seen_watch ON watch_seen(watch_id, id)")
    conn.commit()
    return conn


def _watch_now() -> str:
    return datetime.now(REMINDER_TZ).isoformat()


def watch_add(user_id: int, chat_id: int, query: str, interval_minutes: int) -> int:
    clean = re.sub(r"\s+", " ", (query or "")).strip()
    if not clean:
        raise ValueError("Watch query is empty")
    interval_minutes = max(60, int(interval_minutes or WATCH_INTERVAL_MINUTES))
    with _watch_db() as conn:
        cur = conn.execute(
            "INSERT INTO web_watches(user_id,chat_id,query,interval_minutes,enabled,created_at) VALUES(?,?,?,?,1,?)",
            (user_id, chat_id, clean[:1800], interval_minutes, _watch_now()),
        )
        conn.commit()
        return int(cur.lastrowid)


def watch_get(watch_id: int) -> dict | None:
    with _watch_db() as conn:
        row = conn.execute("SELECT * FROM web_watches WHERE id=?", (watch_id,)).fetchone()
    return dict(row) if row else None


def watch_list(user_id: int) -> list[dict]:
    with _watch_db() as conn:
        rows = conn.execute(
            "SELECT * FROM web_watches WHERE user_id=? AND enabled=1 ORDER BY id",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def watch_disable(watch_id: int, user_id: int | None = None) -> bool:
    with _watch_db() as conn:
        if user_id is None:
            cur = conn.execute("UPDATE web_watches SET enabled=0 WHERE id=?", (watch_id,))
        else:
            cur = conn.execute("UPDATE web_watches SET enabled=0 WHERE id=? AND user_id=?", (watch_id, user_id))
        conn.commit()
        return cur.rowcount > 0


def _source_fingerprint(source: dict) -> str:
    normalized = "|".join(
        re.sub(r"\s+", " ", str(source.get(key) or "")).strip().casefold()
        for key in ("url", "title", "snippet")
    )
    return hashlib.sha256(normalized.encode("utf-8", errors="ignore")).hexdigest()


def _watch_seen_fingerprints(watch_id: int) -> set[str]:
    with _watch_db() as conn:
        rows = conn.execute("SELECT fingerprint FROM watch_seen WHERE watch_id=?", (watch_id,)).fetchall()
    return {str(row["fingerprint"]) for row in rows}


def _watch_mark_seen(watch_id: int, sources: list[dict]) -> None:
    now = _watch_now()
    with _watch_db() as conn:
        for source in sources:
            fp = _source_fingerprint(source)
            conn.execute(
                "INSERT OR IGNORE INTO watch_seen(watch_id,fingerprint,url,title,first_seen_at) VALUES(?,?,?,?,?)",
                (
                    watch_id,
                    fp,
                    str(source.get("url") or "")[:1200],
                    str(source.get("title") or "")[:500],
                    now,
                ),
            )
        # Keep storage bounded on small hosts.
        conn.execute(
            """
            DELETE FROM watch_seen
            WHERE watch_id=? AND id NOT IN (
                SELECT id FROM watch_seen WHERE watch_id=? ORDER BY id DESC LIMIT ?
            )
            """,
            (watch_id, watch_id, WATCH_MAX_SEEN),
        )
        conn.commit()


def _watch_set_checked(watch_id: int, alerted: bool = False) -> None:
    now = _watch_now()
    with _watch_db() as conn:
        if alerted:
            conn.execute(
                "UPDATE web_watches SET last_checked_at=?,last_alert_at=? WHERE id=?",
                (now, now, watch_id),
            )
        else:
            conn.execute("UPDATE web_watches SET last_checked_at=? WHERE id=?", (now, watch_id))
        conn.commit()


def _watch_search(query: str) -> list[dict]:
    if not WEB_SEARCH_ENABLED:
        return []
    search_query = re.sub(r"\s+", " ", query or "").strip()
    if not search_query:
        return []
    try:
        return _duckduckgo_search(search_query)
    except Exception:
        logger.exception("Watch search failed: query=%r", search_query)
        return []


def _watch_build_alert(query: str, new_sources: list[dict]) -> str | None:
    if not new_sources:
        return None

    evidence = []
    for idx, source in enumerate(new_sources[:8], start=1):
        evidence.append(
            f"[{idx}] {source.get('title') or source.get('url') or 'Источник'}\n"
            f"URL: {source.get('url') or ''}\n"
            f"Фрагмент: {source.get('snippet') or '(нет фрагмента)'}"
        )

    prompt = (
        "Ты проверяешь постоянный мониторинг пользователя. Нужно решить, появилось ли НОВОЕ и действительно "
        "значимое событие, которое соответствует условию. Если результаты выглядят старыми, случайными, "
        "не подтверждают условие или недостаточно ясны — верни ровно NO_CHANGE. "
        "Если условие выполнено, дай очень короткое уведомление: что изменилось и почему это важно. "
        "Не придумывай факты и опирайся только на предоставленные результаты.\n\n"
        f"Условие мониторинга: {query}\n\n"
        "Новые поисковые результаты:\n" + "\n\n".join(evidence)
    )
    try:
        response = llm_client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": "Ты строгий фильтр уведомлений. Либо NO_CHANGE, либо короткое подтверждённое уведомление."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=350,
            stream=False,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        answer = clean_llm_answer(response.choices[0].message.content or "").strip()
    except Exception:
        logger.exception("Watch relevance filtering failed")
        return None

    if not answer or answer.upper().replace(" ", "_") == "NO_CHANGE":
        return None

    links = []
    for source in new_sources[:5]:
        url = str(source.get("url") or "").strip()
        if url and url not in links:
            links.append(url)
    if links:
        answer += "\n\nИсточники:\n" + "\n".join(links)
    return answer


async def _watch_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    watch_id = int(context.job.data["watch_id"])
    item = watch_get(watch_id)
    if not item or not item.get("enabled"):
        return

    sources = await asyncio.to_thread(_watch_search, item["query"])
    if not sources:
        _watch_set_checked(watch_id, False)
        return

    seen = _watch_seen_fingerprints(watch_id)
    if not seen:
        # Initial baseline is silent: existing search results are not "new".
        _watch_mark_seen(watch_id, sources)
        _watch_set_checked(watch_id, False)
        logger.info("Watch baseline created: id=%s sources=%d", watch_id, len(sources))
        return

    new_sources = [source for source in sources if _source_fingerprint(source) not in seen]
    if not new_sources:
        _watch_set_checked(watch_id, False)
        return

    # Mark candidates even if rejected as noise, otherwise the same stale result
    # would trigger an LLM filtering call every cycle.
    _watch_mark_seen(watch_id, new_sources)
    alert = await asyncio.to_thread(_watch_build_alert, item["query"], new_sources)
    if not alert:
        _watch_set_checked(watch_id, False)
        return

    try:
        message = f"👁 Изменение по наблюдению #{watch_id}\n\n{alert}"
        for chunk in [message[i:i + 3900] for i in range(0, len(message), 3900)]:
            await context.bot.send_message(chat_id=int(item["chat_id"]), text=chunk)
        _watch_set_checked(watch_id, True)
    except Exception:
        logger.exception("Watch notification failed: id=%s", watch_id)


def schedule_watch_job(job_queue, watch_id: int, interval_minutes: int, first_seconds: int = 5) -> None:
    if job_queue is None:
        logger.warning("JobQueue unavailable; watch %s stays in DB only", watch_id)
        return
    for job in job_queue.get_jobs_by_name(f"watch:{watch_id}"):
        job.schedule_removal()
    job_queue.run_repeating(
        _watch_job,
        interval=timedelta(minutes=max(60, int(interval_minutes))),
        first=timedelta(seconds=max(1, int(first_seconds))),
        data={"watch_id": watch_id},
        name=f"watch:{watch_id}",
    )


async def restore_watches(application) -> None:
    if not WATCH_ENABLED:
        return
    with _watch_db() as conn:
        rows = conn.execute("SELECT * FROM web_watches WHERE enabled=1 ORDER BY id").fetchall()
    for row in rows:
        item = dict(row)
        schedule_watch_job(application.job_queue, int(item["id"]), int(item["interval_minutes"]), first_seconds=10)
    logger.info("Restored watches: %d", len(rows))


async def restore_runtime_jobs(application) -> None:
    await restore_reminders(application)
    await restore_watches(application)


def _parse_watch_interval(text: str) -> tuple[int, str]:
    raw = text
    low = raw.lower()
    interval = WATCH_INTERVAL_MINUTES

    m = re.search(r"\b(?:каждые|раз в)\s+(\d+)\s*час", low)
    if m:
        interval = max(60, int(m.group(1)) * 60)
        raw = re.sub(r"(?i)\b(?:каждые|раз в)\s+\d+\s*час\w*", "", raw)
    else:
        m = re.search(r"\b(?:каждые|раз в)\s+(\d+)\s*минут", low)
        if m:
            interval = max(60, int(m.group(1)))
            raw = re.sub(r"(?i)\b(?:каждые|раз в)\s+\d+\s*минут\w*", "", raw)
        elif re.search(r"\b(?:каждый час|раз в час)\b", low):
            interval = 60
            raw = re.sub(r"(?i)\b(?:каждый час|раз в час)\b", "", raw)
        elif re.search(r"\b(?:каждый день|раз в день)\b", low):
            interval = 1440
            raw = re.sub(r"(?i)\b(?:каждый день|раз в день)\b", "", raw)

    return interval, re.sub(r"\s+", " ", raw).strip(" ,.-")


def parse_watch_request(text: str) -> dict | None:
    raw = re.sub(r"\s+", " ", (text or "")).strip()
    if not raw or not WATCH_ENABLED:
        return None

    interval, cleaned = _parse_watch_interval(raw)
    patterns = (
        r"(?i)^(?:следи|отслеживай|мониторь)\s+(?:за\s+)?(.+)$",
        r"(?i)^(?:сообщи|уведоми|дай\s+знать)(?:\s+мне)?\s+(?:если|когда)\s+(.+)$",
        r"(?i)^проверяй\s+(.+?)\s+и\s+(?:сообщи|уведоми)(?:\s+мне)?\s+(?:если|когда)\s+(.+)$",
    )
    for pattern in patterns:
        match = re.match(pattern, cleaned)
        if not match:
            continue
        if match.lastindex and match.lastindex >= 2:
            query = f"{match.group(1)}; сообщить если {match.group(2)}"
        else:
            query = match.group(1)
        query = re.sub(r"\s+", " ", query).strip(" ,.-")
        if len(query) >= 4:
            return {"query": query[:1800], "interval_minutes": interval}
    return None


async def maybe_handle_watch_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    if not update.message or not update.effective_user or not update.effective_chat:
        return False
    parsed = parse_watch_request(text)
    if not parsed:
        return False
    watch_id = watch_add(
        update.effective_user.id,
        update.effective_chat.id,
        parsed["query"],
        parsed["interval_minutes"],
    )
    schedule_watch_job(context.job_queue, watch_id, parsed["interval_minutes"], first_seconds=5)
    hours = parsed["interval_minutes"] / 60
    interval_text = f"{int(hours)} ч" if hours.is_integer() else f"{parsed['interval_minutes']} мин"
    await update.message.reply_text(
        f"👁 Наблюдение #{watch_id} создано.\n"
        f"Проверка: раз в {interval_text}.\n"
        f"Условие: {parsed['query']}\n\n"
        "Первую проверку использую как базовую — старые результаты присылать не буду."
    )
    return True


async def watch_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user or not update.effective_chat:
        return
    query = " ".join(context.args).strip() if context.args else ""
    if not query:
        await update.message.reply_text("Использование: /watch условие\nНапример: /watch новые патчи Arma Reforger")
        return
    watch_id = watch_add(update.effective_user.id, update.effective_chat.id, query, WATCH_INTERVAL_MINUTES)
    schedule_watch_job(context.job_queue, watch_id, WATCH_INTERVAL_MINUTES, first_seconds=5)
    await update.message.reply_text(
        f"👁 Наблюдение #{watch_id} создано. Проверяю раз в {WATCH_INTERVAL_MINUTES // 60} ч.\n{query}"
    )


async def watches_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    rows = watch_list(update.effective_user.id)
    if not rows:
        await update.message.reply_text("Активных наблюдений нет.")
        return
    lines = ["👁 Активные наблюдения:"]
    for item in rows[:30]:
        hours = int(item["interval_minutes"]) / 60
        interval_text = f"{int(hours)} ч" if hours.is_integer() else f"{item['interval_minutes']} мин"
        lines.append(f"#{item['id']} · раз в {interval_text} · {item['query']}")
    lines.append("\nУдалить: /delwatch ID")
    await update.message.reply_text("\n".join(lines))


async def delete_watch_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Использование: /delwatch ID")
        return
    watch_id = int(context.args[0])
    if not watch_disable(watch_id, update.effective_user.id):
        await update.message.reply_text("Такого активного наблюдения нет.")
        return
    if context.job_queue:
        for job in context.job_queue.get_jobs_by_name(f"watch:{watch_id}"):
            job.schedule_removal()
    await update.message.reply_text(f"Наблюдение #{watch_id} удалено.")


def _scheduled_state_get(reminder_id: int) -> str:
    with _watch_db() as conn:
        row = conn.execute("SELECT last_digest FROM scheduled_delivery_state WHERE reminder_id=?", (reminder_id,)).fetchone()
    return str(row["last_digest"]) if row else ""


def _scheduled_state_set(reminder_id: int, digest: str) -> None:
    with _watch_db() as conn:
        conn.execute(
            """
            INSERT INTO scheduled_delivery_state(reminder_id,last_digest,last_sent_at) VALUES(?,?,?)
            ON CONFLICT(reminder_id) DO UPDATE SET last_digest=excluded.last_digest,last_sent_at=excluded.last_sent_at
            """,
            (reminder_id, (digest or "")[:6000], _watch_now()),
        )
        conn.commit()


def _generate_deduplicated_scheduled_research(reminder_id: int, task_text: str) -> str | None:
    prompt = _scheduled_task_prompt(task_text)
    deep = is_research_mode(task_text)
    if deep:
        web_context, sources = build_research_web_context(prompt)
    else:
        web_context, sources = build_web_context(prompt)
    if not web_context:
        logger.warning("Scheduled research has no web context: reminder=%s", reminder_id)
        return None

    previous = _scheduled_state_get(reminder_id)
    anti_repeat = ""
    if previous:
        anti_repeat = (
            "\n\nПРЕДЫДУЩАЯ ОТПРАВЛЕННАЯ СВОДКА:\n" + previous[:5000]
            + "\n\nНе повторяй уже отправленные факты. Если в текущих источниках нет действительно новой "
              "существенной информации по сравнению с предыдущей сводкой, верни ровно NO_NEW_INFO. "
              "Если новое есть — сообщи только новые изменения с минимальным контекстом."
        )

    instruction = (
        "Ты выполняешь запланированное глубокое исследование. Сравни источники и отделяй подтверждённые факты от предположений."
        if deep else
        "Ты выполняешь регулярную интернет-сводку пользователя. Выдай только полезные подтверждённые изменения."
    )
    response = llm_client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {
                "role": "system",
                "content": LLM_SYSTEM_PROMPT + "\n\n" + instruction + "\n\n" + web_context + anti_repeat,
            },
            {"role": "user", "content": task_text},
        ],
        temperature=0.25,
        max_tokens=1600 if deep else 900,
        stream=False,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    answer = clean_llm_answer(response.choices[0].message.content or "").strip()
    if not answer or answer.upper().replace(" ", "_") == "NO_NEW_INFO":
        return None

    _scheduled_state_set(reminder_id, answer)
    return append_web_sources(answer, sources)


# Override the reminder job after the base reminder and research modules have loaded.
# This keeps the existing scheduler but suppresses duplicate recurring research output.
async def _reminder_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    reminder_id = int(context.job.data["reminder_id"])
    item = reminder_get(reminder_id)
    if not item or not item.get("enabled"):
        return

    task_kind = item.get("task_kind", "reminder")
    try:
        if task_kind == "research":
            result = await asyncio.to_thread(_generate_deduplicated_scheduled_research, reminder_id, item["text"])
            if result:
                message = f"🔎 Запланированная сводка\n\n{result}"
                for chunk in [message[i:i + 3900] for i in range(0, len(message), 3900)]:
                    await context.bot.send_message(chat_id=int(item["chat_id"]), text=chunk)
            else:
                logger.info("Scheduled research suppressed as duplicate/no-change: id=%s", reminder_id)
        else:
            await context.bot.send_message(chat_id=int(item["chat_id"]), text=f"⏰ Напоминание\n{item['text']}")
    except Exception:
        logger.exception("Failed to run scheduled task id=%s", reminder_id)

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
