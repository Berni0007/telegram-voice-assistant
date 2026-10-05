async def new_dialog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return

    user_id = update.effective_user.id
    status = await update.message.reply_text(
        "Сохраняю важный контекст и начинаю новый диалог…"
    )

    try:
        await asyncio.to_thread(build_memory_summary, user_id, True)
    except Exception:
        logger.exception("Memory summary on /new failed")

    try:
        await asyncio.to_thread(extract_structured_memory, user_id, True)
    except Exception:
        logger.exception("Structured memory extraction on /new failed")

    session_id = memory_store.new_session(user_id)
    await status.edit_text(
        f"Новый диалог начат. Важные факты, предпочтения и открытые темы сохранены. "
        f"Сессия #{session_id}."
    )
