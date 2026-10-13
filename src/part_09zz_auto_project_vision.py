# Final Telegram routing layer: automatic project selection + screenshot analysis.
# Loaded after reminder/project wrappers so all existing behavior is preserved.


async def _auto_route_before_reply(user_id: int, text: str) -> None:
    try:
        await asyncio.to_thread(auto_route_project, user_id, text)
    except Exception:
        logger.exception("Auto project routing wrapper failed")


async def echo_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text or not update.effective_user:
        return

    text = update.message.text

    # Explicit controls always win over automatic classification.
    if await maybe_handle_project_text(update, context, text):
        return
    if parse_reminder_request(text):
        if await maybe_handle_reminder_text(update, context, text):
            return

    await _auto_route_before_reply(update.effective_user.id, text)
    await process_user_text(update, text)


async def voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Voice -> ASR -> explicit commands/reminders -> auto project -> assistant."""
    if not update.message or not update.message.voice or not update.effective_user:
        return

    status = await update.message.reply_text("Распознаю голос…")
    try:
        voice = update.message.voice
        with tempfile.TemporaryDirectory(prefix="telegram_asr_") as tmp:
            voice_path = Path(tmp) / "voice.ogg"
            telegram_file = await context.bot.get_file(voice.file_id)
            await telegram_file.download_to_drive(custom_path=str(voice_path))
            transcript = await asyncio.to_thread(transcribe_voice_file, voice_path)

        if await maybe_handle_project_text(update, context, transcript):
            try:
                await status.delete()
            except Exception:
                pass
            return

        if parse_reminder_request(transcript):
            try:
                await status.delete()
            except Exception:
                pass
            if await maybe_handle_reminder_text(update, context, transcript):
                return

        await _auto_route_before_reply(update.effective_user.id, transcript)
        await status.edit_text(f"Распознано: {transcript}\n\nДумаю…")
        await process_user_text(update, transcript, status=status)

    except grpc.RpcError as exc:
        code = exc.code().name if exc.code() else "RPC_ERROR"
        detail = (exc.details() or "без описания")[:700]
        logger.warning("ASR gRPC error: %s: %s", code, detail)
        await status.edit_text(f"Ошибка распознавания NVIDIA ASR: {code}: {detail}")
    except Exception as exc:
        logger.exception("Voice recognition failed")
        await status.edit_text(f"Ошибка распознавания голоса: {type(exc).__name__}: {exc}")


def _remember_image_turn(user_id: int, prompt: str, answer: str) -> None:
    user_record = "[Изображение/скриншот] " + ((prompt or "без подписи").strip())
    active = project_get_active(user_id)
    if active:
        project_add_message(user_id, "user", user_record)
        project_add_message(user_id, "assistant", answer)
        try:
            project_refresh_summary(user_id, force=False)
        except Exception:
            logger.exception("Project summary refresh after image failed")
        return

    session_id = memory_store.get_active_session(user_id)
    memory_store.add_message(user_id, "user", user_record, session_id=session_id)
    memory_store.add_message(user_id, "assistant", answer, session_id=session_id)


def _telegram_chunks(text: str, size: int = 3900) -> list[str]:
    value = (text or "").strip()
    if not value:
        return ["Не удалось сформировать ответ."]
    return [value[i:i + size] for i in range(0, len(value), size)]


async def photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Analyze Telegram photos/screenshots via remote NVIDIA vision API."""
    if not update.message or not update.message.photo or not update.effective_user:
        return

    user_id = update.effective_user.id
    photo = update.message.photo[-1]
    caption = re.sub(r"\s+", " ", (update.message.caption or "")).strip()
    prompt = caption or "Разбери этот скриншот. Что здесь происходит и что мне нужно сделать?"

    status = await update.message.reply_text("Анализирую изображение…")
    try:
        if photo.file_size and int(photo.file_size) > VISION_MAX_BYTES:
            raise ValueError(
                f"Изображение слишком большое: {photo.file_size / 1024 / 1024:.1f} МБ. "
                f"Лимит {VISION_MAX_BYTES / 1024 / 1024:.1f} МБ."
            )

        # A meaningful caption can select the right project before vision analysis.
        if caption:
            await _auto_route_before_reply(user_id, caption)

        telegram_file = await context.bot.get_file(photo.file_id)
        image_buffer = await telegram_file.download_as_bytearray()
        image_bytes = bytes(image_buffer)
        if len(image_bytes) > VISION_MAX_BYTES:
            raise ValueError(
                f"Изображение слишком большое: {len(image_bytes) / 1024 / 1024:.1f} МБ. "
                f"Лимит {VISION_MAX_BYTES / 1024 / 1024:.1f} МБ."
            )

        answer = await asyncio.to_thread(
            analyze_image_bytes,
            user_id,
            image_bytes,
            prompt,
            "image/jpeg",
        )
        # Release the encoded source as soon as the remote inference is complete.
        del image_bytes
        del image_buffer

        # With no useful caption, the visual result itself can identify the project.
        if not caption:
            await _auto_route_before_reply(user_id, "Скриншот/изображение: " + answer[:1400])

        await asyncio.to_thread(_remember_image_turn, user_id, prompt, answer)

        chunks = _telegram_chunks(answer)
        mode = get_output_mode(user_id)
        if mode == "manual":
            token = remember_manual_voice(user_id, answer)
            await status.edit_text(chunks[0])
            for chunk in chunks[1:]:
                await update.message.reply_text(chunk)
            await update.message.reply_text("▶️ Озвучить разбор", reply_markup=play_keyboard(token))
            return

        await status.edit_text(chunks[0])
        for chunk in chunks[1:]:
            await update.message.reply_text(chunk)

        # Voice output remains best-effort; the useful text is already delivered.
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
            await update.message.reply_voice(
                voice=InputFile(ogg_bytes, filename="vision_answer.ogg"),
                duration=max(1, int(duration + 0.999)),
            )
            try:
                await asyncio.to_thread(play_wav_on_windows, wav_bytes)
            except Exception:
                logger.exception("Local playback after image analysis failed")
        except Exception:
            logger.exception("TTS after image analysis failed")

    except Exception as exc:
        logger.exception("Image analysis failed")
        await status.edit_text(f"Не удалось разобрать изображение: {type(exc).__name__}: {exc}")
