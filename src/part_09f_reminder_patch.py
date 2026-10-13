# Reminder integration loaded after the normal Telegram handlers.

_original_echo_text = echo_text
_original_voice_message = voice_message


async def echo_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    if await maybe_handle_reminder_text(update, context, update.message.text):
        return

    await _original_echo_text(update, context)


async def voice_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Voice -> ASR; reminder phrases are scheduled before normal LLM handling."""
    if not update.message or not update.message.voice:
        return

    status = await update.message.reply_text("Распознаю голос…")

    try:
        voice = update.message.voice

        with tempfile.TemporaryDirectory(prefix="telegram_asr_") as tmp:
            voice_path = Path(tmp) / "voice.ogg"

            telegram_file = await context.bot.get_file(voice.file_id)
            await telegram_file.download_to_drive(custom_path=str(voice_path))

            transcript = await asyncio.to_thread(
                transcribe_voice_file,
                voice_path,
            )

        # For a reminder request, remove the temporary ASR status and return
        # the normal reminder confirmation instead of sending it to the LLM.
        if parse_reminder_request(transcript):
            try:
                await status.delete()
            except Exception:
                pass
            if await maybe_handle_reminder_text(update, context, transcript):
                return

        await status.edit_text(
            f"Распознано: {transcript}\n\nДумаю…"
        )
        await process_user_text(update, transcript, status=status)

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
