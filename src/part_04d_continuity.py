# v5.3 conversational continuity layer.
# Adds time awareness, safer open-topic closing and firmer recommendation behavior
# without adding a second LLM call to every visible reply.

from zoneinfo import ZoneInfo

USER_TIMEZONE = os.getenv("USER_TIMEZONE", "Europe/Moscow").strip() or "Europe/Moscow"

_RU_WEEKDAYS = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)

_EXPLICIT_DONE_MARKERS = (
    "всё работает",
    "все работает",
    "теперь работает",
    "заработало",
    "всё заработало",
    "все заработало",
    "проблема решена",
    "вопрос решен",
    "вопрос решён",
    "разобрались",
    "готово, работает",
    "закончили с этим",
    "закрыли вопрос",
)

_OPINION_MARKERS = (
    "что думаешь",
    "как считаешь",
    "твоё мнение",
    "твое мнение",
    "что лучше",
    "какой лучше",
    "какой выбрать",
    "что выбрать",
    "стоит ли",
    "что бы ты выбрал",
    "как бы ты сделал",
    "как лучше",
    "посоветуй",
)


def _safe_user_timezone() -> ZoneInfo:
    try:
        return ZoneInfo(USER_TIMEZONE)
    except Exception:
        logger.warning("Invalid USER_TIMEZONE=%r; using UTC", USER_TIMEZONE)
        return ZoneInfo("UTC")


def _parse_memory_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def _previous_user_message_time(user_id: int) -> datetime | None:
    memory_store.ensure_user(user_id)
    with memory_store._lock, memory_store._connect() as conn:
        row = conn.execute(
            """
            SELECT created_at
            FROM messages
            WHERE user_id=? AND role='user'
            ORDER BY id DESC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()
    if not row:
        return None
    return _parse_memory_timestamp(row["created_at"])


def build_temporal_context(user_id: int) -> str:
    """Give the LLM useful clock/continuity context without forcing it into prose."""
    tz = _safe_user_timezone()
    now_local = datetime.now(tz)
    previous_utc = _previous_user_message_time(user_id)

    if previous_utc is None:
        continuity = "Это первый сохранённый разговор с пользователем."
        gap_text = "нет предыдущего сохранённого сообщения"
    else:
        previous_local = previous_utc.astimezone(tz)
        gap_seconds = max(0.0, (now_local - previous_local).total_seconds())
        if gap_seconds < 15 * 60:
            continuity = "Диалог идёт практически без паузы; считай это прямым продолжением разговора."
            gap_text = f"около {max(1, int(gap_seconds // 60))} мин"
        elif gap_seconds < 2 * 3600:
            continuity = "Пользователь вернулся после небольшой паузы; контекст прошлого обмена всё ещё очень актуален."
            gap_text = f"около {max(1, int(gap_seconds // 60))} мин"
        elif gap_seconds < 24 * 3600:
            continuity = "Пользователь вернулся позже в тот же день или спустя несколько часов. Не делай вид, что разговор только начался."
            gap_text = f"около {gap_seconds / 3600:.1f} ч"
        elif gap_seconds < 72 * 3600:
            days = max(1, int(gap_seconds // 86400))
            continuity = "Была заметная пауза. Связывай новый разговор со старым только когда тема действительно совпадает."
            gap_text = f"около {days} дн."
        else:
            days = max(3, int(gap_seconds // 86400))
            continuity = "После прошлого разговора прошло много времени. Не тащи старые темы в новый разговор без явной смысловой связи."
            gap_text = f"около {days} дн."

    weekday = _RU_WEEKDAYS[now_local.weekday()]
    return (
        "КОНТЕКСТ ВРЕМЕНИ И ПРОДОЛЖЕНИЯ:\n"
        f"Локальное время пользователя: {now_local:%Y-%m-%d %H:%M}, {weekday}; "
        f"часовой пояс: {getattr(tz, 'key', USER_TIMEZONE)}.\n"
        f"Пауза с предыдущего сообщения пользователя: {gap_text}.\n"
        f"{continuity}\n"
        "Используй время для слов «сегодня», «вчера», «завтра», «утром», «вечером» и для естественного продолжения разговора. "
        "Не сообщай дату, время или длительность паузы без необходимости и не говори, что отслеживаешь пользователя."
    )


def maybe_close_explicitly_finished_topic(user_id: int, user_text: str) -> str | None:
    """
    Close a stored open topic when the user explicitly says the current problem is done.
    Conservative by design: a generic "всё работает" only auto-closes when there is
    exactly one open topic. With several topics, semantic evidence is required.
    """
    low = re.sub(r"\s+", " ", (user_text or "").strip().lower())
    if not low or not any(marker in low for marker in _EXPLICIT_DONE_MARKERS):
        return None

    topics = memory_get_open_topics(user_id, 12)
    if not topics:
        return None

    if len(topics) == 1:
        topic = topics[0]
        memory_update_item(int(topic["id"]), status="closed")
        logger.info("Open topic explicitly closed: user=%s topic=%r", user_id, topic["content"])
        return str(topic["content"])

    # Several open topics: close only a clearly related one.
    try:
        query_embedding = _memory_embed(user_text)
    except Exception:
        query_embedding = []

    ranked: list[tuple[float, dict]] = []
    for topic in topics:
        lexical = _lexical_memory_score(user_text, topic.get("content", ""))
        semantic = 0.0
        topic_embedding = _load_embedding(topic.get("embedding", ""))
        if query_embedding and topic_embedding:
            semantic = _cosine_similarity(query_embedding, topic_embedding)
        ranked.append((max(semantic, lexical * 0.72), topic))

    ranked.sort(key=lambda x: x[0], reverse=True)
    if ranked and ranked[0][0] >= 0.42:
        topic = ranked[0][1]
        memory_update_item(int(topic["id"]), status="closed")
        logger.info(
            "Open topic semantically closed: user=%s score=%.3f topic=%r",
            user_id,
            ranked[0][0],
            topic["content"],
        )
        return str(topic["content"])
    return None


def build_decision_stance_context(user_text: str) -> str:
    low = (user_text or "").lower()
    if not any(marker in low for marker in _OPINION_MARKERS):
        return (
            "ПОЗИЦИЯ В ОТВЕТЕ:\n"
            "Не соглашайся автоматически и не спорь ради спора. Если у вариантов есть явный лидер, можешь назвать его прямо."
        )

    return (
        "ПОЗИЦИЯ В ОТВЕТЕ:\n"
        "Пользователь просит мнение или выбор. Сначала дай одну ясную рекомендацию/позицию. "
        "Потом коротко объясни главные причины и компромисс. Не прячь решение за нейтральным списком одинаково хороших вариантов. "
        "Если данных недостаточно для уверенного выбора — прямо назови, какого ключевого факта не хватает."
    )


def generate_assistant_reply(user_id: int, user_text: str) -> str:
    session_id = memory_store.get_active_session(user_id)

    # Resolve an explicitly finished topic before building memory context so it
    # does not get presented to the model as still open in the same turn.
    try:
        closed_topic = maybe_close_explicitly_finished_topic(user_id, user_text)
    except Exception:
        logger.exception("Explicit open-topic close failed")
        closed_topic = None

    try:
        summary = build_memory_summary(user_id, force=False)
    except Exception:
        logger.exception("Background memory summarization failed")
        summary = memory_store.get_long_term_summary(user_id)

    recent = memory_store.get_recent_messages(
        user_id,
        limit=MEMORY_RECENT_MESSAGES,
        session_id=session_id,
    )

    try:
        relevant = retrieve_relevant_memories(user_id, user_text)
    except Exception:
        logger.exception("Semantic memory retrieval failed")
        relevant = []

    try:
        open_topics = memory_get_open_topics(user_id, 6)
    except Exception:
        open_topics = []

    relevant_text = "\n".join(
        f"- [{item['kind']}] {item['content']}"
        for item in relevant
    ) or "(ничего явно связанного не найдено)"

    topics_text = "\n".join(
        f"- {item['content']}"
        for item in open_topics
    ) or "(нет)"

    closed_note = (
        f"\nТолько что пользователь явно сообщил о завершении темы: {closed_topic}. Считай её закрытой."
        if closed_topic else ""
    )

    memory_context = (
        "ПАМЯТЬ СОБЕСЕДНИКА:\n"
        "Релевантные воспоминания по смыслу:\n"
        + relevant_text
        + "\n\nНезавершённые темы:\n"
        + topics_text
        + "\n\nСтарое компактное резюме (используй как резерв, если полезно):\n"
        + ((summary or "(пусто)")[:2800])
        + closed_note
        + "\n\nНе перечисляй память пользователю и не демонстрируй её ради эффекта. "
          "Используй только то, что естественно связано с текущим сообщением. "
          "Незавершённую тему упоминай только если связь действительно есть."
    )

    interaction_context = infer_interaction_context(user_text)
    adaptive_context = build_adaptive_personality_context(user_id, user_text)
    temporal_context = build_temporal_context(user_id)
    stance_context = build_decision_stance_context(user_text)

    messages = [
        {
            "role": "system",
            "content": (
                LLM_SYSTEM_PROMPT
                + "\n\n" + PERSONALITY_ENGINE_PROMPT
                + "\n\n" + memory_context
                + "\n\n" + temporal_context
                + "\n\n" + interaction_context
                + "\n\n" + adaptive_context
                + "\n\n" + stance_context
            ),
        },
        *recent,
        {"role": "user", "content": user_text},
    ]

    response = llm_client.chat.completions.create(
        model=LLM_MODEL,
        messages=messages,
        temperature=0.58,
        max_tokens=750,
        stream=False,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )

    answer = clean_llm_answer(response.choices[0].message.content or "")
    if not answer:
        raise RuntimeError("LLM returned an empty response.")

    memory_store.add_message(user_id, "user", user_text, session_id=session_id)
    memory_store.add_message(user_id, "assistant", answer, session_id=session_id)
    return answer
