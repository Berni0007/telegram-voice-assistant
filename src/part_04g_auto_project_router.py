# Automatic project selection with a small remote classifier call.
# No local ML model is loaded on the host.

import json

AUTO_PROJECT_ENABLED = os.getenv("AUTO_PROJECT_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
AUTO_PROJECT_MODEL = os.getenv("AUTO_PROJECT_MODEL", LLM_MODEL).strip() or LLM_MODEL
AUTO_PROJECT_MIN_CONFIDENCE = min(0.99, max(0.50, float(os.getenv("AUTO_PROJECT_MIN_CONFIDENCE", "0.84"))))
AUTO_PROJECT_GENERAL_CONFIDENCE = min(0.99, max(AUTO_PROJECT_MIN_CONFIDENCE, float(os.getenv("AUTO_PROJECT_GENERAL_CONFIDENCE", "0.94"))))


def _project_router_json(text: str) -> dict:
    cleaned = clean_llm_answer(text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        value = json.loads(cleaned[start:end + 1])
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def auto_route_project(user_id: int, user_text: str) -> dict:
    """Select a project only when confidence is high; otherwise keep current state."""
    current = project_get_active(user_id)
    result = {
        "changed": False,
        "project": current,
        "confidence": 0.0,
        "decision": current["name"] if current else "GENERAL",
    }
    if not AUTO_PROJECT_ENABLED:
        return result

    raw = re.sub(r"\s+", " ", (user_text or "")).strip()
    if len(raw) < 4:
        return result

    # Explicit project-control phrases are handled by maybe_handle_project_text().
    low = raw.casefold()
    if low.startswith("/project") or re.match(r"(?i)^(?:проект\s*[:=-]|переключись на|перейди в|открой проект|создай проект)", raw):
        return result

    projects = project_list(user_id)
    if not projects:
        return result

    # Fast deterministic route when a unique project name is directly mentioned.
    direct_matches = []
    for item in projects:
        name = str(item.get("name") or "").strip()
        key = name.casefold()
        if len(key) >= 3 and re.search(r"(?<!\w)" + re.escape(key) + r"(?!\w)", low):
            direct_matches.append(item)
    if len(direct_matches) == 1:
        chosen = direct_matches[0]
        if not current or int(current["id"]) != int(chosen["id"]):
            project_switch(user_id, chosen["name"])
            chosen = project_get_active(user_id)
            result.update({"changed": True, "project": chosen, "confidence": 1.0, "decision": chosen["name"]})
        else:
            result.update({"project": current, "confidence": 1.0, "decision": current["name"]})
        return result

    # One project only: don't burn an API call just to rediscover the same choice.
    if len(projects) == 1:
        return result

    candidate_lines = []
    for item in projects[:20]:
        summary = re.sub(r"\s+", " ", str(item.get("summary") or "")).strip()
        if len(summary) > 650:
            summary = summary[:650] + "…"
        candidate_lines.append(
            f"- {item['name']}: {summary or '(резюме пока пустое)'}"
        )

    current_name = current["name"] if current else "GENERAL"
    prompt = (
        "Определи, к какому из проектов относится новое сообщение пользователя. "
        "Верни ТОЛЬКО JSON: {\"project\":\"точное имя проекта или GENERAL\",\"confidence\":0.0}. "
        "GENERAL выбирай, если сообщение явно не относится ни к одному проекту. "
        "Не переключай проект из-за слабой тематической связи или случайного слова.\n\n"
        f"Сейчас активен: {current_name}\n"
        "Проекты:\n" + "\n".join(candidate_lines) + "\n\n"
        f"Новое сообщение:\n{raw[:1800]}"
    )

    try:
        response = llm_client.chat.completions.create(
            model=AUTO_PROJECT_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": "Ты короткий классификатор проекта. Никаких объяснений, только JSON.",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_tokens=80,
            stream=False,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        parsed = _project_router_json(response.choices[0].message.content or "")
    except Exception:
        logger.exception("Automatic project routing failed")
        return result

    decision = str(parsed.get("project") or "").strip()
    try:
        confidence = float(parsed.get("confidence", 0.0))
    except Exception:
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    if decision.casefold() == "general":
        # Exiting a project automatically is intentionally harder than switching
        # between known projects, to avoid losing context on an ambiguous follow-up.
        if current and confidence >= AUTO_PROJECT_GENERAL_CONFIDENCE:
            project_switch(user_id, None)
            result.update({"changed": True, "project": None, "confidence": confidence, "decision": "GENERAL"})
        else:
            result.update({"confidence": confidence, "decision": "GENERAL"})
        return result

    chosen = next((p for p in projects if str(p["name"]).casefold() == decision.casefold()), None)
    if chosen is None or confidence < AUTO_PROJECT_MIN_CONFIDENCE:
        result.update({"confidence": confidence, "decision": decision or current_name})
        return result

    if current and int(current["id"]) == int(chosen["id"]):
        result.update({"project": current, "confidence": confidence, "decision": current["name"]})
        return result

    project_switch(user_id, chosen["name"])
    selected = project_get_active(user_id)
    result.update({"changed": True, "project": selected, "confidence": confidence, "decision": selected["name"] if selected else chosen["name"]})
    logger.info("Auto project switch: user=%s project=%s confidence=%.2f", user_id, result["decision"], confidence)
    return result
