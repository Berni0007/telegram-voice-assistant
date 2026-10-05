def wav_to_ogg_opus(wav_bytes: bytes) -> tuple[bytes, float]:
    """
    Convert through real files instead of stdout pipes so ffmpeg can fully
    finalize the OGG container before Telegram receives it.
    """
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()

    with tempfile.TemporaryDirectory(prefix="telegram_tts_") as tmp:
        tmpdir = Path(tmp)
        wav_path = tmpdir / "speech.wav"
        ogg_path = tmpdir / "speech.ogg"

        wav_path.write_bytes(wav_bytes)

        proc = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel", "error",
                "-y",
                "-i", str(wav_path),
                "-vn",
                "-ac", "1",
                "-ar", "48000",
                "-c:a", "libopus",
                "-b:a", "48k",
                "-vbr", "on",
                "-application", "voip",
                str(ogg_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=60,
        )

        if proc.returncode != 0:
            error = proc.stderr.decode("utf-8", errors="replace")[-1200:]
            raise RuntimeError(f"ffmpeg OGG/Opus conversion failed: {error}")

        if not ogg_path.exists():
            raise RuntimeError("ffmpeg did not create speech.ogg")

        ogg = ogg_path.read_bytes()
        if len(ogg) < 100 or not ogg.startswith(b"OggS"):
            raise RuntimeError(
                f"ffmpeg returned invalid OGG/Opus data ({len(ogg)} bytes)."
            )

        probe = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", str(ogg_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=20,
        )
        meta = probe.stderr.decode("utf-8", errors="replace")
        match = re.search(
            r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)",
            meta,
        )
        if not match:
            raise RuntimeError(
                "ffmpeg could not determine duration of generated OGG/Opus."
            )

        h, m, s = match.groups()
        duration = int(h) * 3600 + int(m) * 60 + float(s)

        # Very short replies such as "Да.", "Нет." or "Ок." can legitimately
        # produce sub-second audio. Do not reject them merely for being < 0.5s.
        # Only treat essentially empty/corrupt output as invalid.
        if duration < 0.05:
            raise RuntimeError(
                f"Generated OGG/Opus is effectively empty: {duration:.2f} sec."
            )

        debug_wav = Path(__file__).with_name("last_tts.wav")
        debug_ogg = Path(__file__).with_name("last_tts.ogg")
        debug_wav.write_bytes(wav_bytes)
        debug_ogg.write_bytes(ogg)

        logger.info(
            "Audio prepared: WAV=%d bytes, OGG=%d bytes, duration=%.2fs",
            len(wav_bytes),
            len(ogg),
            duration,
        )

        return ogg, duration


def transcribe_voice_file(path: Path) -> str:
    """
    Transcribe Telegram voice-note audio with NVIDIA Parakeet RNNT multilingual.

    Telegram voice notes are OGG/Opus. NVIDIA's official Parakeet API accepts
    WAV/OGG/OPUS containers, so no lossy conversion is needed before ASR.
    """
    audio_bytes = path.read_bytes()
    if len(audio_bytes) < 100:
        raise RuntimeError("Voice message is empty or too small.")

    config = riva.client.RecognitionConfig(
        language_code=ASR_LANGUAGE,
        max_alternatives=1,
        enable_automatic_punctuation=True,
    )

    response = asr_service.offline_recognize(audio_bytes, config)

    parts: list[str] = []
    for result in response.results:
        if result.alternatives:
            transcript = (result.alternatives[0].transcript or "").strip()
            if transcript:
                parts.append(transcript)

    transcript = " ".join(parts).strip()
    if not transcript:
        raise RuntimeError("NVIDIA ASR did not recognize speech.")

    logger.info(
        "ASR complete: chars=%d text=%r",
        len(transcript),
        transcript[:200],
    )
    return transcript


def synthesize_pcm_chunk(
    text: str,
    chunk_no: int,
    total_chunks: int,
    depth: int = 0,
    voice_name: str | None = None,
    exaggeration_factor: float = 0.50,
) -> bytes:
    """
    Synthesize one bounded chunk.

    If hosted Chatterbox still reports its ~20s speech-token limit, split that
    specific chunk again and synthesize the halves instead of failing the
    entire answer.
    """
    last_error = None

    for attempt in range(1, TTS_CHUNK_RETRIES + 1):
        try:
            logger.info(
                "TTS chunk %d/%d: chars=%d words=%d attempt=%d/%d depth=%d",
                chunk_no,
                total_chunks,
                len(text),
                len(text.split()),
                attempt,
                TTS_CHUNK_RETRIES,
                depth,
            )

            response = tts_service.synthesize(
                text=text,
                voice_name=voice_name or DEFAULT_RU_VOICE,
                language_code=TTS_LANGUAGE,
                encoding=AudioEncoding.LINEAR_PCM,
                sample_rate_hz=TTS_SAMPLE_RATE,
                custom_configuration={
                    "exaggeration_factor": f"{exaggeration_factor:.2f}",
                },
            )

            pcm = bytes(response.audio)
            if len(pcm) < 100:
                raise RuntimeError(
                    f"NVIDIA TTS returned too little PCM audio "
                    f"for chunk {chunk_no}/{total_chunks}: {len(pcm)} bytes."
                )
            return pcm

        except grpc.RpcError as exc:
            last_error = exc

            if is_tts_length_error(exc) and depth < 6 and len(text) > 20:
                smaller = [
                    p for p in split_tts_chunk_in_half(text)
                    if p.strip()
                ]
                if len(smaller) >= 2:
                    logger.warning(
                        "TTS chunk %d/%d hit speech-length limit. "
                        "Auto-splitting %d chars into %d smaller chunks.",
                        chunk_no,
                        total_chunks,
                        len(text),
                        len(smaller),
                    )

                    # 120 ms silence between emergency subchunks.
                    silence = (
                        b"\x00\x00"
                        * int(TTS_SAMPLE_RATE * 0.12)
                    )
                    sub_pcm: list[bytes] = []
                    for sub_idx, sub_text in enumerate(smaller, start=1):
                        if sub_pcm:
                            sub_pcm.append(silence)
                        sub_pcm.append(
                            synthesize_pcm_chunk(
                                sub_text,
                                sub_idx,
                                len(smaller),
                                depth + 1,
                                voice_name=voice_name,
                                exaggeration_factor=exaggeration_factor,
                            )
                        )
                    return b"".join(sub_pcm)

            if attempt >= TTS_CHUNK_RETRIES:
                raise

            import time
            delay = min(2 ** (attempt - 1), 4)
            logger.warning(
                "TTS chunk %d/%d failed (%s). Retry in %ss.",
                chunk_no,
                total_chunks,
                exc.code().name if exc.code() else "RPC_ERROR",
                delay,
            )
            time.sleep(delay)

        except Exception as exc:
            last_error = exc
            if attempt >= TTS_CHUNK_RETRIES:
                raise

            import time
            delay = min(2 ** (attempt - 1), 4)
            logger.warning(
                "TTS chunk %d/%d failed (%s). Retry in %ss.",
                chunk_no,
                total_chunks,
                type(exc).__name__,
                delay,
            )
            time.sleep(delay)

    raise RuntimeError(
        f"TTS chunk {chunk_no}/{total_chunks} failed: {last_error}"
    )


