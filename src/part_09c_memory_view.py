async def memory_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not update.message or not update.effective_user:
        return

    user_id = update.effective_user.id
    try:
        await asyncio.to_thread(_repair_memory_item_kinds_v521, user_id)
        summary = await asyncio.to_thread(memory_store.get_long_term_summary, user_id)
        active_session = await asyncio.to_thread(memory_store.get_active_session, user_id)
        session_count = await asyncio.to_thread(memory_store.current_session_count, user_id)
        total_count = await asyncio.to_thread(memory_store.total_message_count, user_id)
        structured_count = await asyncio.to_thread(memory_count_items, user_id, "active")
        topics = await asyncio.to_thread(memory_get_open_topics, user_id, 6)
        styles = await asyncio.to_thread(memory_get_style_items, user_id, 8)
        facts = await asyncio.to_thread(
            memory_list_items,
            user_id,
            "active",
            ("fact", "project", "decision"),
            10,
        )
    except Exception as exc:
        logger.exception("Memory status failed")
        await update.message.reply_text(
            f"Ошибка чтения памяти: {type(exc).__name__}: {exc}"
        )
        return

    topics_text = "\n".join(f"• {x['content']}" for x in topics) or "• нет"
    styles_text = "\n".join(f"• {x['content']}" for x in styles) or "• пока не сформированы"
    facts_text = "\n".join(f"• {x['content']}" for x in facts) or "• пока нет"
    summary_text = summary or "(старое компактное резюме пока пустое)"
    if len(summary_text) > 1400:
        summary_text = summary_text[:1400] + "…"

    text = (
        "Память собеседника:\n"
        f"Сессия: #{active_session}\n"
        f"Сообщений в текущем диалоге: {session_count}\n"
        f"Всего сообщений: {total_count}\n"
        f"Активных структурированных воспоминаний: {structured_count}\n\n"
        f"Факты / проекты / решения:\n{facts_text}\n\n"
        f"Открытые темы:\n{topics_text}\n\n"
        f"Стиль и предпочтения:\n{styles_text}\n\n"
        f"Резервное резюме:\n{summary_text}"
    )
    await update.message.reply_text(text[:4000])
