def _memory_embed(text: str) -> list[float]:
    if not MEMORY_STRUCTURED_ENABLED:
        return []
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if not text:
        return []
    response = llm_client.embeddings.create(
        model=MEMORY_EMBEDDING_MODEL,
        input=[text[:12000]],
        encoding_format="float",
        extra_body={"truncate": "END"},
    )
    return list(response.data[0].embedding)


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if not na or not nb:
        return 0.0
    return dot / (na * nb)


def _load_embedding(raw: str) -> list[float]:
    try:
        value = json.loads(raw or "[]")
        if isinstance(value, list):
            return [float(x) for x in value]
    except Exception:
        pass
    return []


def _lexical_memory_score(query: str, content: str) -> float:
    q = {w for w in re.findall(r"[\w-]{3,}", query.lower())}
    c = {w for w in re.findall(r"[\w-]{3,}", content.lower())}
    if not q or not c:
        return 0.0
    return len(q & c) / max(1, len(q))


def retrieve_relevant_memories(user_id: int, query: str) -> list[dict]:
    if not MEMORY_STRUCTURED_ENABLED:
        return []

    items = memory_list_items(
        user_id,
        status="active",
        kinds=("fact", "project", "decision", "preference", "open_topic"),
        limit=MEMORY_MAX_ITEMS,
    )
    if not items:
        return []

    query_embedding: list[float] = []
    try:
        query_embedding = _memory_embed(query)
    except Exception:
        logger.exception("Semantic memory query embedding failed; using lexical fallback")

    ranked = []
    for item in items:
        semantic = 0.0
        emb = _load_embedding(item.get("embedding", ""))
        if query_embedding and emb:
            semantic = _cosine_similarity(query_embedding, emb)
        lexical = _lexical_memory_score(query, item.get("content", ""))
        score = max(semantic, lexical * 0.72)
        score += max(0, int(item.get("importance", 3)) - 3) * 0.015
        if item.get("kind") == "open_topic":
            score += 0.02
        ranked.append((score, item))

    ranked.sort(key=lambda x: x[0], reverse=True)
    selected = [item for score, item in ranked if score >= MEMORY_MIN_SIMILARITY]

    # If embeddings are temporarily unavailable, still return a couple of
    # obvious lexical/high-importance matches instead of losing all memory.
    if not selected and ranked:
        selected = [item for score, item in ranked[:2] if score >= 0.12]

    return selected[:MEMORY_SEMANTIC_TOP_K]


def _parse_json_object(text: str) -> dict:
    cleaned = clean_llm_answer(text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("Memory extractor did not return JSON")
    return json.loads(cleaned[start:end + 1])


def _find_semantic_duplicate(user_id: int, kind: str, embedding: list[float]) -> dict | None:
    if not embedding:
        return None
    for item in memory_list_items(user_id, "active", (kind,), 60):
        old_emb = _load_embedding(item.get("embedding", ""))
        if old_emb and _cosine_similarity(embedding, old_emb) >= 0.92:
            return item
    return None


def _close_matching_topic(user_id: int, topic_text: str) -> bool:
    topics = memory_get_open_topics(user_id, 20)
    if not topics:
        return False

    normalized = _normalize_memory_text(topic_text)
    for topic in topics:
        old = _normalize_memory_text(topic["content"])
        if normalized in old or old in normalized:
            memory_update_item(int(topic["id"]), status="closed")
            return True

    try:
        q_emb = _memory_embed(topic_text)
    except Exception:
        q_emb = []

    best = (0.0, None)
    if q_emb:
        for topic in topics:
            emb = _load_embedding(topic.get("embedding", ""))
            score = _cosine_similarity(q_emb, emb) if emb else 0.0
            if score > best[0]:
                best = (score, topic)
    if best[1] is not None and best[0] >= 0.55:
        memory_update_item(int(best[1]["id"]), status="closed")
        return True
    return False


def extract_structured_memory(user_id: int, force: bool = False) -> None:
    if not MEMORY_STRUCTURED_ENABLED:
        return

    last_processed = memory_get_last_extract_message_id(user_id)
    new_rows = memory_messages_after(user_id, last_processed, limit=24)
    if not new_rows:
        return

    user_turns = sum(1 for row in new_rows if row["role"] == "user")
    if not force and user_turns < max(1, MEMORY_EXTRACT_INTERVAL):
        return

    latest_id = max(int(row["id"]) for row in new_rows)
    transcript = "\n".join(
        ("Пользователь" if row["role"] == "user" else "Ассистент")
        + ": " + row["content"]
        for row in new_rows[-16:]
    )

    existing_summary = memory_store.get_long_term_summary(user_id)
    existing_items = memory_list_items(user_id, "active", None, 30)
    existing_text = "\n".join(
        f"- {item['kind']}: {item['content']}"
        for item in existing_items
    ) or "(нет)"

    prompt = f"""
Ты модуль структурированной памяти постоянного русскоязычного собеседника.
Извлеки только сведения, которые реально пригодятся в будущих разговорах.

Разрешённые kind:
- preference: устойчивые предпочтения пользователя;
- style: как пользователь предпочитает получать ответы;
- fact: важный явно сообщённый факт;
- project: длительный проект/задача;
- decision: принятое решение/договорённость;
- open_topic: незавершённая тема, к которой нужно вернуться.

Не сохраняй секреты, токены, ключи, пароли, одноразовые эмоции, приветствия,
случайную болтовню, догадки о чувствительных свойствах пользователя или факты,
которые пользователь прямо не сообщал. Не придумывай ничего.
Если тема явно завершилась, добавь её краткое название в close_topics.
Стиль сохраняй только если предпочтение явно сказано или подтверждается повторно.

Верни ТОЛЬКО JSON такого вида:
{{
  "upsert": [
    {{"kind":"style","content":"...","importance":1}}
  ],
  "close_topics": ["..."]
}}
importance: 1..5. Максимум 8 upsert за один проход.

Старое резюме:
{existing_summary or '(пусто)'}

Уже сохранённые структурированные воспоминания:
{existing_text}

Новые сообщения:
{transcript}
""".strip()

    response = llm_client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {
                "role": "system",
                "content": "Ты модуль памяти. Возвращай только валидный JSON без markdown и рассуждений.",
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.1,
        max_tokens=650,
        stream=False,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )

    data = _parse_json_object(response.choices[0].message.content or "")
    upserts = data.get("upsert", [])
    close_topics = data.get("close_topics", [])

    if isinstance(upserts, list):
        for raw in upserts[:8]:
            if not isinstance(raw, dict):
                continue
            kind = str(raw.get("kind", "")).strip()
            content = re.sub(r"\s+", " ", str(raw.get("content", ""))).strip()
            if kind not in MEMORY_KINDS or len(content) < 5 or len(content) > 500:
                continue
            try:
                importance = max(1, min(5, int(raw.get("importance", 3))))
            except Exception:
                importance = 3

            try:
                emb = _memory_embed(content)
            except Exception:
                logger.exception("Memory item embedding failed")
                emb = []

            duplicate = _find_semantic_duplicate(user_id, kind, emb)
            if duplicate is not None:
                memory_update_item(
                    int(duplicate["id"]),
                    content=content,
                    importance=max(importance, int(duplicate.get("importance", 3))),
                    embedding=emb or None,
                    status="active",
                )
            else:
                memory_upsert_item(user_id, kind, content, importance, emb)

    if isinstance(close_topics, list):
        for topic in close_topics[:8]:
            topic_text = re.sub(r"\s+", " ", str(topic)).strip()
            if topic_text:
                _close_matching_topic(user_id, topic_text)

    memory_set_last_extract_message_id(user_id, latest_id)
    logger.info(
        "Structured memory refreshed: user=%s upserts=%d close_topics=%d through_message=%d",
        user_id,
        len(upserts) if isinstance(upserts, list) else 0,
        len(close_topics) if isinstance(close_topics, list) else 0,
        latest_id,
    )


async def refresh_structured_memory_background(user_id: int) -> None:
    try:
        await asyncio.to_thread(extract_structured_memory, user_id, False)
    except Exception:
        logger.exception("Structured memory background refresh failed")


def build_adaptive_personality_context(user_id: int, user_text: str) -> str:
    styles = memory_get_style_items(user_id, 8) if MEMORY_STRUCTURED_ENABLED else []
    style_lines = "\n".join(f"- {item['content']}" for item in styles)

    raw = (user_text or "").strip()
    low = raw.lower()
    dynamic = []
    if len(raw) <= 60:
        dynamic.append("Сейчас пользователь пишет коротко: не раздувай ответ без причины.")
    if any(x in low for x in ("подробно", "распиши", "объясни", "разбер", "почему")):
        dynamic.append("Сейчас уместен более подробный разбор, но без повторов.")
    if any(x in low for x in ("что думаешь", "как считаешь", "посоветуй", "стоит ли")):
        dynamic.append("Дай ясную собственную оценку и аргументы, а не только нейтральные варианты.")

    return (
        "АДАПТИВНЫЙ СТИЛЬ:\n"
        + ("Устойчивые предпочтения пользователя:\n" + style_lines + "\n" if style_lines else "")
        + ("\n".join(dynamic) if dynamic else "Подстраивай длину и тон под текущую реплику.")
        + "\nНе копируй эти правила в ответ и не упоминай, что подстраиваешься."
    )


def generate_assistant_reply(user_id: int, user_text: str) -> str:
    session_id = memory_store.get_active_session(user_id)

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

    memory_context = (
        "ПАМЯТЬ СОБЕСЕДНИКА:\n"
        "Релевантные воспоминания по смыслу:\n"
        + relevant_text
        + "\n\nНезавершённые темы:\n"
        + topics_text
        + "\n\nСтарое компактное резюме (используй как резерв, если полезно):\n"
        + ((summary or "(пусто)")[:2800])
        + "\n\nНе перечисляй память пользователю и не демонстрируй её ради эффекта. "
          "Используй только то, что естественно связано с текущим сообщением. "
          "Незавершённую тему упоминай только если связь действительно есть."
    )

    interaction_context = infer_interaction_context(user_text)
    adaptive_context = build_adaptive_personality_context(user_id, user_text)

    messages = [
        {
            "role": "system",
            "content": (
                LLM_SYSTEM_PROMPT
                + "\n\n" + PERSONALITY_ENGINE_PROMPT
                + "\n\n" + memory_context
                + "\n\n" + interaction_context
                + "\n\n" + adaptive_context
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
