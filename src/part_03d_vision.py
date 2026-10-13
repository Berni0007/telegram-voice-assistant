# Lightweight image/screenshot analysis through NVIDIA API.
# Images are kept only in memory for the duration of the request; no local vision model.

import base64

VISION_ENABLED = os.getenv("VISION_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
VISION_MAX_BYTES = max(256_000, int(os.getenv("VISION_MAX_BYTES", "3500000")))
VISION_MAX_TOKENS = max(300, int(os.getenv("VISION_MAX_TOKENS", "1100")))

# NVIDIA retires hosted models periodically. Keep a small ordered fallback chain so
# screenshot analysis survives a single model reaching EOL or becoming unavailable.
_VISION_DEFAULT_MODELS = (
    "qwen/qwen3.5-122b-a10b",
    "stepfun-ai/step-3.7-flash",
    "deepseek-ai/deepseek-v4.1-flash",
)
_VISION_EOL_MODELS = {
    "google/gemma-3n-e4b-it",  # NVIDIA endpoint EOL 2026-07-27
}


def _vision_model_candidates() -> list[str]:
    candidates: list[str] = []

    # Backwards-compatible single-model override. Known retired models are skipped
    # instead of causing one guaranteed failing API call for every image.
    legacy = os.getenv("VISION_MODEL", "").strip()
    if legacy and legacy not in _VISION_EOL_MODELS:
        candidates.append(legacy)

    configured = os.getenv("VISION_MODELS", "").strip()
    if configured:
        candidates.extend(x.strip() for x in configured.split(",") if x.strip())
    else:
        candidates.extend(_VISION_DEFAULT_MODELS)

    result: list[str] = []
    seen: set[str] = set()
    for model in candidates:
        if not model or model in _VISION_EOL_MODELS or model in seen:
            continue
        seen.add(model)
        result.append(model)

    return result or list(_VISION_DEFAULT_MODELS)


# Kept for logging/backwards compatibility with the rest of the bot.
VISION_MODEL = _vision_model_candidates()[0]


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


def _vision_temperature(model: str) -> float:
    # DeepSeek's hosted reference recommends temperature=1.0; the others work well
    # with a lower value for deterministic screenshot troubleshooting.
    return 1.0 if "deepseek" in model.lower() else 0.25


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

    errors: list[str] = []
    for model in _vision_model_candidates():
        try:
            response = llm_client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": instruction},
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ],
                    }
                ],
                temperature=_vision_temperature(model),
                max_tokens=VISION_MAX_TOKENS,
                stream=False,
            )
            answer = clean_llm_answer(response.choices[0].message.content or "").strip()
            if answer:
                if model != VISION_MODEL:
                    logger.info("Vision fallback succeeded with model=%s", model)
                return answer
            errors.append(f"{model}: пустой ответ")
            logger.warning("Vision model returned empty response: model=%s", model)
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            errors.append(f"{model}: {status or type(exc).__name__}")
            logger.warning(
                "Vision model failed; trying fallback: model=%s status=%s error=%s",
                model,
                status,
                str(exc)[:500],
            )

    raise RuntimeError("Все доступные vision-модели NVIDIA не ответили: " + "; ".join(errors))
