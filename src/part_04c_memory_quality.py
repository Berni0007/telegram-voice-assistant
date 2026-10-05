# Memory-quality guards for v5.2.1.
# These run after the semantic-memory layer and repair obvious category mistakes
# without adding another LLM call to normal replies.


def _normalize_memory_kind_v521(kind: str, content: str) -> str:
    kind = (kind or "").strip()
    low = _normalize_memory_text(content)

    # Identity / biographical facts must never become conversation style.
    fact_patterns = (
        r"\bпользовател(?:я|ь|ю)?\s+(?:зовут|называют)\b",
        r"\bимя пользователя\b",
        r"\bпользователь\s+(?:жив[её]т|работает|родился|родилась|использует|владеет)\b",
        r"\bу пользователя\s+(?:есть|установлен|установлена|компьютер|сервер|телефон|машина)\b",
    )
    if any(re.search(pattern, low) for pattern in fact_patterns):
        return "fact"

    # How the user addresses the assistant is a preference, not answer style.
    assistant_name_markers = (
        "называет меня ",
        "называет ассистента ",
        "предпочитает называть меня ",
        "предпочитает называть ассистента ",
        "обращается ко мне как ",
        "обращается к ассистенту как ",
    )
    if any(marker in low for marker in assistant_name_markers):
        return "preference"

    # Obvious long-running work should not be stored as a mere preference.
    project_markers = (
        "пользователь разрабатывает ",
        "пользователь делает проект ",
        "пользователь работает над ",
        "проект пользователя ",
    )
    if any(marker in low for marker in project_markers):
        return "project"

    return kind


def _repair_memory_item_kinds_v521(user_id: int) -> int:
    """Repair obvious misclassified active items already stored in SQLite."""
    repaired = 0
    items = memory_list_items(user_id, status="active", kinds=None, limit=MEMORY_MAX_ITEMS)
    now = memory_store._now()

    with memory_store._lock, memory_store._connect() as conn:
        for item in items:
            old_kind = str(item.get("kind", ""))
            new_kind = _normalize_memory_kind_v521(old_kind, item.get("content", ""))
            if new_kind != old_kind and new_kind in MEMORY_KINDS:
                conn.execute(
                    "UPDATE memory_items SET kind=?, updated_at=? WHERE id=?",
                    (new_kind, now, int(item["id"])),
                )
                repaired += 1

    if repaired:
        logger.info("Memory category repair: user=%s repaired=%d", user_id, repaired)
    return repaired


# Repair before semantic retrieval so bad old categories do not pollute the
# personality context even for the very first message after redeploy.
_retrieve_relevant_memories_v52 = retrieve_relevant_memories


def retrieve_relevant_memories(user_id: int, query: str) -> list[dict]:
    try:
        _repair_memory_item_kinds_v521(user_id)
    except Exception:
        logger.exception("Memory category repair failed before retrieval")
    return _retrieve_relevant_memories_v52(user_id, query)


_memory_get_style_items_v52 = memory_get_style_items


def memory_get_style_items(user_id: int, limit: int = 8) -> list[dict]:
    try:
        _repair_memory_item_kinds_v521(user_id)
    except Exception:
        logger.exception("Memory category repair failed before style lookup")
    return _memory_get_style_items_v52(user_id, limit)


# The extractor may still occasionally choose a wrong category. Repair the
# result immediately after every extraction pass.
_extract_structured_memory_v52 = extract_structured_memory


def extract_structured_memory(user_id: int, force: bool = False) -> None:
    _extract_structured_memory_v52(user_id, force)
    try:
        _repair_memory_item_kinds_v521(user_id)
    except Exception:
        logger.exception("Memory category repair failed after extraction")
