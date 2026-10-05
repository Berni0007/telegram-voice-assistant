# v5.3.1: speed up long voice replies by synthesizing independent TTS chunks
# in parallel, then joining them in the original order.
#
# Keep concurrency conservative for NVIDIA's hosted endpoint. Increase
# TTS_PARALLEL_CHUNKS only if the host/account tolerates more simultaneous calls.
TTS_PARALLEL_CHUNKS = int(os.getenv("TTS_PARALLEL_CHUNKS", "2"))
TTS_PARALLEL_CHUNKS = max(1, min(4, TTS_PARALLEL_CHUNKS))


def synthesize_voice(
    text: str,
    voice_name: str | None = None,
    exaggeration_factor: float = 0.50,
) -> tuple[bytes, bytes, float]:
    """Synthesize a reply, parallelizing long multi-chunk TTS generation."""
    chunks = split_tts_text(text)
    if not chunks:
        raise RuntimeError("Nothing to synthesize.")

    workers = min(TTS_PARALLEL_CHUNKS, len(chunks))
    logger.info(
        "TTS reply split into %d chunk(s): total_chars=%d max_chunk=%d voice=%s workers=%d",
        len(chunks),
        len(text),
        max(len(c) for c in chunks),
        voice_name or "<auto>",
        workers,
    )

    if workers <= 1:
        pcm_chunks = [
            synthesize_pcm_chunk(
                chunk,
                idx,
                len(chunks),
                voice_name=voice_name,
                exaggeration_factor=exaggeration_factor,
            )
            for idx, chunk in enumerate(chunks, start=1)
        ]
    else:
        from concurrent.futures import ThreadPoolExecutor

        # Futures are stored in source order. Calls run concurrently, but we
        # collect them in that same order so speech is never rearranged.
        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="tts",
        ) as pool:
            futures = [
                pool.submit(
                    synthesize_pcm_chunk,
                    chunk,
                    idx,
                    len(chunks),
                    voice_name=voice_name,
                    exaggeration_factor=exaggeration_factor,
                )
                for idx, chunk in enumerate(chunks, start=1)
            ]
            pcm_chunks = [future.result() for future in futures]

    inter_chunk_silence = b"\x00\x00" * int(TTS_SAMPLE_RATE * 0.12)
    pcm_parts: list[bytes] = []
    for pcm in pcm_chunks:
        if pcm_parts:
            pcm_parts.append(inter_chunk_silence)
        pcm_parts.append(pcm)

    combined_pcm = b"".join(pcm_parts)
    if len(combined_pcm) < 100:
        raise RuntimeError("Combined NVIDIA TTS PCM is empty.")

    wav_bytes = pcm16_to_wav(combined_pcm, TTS_SAMPLE_RATE)
    ogg_bytes, duration = wav_to_ogg_opus(wav_bytes)

    logger.info(
        "TTS complete: chunks=%d workers=%d PCM=%d WAV=%d OGG=%d duration=%.2fs",
        len(chunks),
        workers,
        len(combined_pcm),
        len(wav_bytes),
        len(ogg_bytes),
        duration,
    )
    return ogg_bytes, wav_bytes, duration
