# Final assistant reply layer: isolated project memory + optional deep research.
# Loaded after the normal web integration so this becomes the active generator.


def _general_style_context(user_id: int) -> str:
    """Global style/preferences are safe to reuse inside projects; project facts are not."""
    try:
        items = memory_list_items(
            user_id,
            status="active",
            kinds=("style", "preference"),
            limit=12,
        )
    except Exception:
        items = []
    if not items:
        return ""
    return "ОБЩИЕ ПРЕДПОЧТЕНИЯ ПОЛЬЗОВАТЕЛЯ:\n" + "\n".join(
        f"- {item['content']}" for item in items
    )


def _build_normal_memory_context(user_id: int, user_text: str) -> tuple[str, list[dict], str | None]:
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
        session_id=memory_store.get_active_session(user_id),
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
        f"- [{item['kind']}] {item['content']}" for item in relevant
    ) or "(ничего явно связанного не найдено)"
    topics_text = "\n".join(f"- {item['content']}" for item in open_topics) or "(нет)"
    closed_note = (
        f"\nТолько что пользователь явно сообщил о завершении темы: {closed_topic}. Считай её закрытой."
        if closed_topic else ""
    )
    context = (
        "ПАМЯТЬ СОБЕСЕДНИКА:\n"
        "Релевантные воспоминания по смыслу:\n" + relevant_text
        + "\n\nНезавершённые темы:\n" + topics_text
        + "\n\nСтарое компактное резюме (используй как резерв, если полезно):\n"
        + ((summary or "(пусто)")[:2800])
        + closed_note
        + "\n\nНе перечисляй память пользователю и не демонстрируй её ради эффекта. "
          "Используй только то, что естественно связано с текущим сообщением."
    )
    return context, recent, closed_topic


def generate_assistant_reply(user_id: int, user_text: str) -> str:
    active_project = project_get_active(user_id)

    if active_project:
        # Strict project isolation: use only this project's recent history and
        # durable project summary. Global style/preferences may still apply.
        recent = project_recent_messages(user_id, PROJECT_RECENT_MESSAGES)
        memory_context = build_project_memory_context(user_id)
        style_context = _general_style_context(user_id)
    else:
        memory_context, recent, _closed_topic = _build_normal_memory_context(user_id, user_text)
        style_context = ""

    interaction_context = infer_interaction_context(user_text)
    adaptive_context = build_adaptive_personality_context(user_id, user_text)
    temporal_context = build_temporal_context(user_id)
    stance_context = build_decision_stance_context(user_text)

    research_mode = is_research_mode(user_text)
    try:
        if research_mode:
            web_context, web_sources = build_research_web_context(user_text)
        else:
            web_context, web_sources = build_web_context(user_text)
    except Exception:
        logger.exception("Web/research context build failed")
        web_context, web_sources = "", []

    web_instruction = ""
    if web_context:
        if research_mode:
            web_instruction = (
                "\n\nИССЛЕДОВАТЕЛЬСКИЙ РЕЖИМ:\n" + web_context
                + "\n\nСделай содержательный итог исследования. Сначала дай короткий вывод, затем ключевые факты, "
                  "сравнение/расхождения источников и практический вывод. Не скрывай неопределённость. "
                  "При важных фактах указывай номера источников [1], [2] и т.д. Не выдумывай данные."
            )
        else:
            web_instruction = (
                "\n\nВЕБ-ПОИСК:\n" + web_context
                + "\n\nОтветь с учётом найденной информации. Предпочитай более свежие и прямые источники. "
                  "Ссылайся на номера источников [1], [2] там, где это помогает. Не придумывай содержимое страниц."
            )
    elif research_mode:
        web_instruction = (
            "\n\nПользователь запросил глубокое исследование, но веб-источники получить не удалось. "
            "Скажи об этом прямо и не выдавай старые знания за свежую проверку."
        )

    system_content = (
        LLM_SYSTEM_PROMPT
        + "\n\n" + PERSONALITY_ENGINE_PROMPT
        + "\n\n" + memory_context
        + ("\n\n" + style_context if style_context else "")
        + "\n\n" + temporal_context
        + "\n\n" + interaction_context
        + "\n\n" + adaptive_context
        + "\n\n" + stance_context
        + web_instruction
    )

    response = llm_client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": system_content},
            *recent,
            {"role": "user", "content": user_text},
        ],
        temperature=0.42 if research_mode else 0.58,
        max_tokens=1900 if research_mode else (900 if web_context else 750),
        stream=False,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )

    answer_core = clean_llm_answer(response.choices[0].message.content or "")
    if not answer_core:
        raise RuntimeError("LLM returned an empty response.")

    if active_project:
        project_add_message(user_id, "user", user_text)
        project_add_message(user_id, "assistant", answer_core)
        try:
            project_refresh_summary(user_id, force=False)
        except Exception:
            logger.exception("Project memory refresh failed")
    else:
        session_id = memory_store.get_active_session(user_id)
        memory_store.add_message(user_id, "user", user_text, session_id=session_id)
        memory_store.add_message(user_id, "assistant", answer_core, session_id=session_id)

    return append_web_sources(answer_core, web_sources) if web_context else answer_core


def generate_scheduled_research(task_text: str) -> str:
    """Scheduled research also honors deep-research wording."""
    prompt = _scheduled_task_prompt(task_text)
    deep = is_research_mode(task_text)
    if deep:
        web_context, sources = build_research_web_context(prompt)
    else:
        web_context, sources = build_web_context(prompt)

    if not web_context:
        return (
            "Не удалось получить свежую информацию из интернета по запланированной задаче. "
            "Попробую снова при следующем запуске, если задача повторяется."
        )

    instruction = (
        "Ты выполняешь запланированное глубокое исследование. Сравни несколько источников, отметь расхождения, "
        "дай вывод и только подтверждённые факты."
        if deep else
        "Ты выполняешь запланированную задачу пользователя. Сделай готовую краткую полезную выжимку."
    )
    response = llm_client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {
                "role": "system",
                "content": LLM_SYSTEM_PROMPT + "\n\n" + instruction + "\n\n" + web_context,
            },
            {"role": "user", "content": task_text},
        ],
        temperature=0.3,
        max_tokens=1600 if deep else 900,
        stream=False,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    answer = clean_llm_answer(response.choices[0].message.content or "").strip()
    if not answer:
        answer = "Не удалось сформировать результат по запланированной задаче."
    return append_web_sources(answer, sources)
