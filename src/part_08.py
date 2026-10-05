async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not update.effective_user:
        return

    await query.answer()
    data = query.data or ""
    user_id = update.effective_user.id

    if data.startswith("voicestyle:"):
        style_key = data.split(":", 1)[1]
        if style_key not in VOICE_STYLES:
            await query.message.reply_text("Неизвестный профиль голоса.")
            return

        label, exaggeration = VOICE_STYLES[style_key]
        user_tts_exaggeration[user_id] = exaggeration
        selected_voice = get_tts_voice(user_id)

        await query.edit_message_text(
            f"Выбран стиль: {label}\n"
            f"Русский диктор: {selected_voice}\n"
            f"Emotion exaggeration: {exaggeration:.2f}\n\n"
            "Генерирую тестовую фразу…"
        )

        try:
            ogg_bytes, wav_bytes, duration = await asyncio.to_thread(
                synthesize_voice,
                VOICE_TEST_TEXT,
                selected_voice,
                exaggeration,
            )

            await query.message.reply_voice(
                voice=InputFile(ogg_bytes, filename="voice_test.ogg"),
                duration=max(1, int(duration + 0.999)),
                caption=f"Тест: {label}",
            )

            if get_output_mode(user_id) == "pc_auto":
                try:
                    await asyncio.to_thread(play_wav_on_windows, wav_bytes)
                except Exception:
                    logger.exception("Voice style preview local playback failed")

        except grpc.RpcError as exc:
            code = exc.code().name if exc.code() else "RPC_ERROR"
            detail = (exc.details() or "без описания")[:500]
            await query.message.reply_text(
                f"Не удалось протестировать стиль: {code}: {detail}"
            )
        except Exception as exc:
            logger.exception("Voice style preview failed")
            await query.message.reply_text(
                f"Не удалось протестировать стиль: {type(exc).__name__}: {exc}"
            )
        return

    if data == "mode:pc_auto":
        user_output_mode[user_id] = "pc_auto"
        await query.edit_message_text(
            "Режим включён: 🔊 Авто + Telegram.\n"
            "После каждого ответа бот сразу отправляет голосовое в Telegram "
            "и одновременно проигрывает тот же ответ на Windows-компьютере."
        )
        return

    if data == "mode:manual":
        user_output_mode[user_id] = "manual"
        await query.edit_message_text(
            "Режим включён: ▶️ По кнопке.\n"
            "Сначала приходит текст. Голосовое сообщение в Telegram создаётся только "
            "после нажатия «Проиграть ответ»."
        )
        return

    if not data.startswith("play:"):
        return

    token = data.split(":", 1)[1]
    saved = pending_voice_answers.get(token)
    if not saved:
        await query.message.reply_text(
            "Этот ответ уже недоступен для озвучивания. Отправь новый вопрос."
        )
        return

    owner_id, answer = saved
    if owner_id != user_id:
        await query.message.reply_text("Эта кнопка относится к другому пользователю.")
        return

    status = await query.message.reply_text("Озвучиваю…")

    try:
        selected_voice = get_tts_voice(user_id)
        exaggeration = get_tts_exaggeration(user_id)
        spoken_answer = prepare_speech_text(answer)
        ogg_bytes, wav_bytes, duration = await asyncio.to_thread(
            synthesize_voice,
            spoken_answer,
            selected_voice,
            exaggeration,
        )

        await query.message.reply_voice(
            voice=InputFile(ogg_bytes, filename="speech.ogg"),
            duration=max(1, int(duration + 0.999)),
        )
        await status.delete()

    except grpc.RpcError as exc:
        code = exc.code().name if exc.code() else "RPC_ERROR"
        detail = (exc.details() or "без описания")[:500]
        logger.warning("Manual TTS gRPC error: %s: %s", code, detail)
        await status.edit_text(
            f"Ошибка NVIDIA TTS: {code}: {detail}"
        )
    except Exception as exc:
        logger.exception("Manual TTS generation failed")
        await status.edit_text(
            f"Ошибка TTS: {type(exc).__name__}: {exc}"
        )


async def new_dialog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return

    user_id = update.effective_user.id
    status = await update.message.reply_text(
        "Сохраняю важный контекст и начинаю новый диалог…"
    )

    try:
        await asyncio.to_thread(
            build_memory_summary,
            user_id,
            True,
        )
    except Exception:
        logger.exception("Memory summary on /new failed")

    session_id = memory_store.new_session(user_id)

    await status.edit_text(
        f"Новый диалог начат. Долгосрочная память сохранена. "
        f"Сессия #{session_id}."
    )


