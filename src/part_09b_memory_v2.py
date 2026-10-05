_memory_refreshing_users: set[int] = set()


async def _refresh_memory_once(user_id: int) -> None:
    if user_id in _memory_refreshing_users:
        return
    _memory_refreshing_users.add(user_id)
    try:
        await refresh_structured_memory_background(user_id)
    finally:
        _memory_refreshing_users.discard(user_id)


def _schedule_memory_refresh(user_id: int) -> None:
    try:
        asyncio.create_task(_refresh_memory_once(user_id))
    except Exception:
        logger.exception("Could not schedule structured memory refresh")


async def process_user_text(
    update: Update,
    text: str,
    status=None,
) -> None:
    if not update.message:
        return

    text = (text or "").strip()
    if not text:
        return

    if len(text) > 4000:
        message = "Сообщение слишком длинное. Пока отправляй до 4000 символов."
        if status:
            await status.edit_text(message)
        else:
            await update.message.reply_text(message)
        return

    answer_published = False
    if status is None:
        status = await update.message.reply_text("Думаю…")
    else:
        await status.edit_text("Думаю…")

    try:
        user_id = update.effective_user.id if update.effective_user else 0
        answer = await asyncio.to_thread(generate_assistant_reply, user_id, text)

        # Memory extraction is intentionally background work: it must not make
        # the visible answer slower. It runs every MEMORY_EXTRACT_INTERVAL turns.
        _schedule_memory_refresh(user_id)

        mode = get_output_mode(user_id)
        if mode == "manual":
            token = remember_manual_voice(user_id, answer)
            await status.edit_text(answer, reply_markup=play_keyboard(token))
            answer_published = True
            return

        await status.edit_text(answer)
        answer_published = True

        selected_voice = get_tts_voice(user_id)
        exaggeration = get_tts_exaggeration(user_id)
        spoken_answer = prepare_speech_text(answer)
        ogg_bytes, wav_bytes, duration = await asyncio.to_thread(
            synthesize_voice,
            spoken_answer,
            selected_voice,
            exaggeration,
        )

        await update.message.reply_voice(
            voice=InputFile(ogg_bytes, filename="speech.ogg"),
            duration=max(1, int(duration + 0.999)),
        )

        try:
            await asyncio.to_thread(play_wav_on_windows, wav_bytes)
            logger.info("Local autoplay finished: %.2fs", duration)
        except Exception as playback_exc:
            logger.exception("Local Windows playback failed")
            await update.message.reply_text(
                "Голосовое отправлено в Telegram, но автопроигрывание на ПК не сработало: "
                f"{type(playback_exc).__name__}: {playback_exc}"
            )

    except grpc.RpcError as exc:
        code = exc.code().name if exc.code() else "RPC_ERROR"
        detail = (exc.details() or "без описания")[:700]
        logger.warning("NVIDIA gRPC error: %s: %s", code, detail)
        error_text = f"Ошибка NVIDIA: {code}: {detail}"
        if answer_published:
            await update.message.reply_text(error_text)
        else:
            await status.edit_text(error_text)

    except Exception as exc:
        logger.exception("Assistant processing failed")
        error_text = f"Ошибка озвучки/отправки: {type(exc).__name__}: {exc}"
        if answer_published:
            await update.message.reply_text(error_text)
        else:
            await status.edit_text(f"Ошибка: {type(exc).__name__}: {exc}")


async def memory_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not update.message or not update.effective_user:
        return

    user_id = update.effective_user.id
    try:
        summary = await asyncio.to_thread(memory_store.get_long_term_summary, user_id)
        active_session = await asyncio.to_thread(memory_store.get_active_session, user_id)
        session_count = await asyncio.to_thread(memory_store.current_session_count, user_id)
        total_count = await asyncio.to_thread(memory_store.total_message_count, user_id)
        structured_count = await asyncio.to_thread(memory_count_items, user_id, "active")
        topics = await asyncio.to_thread(memory_get_open_topics, user_id, 6)
        styles = await asyncio.to_thread(memory_get_style_items, user_id, 5)
    except Exception as exc:
        logger.exception("Memory status failed")
        await update.message.reply_text(
            f"Ошибка чтения памяти: {type(exc).__name__}: {exc}"
        )
        return

    topics_text = "\n".join(f"• {x['content']}" for x in topics) or "• нет"
    styles_text = "\n".join(f"• {x['content']}" for x in styles) or "• пока не сформированы"
    summary_text = summary or "(старое компактное резюме пока пустое)"
    if len(summary_text) > 1800:
        summary_text = summary_text[:1800] + "…"

    await update.message.reply_text(
        "Память собеседника:\n"
        f"Сессия: #{active_session}\n"
        f"Сообщений в текущем диалоге: {session_count}\n"
        f"Всего сообщений: {total_count}\n"
        f"Активных структурированных воспоминаний: {structured_count}\n\n"
        f"Открытые темы:\n{topics_text}\n\n"
        f"Изученный стиль/предпочтения:\n{styles_text}\n\n"
        f"Резервное резюме:\n{summary_text}"
    )
