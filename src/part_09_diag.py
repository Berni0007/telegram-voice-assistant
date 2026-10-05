def run_host_diagnostics() -> str:
    """Safe host/NVIDIA diagnostics. Never prints API keys or bot tokens."""
    import httpx

    lines: list[str] = []
    key = os.getenv("NVIDIA_API_KEY", "").strip()

    lines.append("=== HOST DIAG ===")
    lines.append(f"NVIDIA_API_KEY: {'SET' if key else 'MISSING'}")
    lines.append(f"BOT_MEMORY_DB: {os.getenv('BOT_MEMORY_DB', '(default)')}")

    # Check the actual public egress IP used by this container.
    try:
        r = httpx.get("https://api.ipify.org?format=json", timeout=12)
        lines.append(f"Public IP: {r.text[:200]}")
    except Exception as exc:
        lines.append(f"Public IP check: {type(exc).__name__}: {exc}")

    # Independent geo lookup for the same outbound path.
    try:
        r = httpx.get("https://ipapi.co/json/", timeout=12)
        if r.status_code == 200:
            data = r.json()
            lines.append(
                "Geo: "
                f"country={data.get('country_name')} ({data.get('country_code')}), "
                f"city={data.get('city')}, org={data.get('org')}"
            )
        else:
            lines.append(f"Geo check HTTP: {r.status_code}")
    except Exception as exc:
        lines.append(f"Geo check: {type(exc).__name__}: {exc}")

    if not key:
        return "\n".join(lines)

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    try:
        r = httpx.get(
            "https://integrate.api.nvidia.com/v1/models",
            headers=headers,
            timeout=20,
        )
        lines.append(f"NVIDIA /v1/models: HTTP {r.status_code}")
        if r.status_code != 200:
            lines.append("models body: " + (r.text or "(empty)")[:900])
    except Exception as exc:
        lines.append(f"NVIDIA /v1/models: {type(exc).__name__}: {exc}")

    try:
        r = httpx.post(
            "https://integrate.api.nvidia.com/v1/chat/completions",
            headers=headers,
            json={
                "model": LLM_MODEL,
                "messages": [{"role": "user", "content": "Reply only OK"}],
                "max_tokens": 4,
                "stream": False,
            },
            timeout=30,
        )
        lines.append(f"NVIDIA chat: HTTP {r.status_code}")
        if r.status_code != 200:
            lines.append("chat body: " + (r.text or "(empty)")[:1400])
        else:
            lines.append("NVIDIA chat: OK")
    except Exception as exc:
        lines.append(f"NVIDIA chat: {type(exc).__name__}: {exc}")

    result = "\n".join(lines)
    return result[:3900]


async def diag_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not update.message:
        return

    status = await update.message.reply_text("Проверяю хост и NVIDIA API…")
    try:
        report = await asyncio.to_thread(run_host_diagnostics)
        await status.edit_text(report)
    except Exception as exc:
        logger.exception("Host diagnostics failed")
        await status.edit_text(
            f"DIAG ERROR: {type(exc).__name__}: {exc}"
        )
