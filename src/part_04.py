def build_memory_summary(user_id: int, force: bool = False) -> str:
    """
    Fold older messages into a compact long-term summary.

    This is best-effort. A summarization failure never breaks the main reply.
    """
    older = memory_store.get_unsummarized_older_messages(
        user_id,
        keep_latest=0 if force else MEMORY_KEEP_UNSUMMARIZED,
    )

    if not older:
        return memory_store.get_long_term_summary(user_id)

    if not force and len(older) < (
        MEMORY_SUMMARY_TRIGGER - MEMORY_KEEP_UNSUMMARIZED
    ):
        return memory_store.get_long_term_summary(user_id)

    existing = memory_store.get_long_term_summary(user_id)

    transcript_lines = []
    for row in older:
        who = "Пользователь" if row["role"] == "user" else "Ассистент"
        transcript_lines.append(
            f"{who}: {row['content']}"
        )

    transcript = "\n".join(transcript_lines)

    summary_prompt = (
        "Обнови долгосрочную память голосового ассистента. "
        "Сохраняй только то, что реально поможет в будущих разговорах. "
        "Обязательно отслеживай: устойчивые предпочтения пользователя; "
        "предпочтительный стиль общения; важные факты; текущие проекты; "
        "принятые решения и договорённости; незавершённые задачи и темы. "
        "Если ранее открытая тема явно завершена, больше не держи её как открытую. "
        "Не сохраняй приветствия, случайную болтовню, одноразовые эмоции, "
        "секреты/ключи и внутренние рассуждения модели.\n\n"
        "Старайся поддерживать компактную структуру:\n"
        "ПРЕДПОЧТЕНИЯ И СТИЛЬ:\n"
        "ВАЖНЫЕ ФАКТЫ И ПРОЕКТЫ:\n"
        "РЕШЕНИЯ И ДОГОВОРЁННОСТИ:\n"
        "ОТКРЫТЫЕ ТЕМЫ:\n\n"
        f"Предыдущая долгосрочная память:\n{existing or '(пусто)'}\n\n"
        f"Новые сообщения для свёртки:\n{transcript}"
    )

    response = llm_client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "Ты модуль памяти. Верни только обновлённое краткое резюме "
                    "без рассуждений и markdown."
                ),
            },
            {"role": "user", "content": summary_prompt},
        ],
        temperature=0.2,
        max_tokens=700,
        stream=False,
        extra_body={
            "chat_template_kwargs": {
                "enable_thinking": False,
            }
        },
    )

    summary = clean_llm_answer(
        response.choices[0].message.content or ""
    ).strip()

    if summary:
        memory_store.set_long_term_summary(user_id, summary)
        memory_store.mark_summarized(
            [int(row["id"]) for row in older]
        )
        logger.info(
            "Memory summary updated: user=%s messages=%d chars=%d",
            user_id,
            len(older),
            len(summary),
        )

    return summary or existing


def generate_assistant_reply(user_id: int, user_text: str) -> str:
    session_id = memory_store.get_active_session(user_id)

    # Summarize older history only when enough material has accumulated.
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

    memory_context = (
        "ДОЛГОСРОЧНАЯ ПАМЯТЬ О ПРЕДЫДУЩИХ РАЗГОВОРАХ:\n"
        + (summary if summary else "(пока пусто)")
        + "\n\n"
        "Используй эту память только когда она действительно относится к "
        "текущему вопросу. Не пересказывай её пользователю без необходимости. "
        "Если в памяти есть открытая тема, возвращайся к ней только естественно "
        "и по связи с текущим разговором."
    )

    interaction_context = infer_interaction_context(user_text)

    messages = [
        {
            "role": "system",
            "content": (
                LLM_SYSTEM_PROMPT
                + "\n\n"
                + PERSONALITY_ENGINE_PROMPT
                + "\n\n"
                + memory_context
                + "\n\n"
                + interaction_context
            ),
        },
        *recent,
        {"role": "user", "content": user_text},
    ]

    response = llm_client.chat.completions.create(
        model=LLM_MODEL,
        messages=messages,
        temperature=0.55,
        max_tokens=700,
        stream=False,
        extra_body={
            "chat_template_kwargs": {
                "enable_thinking": False,
            }
        },
    )

    answer = clean_llm_answer(
        response.choices[0].message.content or ""
    )

    if not answer:
        raise RuntimeError("LLM returned an empty response.")

    # Persist both sides of the conversation only after a successful LLM call.
    memory_store.add_message(
        user_id,
        "user",
        user_text,
        session_id=session_id,
    )
    memory_store.add_message(
        user_id,
        "assistant",
        answer,
        session_id=session_id,
    )

    return answer


