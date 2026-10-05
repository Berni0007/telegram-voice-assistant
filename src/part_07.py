def synthesize_voice(
    text: str,
    voice_name: str | None = None,
    exaggeration_factor: float = 0.50,
) -> tuple[bytes, bytes, float]:
    """
    Synthesize long replies safely:
      text -> natural chunks <= TTS_CHUNK_MAX_CHARS
           -> multiple NVIDIA LINEAR_PCM calls
           -> concatenate PCM in original order
           -> one WAV
           -> one finalized OGG/Opus
           -> one Telegram voice message
    """
    chunks = split_tts_text(text)
    if not chunks:
        raise RuntimeError("Nothing to synthesize.")

    logger.info(
        "TTS reply split into %d chunk(s): total_chars=%d max_chunk=%d voice=%s",
        len(chunks),
        len(text),
        max(len(c) for c in chunks),
        voice_name or "<auto>",
    )

    pcm_parts: list[bytes] = []
    inter_chunk_silence = (
        b"\x00\x00" * int(TTS_SAMPLE_RATE * 0.12)
    )

    for idx, chunk in enumerate(chunks, start=1):
        if pcm_parts:
            pcm_parts.append(inter_chunk_silence)
        pcm_parts.append(
            synthesize_pcm_chunk(
                chunk,
                idx,
                len(chunks),
                voice_name=voice_name,
                exaggeration_factor=exaggeration_factor,
            )
        )

    # All chunks use the same encoding/sample rate/mono layout.
    combined_pcm = b"".join(pcm_parts)

    if len(combined_pcm) < 100:
        raise RuntimeError("Combined NVIDIA TTS PCM is empty.")

    wav_bytes = pcm16_to_wav(combined_pcm, TTS_SAMPLE_RATE)
    ogg_bytes, duration = wav_to_ogg_opus(wav_bytes)

    logger.info(
        "TTS complete: chunks=%d PCM=%d WAV=%d OGG=%d duration=%.2fs",
        len(chunks),
        len(combined_pcm),
        len(wav_bytes),
        len(ogg_bytes),
        duration,
    )

    return ogg_bytes, wav_bytes, duration


def list_tts_voices(language_code: str | None = None) -> list[str]:
    config_response = tts_service.stub.GetRivaSynthesisConfig(
        riva.client.proto.riva_tts_pb2.RivaSynthesisConfigRequest()
    )

    voices: list[str] = []
    for model_config in config_response.model_config:
        params = model_config.parameters
        language = params.get("language_code", "")
        if language_code and language.lower() != language_code.lower():
            continue

        base_name = params.get("voice_name", "")
        subvoices_raw = params.get("subvoices", "")
        subvoices = [
            item.split(":", 1)[0].strip()
            for item in subvoices_raw.split(",")
            if item.strip()
        ]

        if base_name and subvoices:
            voices.extend(f"{base_name}.{subvoice}" for subvoice in subvoices)
        elif base_name:
            voices.append(base_name)

    return sorted(set(voices))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    user_id = update.effective_user.id if update.effective_user else 0
    mode = get_output_mode(user_id)
    mode_name = "авто + Telegram" if mode == "pc_auto" else "по кнопке"

    await update.message.reply_text(
        "Готов. Помню контекст разговора, подстраиваю стиль ответа и могу озвучивать его двумя способами.\n"
        f"Текущий режим: {mode_name}.\n"
        "🔊 Авто + Telegram — голос сразу проигрывается на Windows-ПК И одновременно приходит голосовым сообщением в Telegram.\n"
        "▶️ По кнопке — сначала текст, потом кнопка для получения голосового в Telegram.\n"
        "/mode — выбрать режим ответа.\n"
        "/voice — настроить и протестировать стиль русского голоса.\n"
        "/new — начать новый диалог, сохранив важное в долгую память.\n"
        "/memory — показать состояние памяти.\n"
        "/forget — полностью очистить мою память для тебя.\n"
        "/voices — показать технический список голосов.",
        reply_markup=mode_keyboard(),
    )


async def voices(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    await update.message.reply_text("Запрашиваю доступные голоса NVIDIA…")
    try:
        available = await asyncio.to_thread(list_tts_voices, TTS_LANGUAGE)
    except grpc.RpcError as exc:
        code = exc.code().name if exc.code() else "RPC_ERROR"
        detail = (exc.details() or "без описания")[:500]
        logger.warning("Voice list gRPC error: %s: %s", code, detail)
        await update.message.reply_text(
            f"Ошибка NVIDIA TTS: {code}: {detail}"
        )
        return
    except Exception as exc:
        logger.exception("Voice list error")
        await update.message.reply_text(
            f"Не удалось получить список голосов: {type(exc).__name__}: {exc}"
        )
        return

    if not available:
        await update.message.reply_text(
            f"NVIDIA не вернула список голосов для {TTS_LANGUAGE}. "
            "Оставь TTS_VOICE пустым — Riva выберет голос автоматически."
        )
        return

    body = "\n".join(f"• {name}" for name in available)
    if len(body) > 3500:
        body = body[:3500] + "\n…"
    await update.message.reply_text(
        f"Голоса для {TTS_LANGUAGE}:\n{body}"
    )


async def voice_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return

    user_id = update.effective_user.id
    current = get_tts_exaggeration(user_id)

    rows = []
    for key, (label, value) in VOICE_STYLES.items():
        marker = "✅ " if abs(value - current) < 0.001 else ""
        rows.append(
            [
                InlineKeyboardButton(
                    f"{marker}🎙 {label} ({value:.2f})",
                    callback_data=f"voicestyle:{key}",
                )
            ]
        )

    await update.message.reply_text(
        "У NVIDIA Chatterbox для ru-RU в облаке один встроенный диктор. "
        "Поэтому здесь меняется стиль/эмоциональность речи, а не сам диктор.\n\n"
        "Для более спокойной русской подачи сначала попробуй «Спокойный», "
        "затем «Нейтральный». После выбора бот сразу пришлёт тест.",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return

    current = get_output_mode(update.effective_user.id)
    label = "🔊 Авто + Telegram" if current == "pc_auto" else "▶️ По кнопке"

    await update.message.reply_text(
        f"Текущий режим: {label}\nВыбери, как бот должен озвучивать ответы:",
        reply_markup=mode_keyboard(),
    )


