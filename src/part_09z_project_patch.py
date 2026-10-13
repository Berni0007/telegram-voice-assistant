# Loaded after all message-processing overrides.
# Intercepts project-switch phrases for both typed and ASR-transcribed messages,
# while preserving reminders and preventing project facts from entering general memory.

_project_original_process_user_text = process_user_text
_project_original_echo_text = echo_text
_project_original_schedule_memory_refresh = globals().get("_schedule_memory_refresh")


async def process_user_text(update: Update, text: str, status=None) -> None:
    return await _project_original_process_user_text(update, text, status=status)


async def echo_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    if await maybe_handle_project_text(update, context, update.message.text):
        return
    # Preserve the existing reminder-aware text handler.
    await _project_original_echo_text(update, context)


async def voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Voice -> ASR -> project/reminder routing -> normal assistant."""
    if not update.message or not update.message.voice:
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

        await status.edit_text(f"Распознано: {transcript}\n\nДумаю…")
        await _project_original_process_user_text(update, transcript, status=status)
    except grpc.RpcError as exc:
        code = exc.code().name if exc.code() else "RPC_ERROR"
        detail = (exc.details() or "без описания")[:700]
        logger.warning("ASR gRPC error: %s: %s", code, detail)
        await status.edit_text(f"Ошибка распознавания NVIDIA ASR: {code}: {detail}")
    except Exception as exc:
        logger.exception("Voice recognition failed")
        await status.edit_text(f"Ошибка распознавания голоса: {type(exc).__name__}: {exc}")


if _project_original_schedule_memory_refresh is not None:
    def _schedule_memory_refresh(user_id: int) -> None:
        # Project turns are intentionally excluded from the general structured
        # memory so facts from one project cannot contaminate another.
        if project_get_active(user_id):
            return
        return _project_original_schedule_memory_refresh(user_id)
