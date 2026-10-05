def split_tts_text(
    text: str,
    max_chars: int = TTS_CHUNK_MAX_CHARS,
    max_words: int = TTS_CHUNK_MAX_WORDS,
) -> list[str]:
    """
    Split TTS text conservatively for hosted Chatterbox.

    The service has both an input-length limit and a generated-speech-token
    limit (~20 seconds in the error returned by the hosted endpoint), so a
    character limit alone is not enough. We cap BOTH characters and words.
    """
    cleaned = re.sub(r"\s+", " ", (text or "")).strip()
    if not cleaned:
        return []

    def fits(value: str) -> bool:
        return (
            len(value) <= max_chars
            and len(value.split()) <= max_words
        )

    if fits(cleaned):
        return [cleaned]

    # Start with sentence/clause boundaries.
    units = re.split(r"(?<=[.!?…;,])\s+", cleaned)
    chunks: list[str] = []
    current = ""

    def flush_current() -> None:
        nonlocal current
        if current.strip():
            chunks.append(current.strip())
        current = ""

    def add_piece(piece: str) -> None:
        nonlocal current
        piece = piece.strip()
        if not piece:
            return

        if not fits(piece):
            # Word-based fallback.
            words = piece.split()
            word_buf: list[str] = []
            char_count = 0

            for word in words:
                extra = len(word) + (1 if word_buf else 0)
                if (
                    word_buf
                    and (
                        len(word_buf) + 1 > max_words
                        or char_count + extra > max_chars
                    )
                ):
                    candidate = " ".join(word_buf)
                    if current:
                        flush_current()
                    chunks.append(candidate)
                    word_buf = []
                    char_count = 0

                # Pathological long token/URL.
                while len(word) > max_chars:
                    if word_buf:
                        candidate = " ".join(word_buf)
                        if current:
                            flush_current()
                        chunks.append(candidate)
                        word_buf = []
                        char_count = 0
                    chunks.append(word[:max_chars])
                    word = word[max_chars:]

                if word:
                    word_buf.append(word)
                    char_count += len(word) + (1 if len(word_buf) > 1 else 0)

            if word_buf:
                candidate = " ".join(word_buf)
                if current:
                    combined = f"{current} {candidate}"
                    if fits(combined):
                        current = combined
                    else:
                        flush_current()
                        current = candidate
                else:
                    current = candidate
            return

        if not current:
            current = piece
            return

        combined = f"{current} {piece}"
        if fits(combined):
            current = combined
        else:
            flush_current()
            current = piece

    for unit in units:
        add_piece(unit)

    flush_current()

    # Final defensive guarantee.
    result: list[str] = []
    for chunk in chunks:
        if fits(chunk):
            result.append(chunk)
        else:
            words = chunk.split()
            buf = []
            for word in words:
                candidate = " ".join(buf + [word])
                if buf and not fits(candidate):
                    result.append(" ".join(buf))
                    buf = [word]
                else:
                    buf.append(word)
            if buf:
                result.append(" ".join(buf))

    return [c for c in result if c.strip()]


def split_tts_chunk_in_half(text: str) -> list[str]:
    """
    Emergency split used when Chatterbox says the generated speech itself
    would exceed its maximum speech-token budget.
    """
    words = text.split()
    if len(words) <= 1:
        midpoint = max(1, len(text) // 2)
        return [text[:midpoint].strip(), text[midpoint:].strip()]

    midpoint = max(1, len(words) // 2)
    return [
        " ".join(words[:midpoint]).strip(),
        " ".join(words[midpoint:]).strip(),
    ]


def is_tts_length_error(exc: Exception) -> bool:
    detail = ""
    if isinstance(exc, grpc.RpcError):
        try:
            detail = exc.details() or ""
        except Exception:
            detail = str(exc)
    else:
        detail = str(exc)

    low = detail.lower()
    markers = (
        "maximum allowed length",
        "max_speech_token_len",
        "output reached the maximum",
        "audio generation was truncated",
        "input text is too long",
        "speech tokens",
    )
    return any(marker in low for marker in markers)


def play_wav_on_windows(wav_bytes: bytes) -> None:
    """
    Play generated WAV on the local Windows machine.

    On Linux/server hosting there is no local Windows sound device, so the
    Telegram voice message is still sent and local playback is skipped.
    """
    if winsound is None:
        logger.info("Local Windows playback skipped: winsound is unavailable.")
        return

    if not wav_bytes:
        raise RuntimeError("No WAV audio to play.")

    with tempfile.NamedTemporaryFile(
        prefix="telegram_voice_",
        suffix=".wav",
        delete=False,
    ) as tmp:
        tmp.write(wav_bytes)
        tmp_path = Path(tmp.name)

    try:
        logger.info(
            "Local playback start: %s (%d bytes)",
            tmp_path,
            len(wav_bytes),
        )
        winsound.PlaySound(
            str(tmp_path),
            winsound.SND_FILENAME,
        )
        logger.info("Local playback finished.")
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            logger.warning(
                "Could not delete temporary WAV: %s",
                tmp_path,
            )


def pcm16_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    """Wrap mono 16-bit PCM returned by Riva into a valid WAV container."""
    if not pcm:
        raise RuntimeError("NVIDIA TTS returned empty PCM audio.")

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)  # 16-bit PCM
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)

    return buffer.getvalue()


