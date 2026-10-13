# Lightweight image/screenshot analysis through NVIDIA API.
# Images are kept only in memory for the duration of the request; no local vision model.

import base64

VISION_ENABLED = os.getenv("VISION_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
VISION_MODEL = os.getenv("VISION_MODEL", "google/gemma-3n-e4b-it").strip() or "google/gemma-3n-e4b-it"
VISION_MAX_BYTES = max(256_000, int(os.getenv("VISION_MAX_BYTES", "3500000")))
VISION_MAX_TOKENS = max(300, int(os.getenv("VISION_MAX_TOKENS", "1100")))


def _vision_project_context(user_id: int) -> str:
    """Return a compact active-project hint if project memory is available."""
    try:
        active = project_get_active(user_id)
    except Exception:
        active = None
    if not active:
        return ""
    summary = re.sub(r"\s+", " ", str(active.get("summary") or "")).strip()
    if len(summary) > 1800:
        summary = summary[:1800] + "…"
    return (
        f"\nАктивный проект пользователя: {active['name']}. "
        "Учитывай его только если он действительно связан с изображением."
        + (f"\nКраткий контекст проекта: {summary}" if summary else "")
    )


def analyze_image_bytes(user_id: int, image_bytes: bytes, user_prompt: str = "", mime_type: str = "image/jpeg") -> str:
    if not VISION_ENABLED:
        raise RuntimeError("Анализ изображений отключён.")
    if not image_bytes:
        raise ValueError("Пустое изображение.")
    if len(image_bytes) > VISION_MAX_BYTES:
        raise ValueError(
            f"Изображение слишком большое ({len(image_bytes) / 1024 / 1024:.1f} МБ). "
            f"Лимит: {VISION_MAX_BYTES / 1024 / 1024:.1f} МБ."
        )

    mime = (mime_type or "image/jpeg").lower()
    if mime not in {"image/jpeg", "image/jpg", "image/png"}:
        mime = "image/jpeg"

    encoded = base64.b64encode(image_bytes).decode("ascii")
    data_url = f"data:{mime};base64,{encoded}"
    question = re.sub(r"\s+", " ", (user_prompt or "")).strip()
    if not question:
        question = "Разбери это изображение или скриншот и объясни, что на нём важно."

    instruction = (
        "Ты анализируешь изображение для русскоязычного пользователя. "
        "Внимательно читай видимый текст, сообщения об ошибках, названия элементов интерфейса и значения. "
        "Если это ошибка или настройка программы — сначала назови, что именно видно, затем вероятную причину и конкретные действия. "
        "Если часть текста неразборчива — прямо скажи об этом, не придумывай. "
        "Не описывай очевидные декоративные детали без пользы. Ответ должен быть практичным и по существу."
        + _vision_project_context(user_id)
        + f"\n\nЗапрос пользователя: {question}"
    )

    response = llm_client.chat.completions.create(
        model=VISION_MODEL,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": instruction},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
        temperature=0.25,
        max_tokens=VISION_MAX_TOKENS,
        stream=False,
    )
    answer = clean_llm_answer(response.choices[0].message.content or "").strip()
    if not answer:
        raise RuntimeError("Vision model returned an empty response.")
    return answer
