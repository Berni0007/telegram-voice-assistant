# Global speech pace adjustment applied after NVIDIA synthesis.
# Values below 1.0 slow speech down while preserving pitch via ffmpeg atempo.
TTS_PLAYBACK_SPEED = float(os.getenv("TTS_PLAYBACK_SPEED", "0.90"))
TTS_PLAYBACK_SPEED = max(0.75, min(1.25, TTS_PLAYBACK_SPEED))


def wav_to_ogg_opus(wav_bytes: bytes) -> tuple[bytes, float]:
    """Convert WAV to Telegram OGG/Opus and apply a natural speech pace."""
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
                "-filter:a", f"atempo={TTS_PLAYBACK_SPEED:.3f}",
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
        if duration < 0.05:
            raise RuntimeError(
                f"Generated OGG/Opus is effectively empty: {duration:.2f} sec."
            )

        try:
            Path(__file__).with_name("last_tts.ogg").write_bytes(ogg)
        except Exception:
            pass

        logger.info(
            "Audio prepared with pace %.2f: WAV=%d bytes, OGG=%d bytes, duration=%.2fs",
            TTS_PLAYBACK_SPEED,
            len(wav_bytes),
            len(ogg),
            duration,
        )
        return ogg, duration
