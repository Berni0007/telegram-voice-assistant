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
        if status:
            await status.edit_text(
                "Сообщение слишком длинное. Пока отправляй до 4000 символов."
            )
        else:
            await update.message.reply_text(
                "Сообщение слишком длинное. Пока отправляй до 4000 символов."
            )
        return

    answer_published = False

    if status is None:
        status = await update.message.reply_text("Думаю…")
    else:
        await status.edit_text("Думаю…")

    try:
        user_id = update.effective_user.id if update.effective_user else 0

        answer = await asyncio.to_thread(
            generate_assistant_reply,
            user_id,
            text,
        )

        mode = get_output_mode(user_id)

        if mode == "manual":
            token = remember_manual_voice(user_id, answer)
            await status.edit_text(
                answer,
                reply_markup=play_keyboard(token),
            )
            answer_published = True
            return

        # AUTO mode:
        #   1) show text in Telegram;
        #   2) from this point NEVER overwrite/remove that text;
        #   3) synthesize/send/play audio as a separate best-effort stage.
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

        # Always show a playable voice message on phone/desktop Telegram.
        await update.message.reply_voice(
            voice=InputFile(ogg_bytes, filename="speech.ogg"),
            duration=max(1, int(duration + 0.999)),
        )

        # Then autoplay the same generated reply on the PC where bot.py runs.
        try:
            await asyncio.to_thread(
                play_wav_on_windows,
                wav_bytes,
            )
            logger.info(
                "Local autoplay finished: %.2fs",
                duration,
            )
        except Exception as playback_exc:
            # Telegram voice has already been delivered, so a local audio-device
            # problem should not fail the whole assistant reply.
            logger.exception("Local Windows playback failed")
            await update.message.reply_text(
                f"Голосовое отправлено в Telegram, но автопроигрывание на ПК не сработало: "
                f"{type(playback_exc).__name__}: {playback_exc}"
            )

    except grpc.RpcError as exc:
        code = exc.code().name if exc.code() else "RPC_ERROR"
        detail = (exc.details() or "без описания")[:700]
        logger.warning("NVIDIA gRPC error: %s: %s", code, detail)

        error_text = f"Ошибка NVIDIA: {code}: {detail}"
        if answer_published:
            # The useful written answer is already visible. Never replace it
            # because a later ASR/TTS/audio/network stage failed.
            await update.message.reply_text(error_text)
        else:
            await status.edit_text(error_text)

    except Exception as exc:
        logger.exception("Assistant processing failed")
        error_text = f"Ошибка озвучки/отправки: {type(exc).__name__}: {exc}"

        if answer_published:
            # Preserve the final answer. Report post-answer failures separately.
            await update.message.reply_text(error_text)
        else:
            await status.edit_text(
                f"Ошибка: {type(exc).__name__}: {exc}"
            )


async def echo_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    await process_user_text(
        update,
        update.message.text,
    )


async def memory_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not update.message or not update.effective_user:
        return

    user_id = update.effective_user.id

    try:
        summary = await asyncio.to_thread(
            memory_store.get_long_term_summary,
            user_id,
        )
        active_session = await asyncio.to_thread(
            memory_store.get_active_session,
            user_id,
        )
        session_count = await asyncio.to_thread(
            memory_store.current_session_count,
            user_id,
        )
        total_count = await asyncio.to_thread(
            memory_store.total_message_count,
            user_id,
        )
    except Exception as exc:
        logger.exception("Memory status failed")
        await update.message.reply_text(
            f"Ошибка чтения памяти: {type(exc).__name__}: {exc}"
        )
        return

    shown_summary = summary or "(долгосрочная память пока пустая)"
    if len(shown_summary) > 3000:
        shown_summary = shown_summary[:3000] + "…"

    await update.message.reply_text(
        "Память бота:\n"
        f"Сессия: #{active_session}\n"
        f"Сообщений в текущем диалоге: {session_count}\n"
        f"Всего сохранённых сообщений: {total_count}\n\n"
        f"Долгосрочная память:\n{shown_summary}"
    )


async def forget_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not update.message or not update.effective_user:
        return

    user_id = update.effective_user.id
    await asyncio.to_thread(
        memory_store.forget_user,
        user_id,
    )

    # Also clear volatile per-user state.
    user_output_mode.pop(user_id, None)
    user_tts_exaggeration.pop(user_id, None)

    await update.message.reply_text(
        "Память этого Telegram-пользователя очищена. "
        "Начат новый чистый диалог."
    )


async def voice_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Telegram voice -> NVIDIA Parakeet ru-RU ASR -> Nemotron -> TTS/output mode.
    """
    if not update.message or not update.message.voice:
        return

    status = await update.message.reply_text("Распознаю голос…")

    try:
        voice = update.message.voice

        with tempfile.TemporaryDirectory(prefix="telegram_asr_") as tmp:
            voice_path = Path(tmp) / "voice.ogg"

            telegram_file = await context.bot.get_file(voice.file_id)
            await telegram_file.download_to_drive(
                custom_path=str(voice_path)
            )

            transcript = await asyncio.to_thread(
                transcribe_voice_file,
                voice_path,
            )

        await status.edit_text(
            f"Распознано: {transcript}\\n\\nДумаю…"
        )

        await process_user_text(
            update,
            transcript,
            status=status,
        )

    except grpc.RpcError as exc:
        code = exc.code().name if exc.code() else "RPC_ERROR"
        detail = (exc.details() or "без описания")[:700]
        logger.warning("ASR gRPC error: %s: %s", code, detail)
        await status.edit_text(
            f"Ошибка распознавания NVIDIA ASR: {code}: {detail}"
        )
    except Exception as exc:
        logger.exception("Voice recognition failed")
        await status.edit_text(
            f"Ошибка распознавания голоса: {type(exc).__name__}: {exc}"
        )


