# Final reply layer with optional live web context.
# Loaded after continuity/memory modules so this becomes the active reply function.

_prepare_speech_text_without_web_sources = prepare_speech_text


def prepare_speech_text(text: str) -> str:
    """Do not read the technical source list aloud; keep it in Telegram text."""
    spoken_source = re.split(r"\n\nИсточники:\s*\n", text or "", maxsplit=1)[0]
    return _prepare_speech_text_without_web_sources(spoken_source)


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

    # Web retrieval is best-effort. A blocked site/search engine must never
    # prevent the assistant from answering normally.
    try:
        web_context, web_sources = build_web_context(user_text)
    except Exception:
        logger.exception("Web context build failed")
        web_context, web_sources = "", []

    web_instruction = ""
    if web_context:
        web_instruction = (
            "\n\nВЕБ-ПОИСК:\n"
            + web_context
            + "\n\nОтветь на текущий вопрос с учётом найденной информации. "
              "Предпочитай более свежие и прямые источники. "
              "Ссылайся на номера источников [1], [2] только там, где это помогает. "
              "Не придумывай содержимое страниц, которое не было получено."
        )

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
                + web_instruction
            ),
        },
        *recent,
        {"role": "user", "content": user_text},
    ]

    response = llm_client.chat.completions.create(
        model=LLM_MODEL,
        messages=messages,
        temperature=0.58,
        max_tokens=900 if web_context else 750,
        stream=False,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )

    answer_core = clean_llm_answer(response.choices[0].message.content or "")
    if not answer_core:
        raise RuntimeError("LLM returned an empty response.")

    # Keep memory clean: URLs/search snippets are retrieval context, not durable
    # conversation memory. The user still receives a source list in Telegram.
    memory_store.add_message(user_id, "user", user_text, session_id=session_id)
    memory_store.add_message(user_id, "assistant", answer_core, session_id=session_id)

    return append_web_sources(answer_core, web_sources) if web_context else answer_core
