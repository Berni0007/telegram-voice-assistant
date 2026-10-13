# Natural-language management for condition watches.
# Keeps slash commands as a fallback, but normal conversation is the primary UI.

_WATCH_LIST_PATTERNS = (
    r"(?i)^\s*(?:покажи\s+)?(?:мои\s+)?(?:наблюдения|мониторинги)\s*[?.!]*$",
    r"(?i)^\s*(?:за\s+чем|что)\s+(?:ты\s+)?(?:следишь|отслеживаешь|мониторишь)\s*[?.!]*$",
    r"(?i)^\s*что\s+(?:ты\s+)?сейчас\s+(?:отслеживаешь|мониторишь)\s*[?.!]*$",
    r"(?i)^\s*покажи\s+(?:за\s+чем|что)\s+(?:ты\s+)?следишь\s*[?.!]*$",
)

_WATCH_DELETE_ALL_PATTERNS = (
    r"(?i)^\s*(?:перестань|прекрати)\s+(?:следить|отслеживать|мониторить)\s+(?:за\s+)?(?:всем|всего|всё|все)\s*[?.!]*$",
    r"(?i)^\s*(?:удали|отмени|выключи)\s+(?:все|всё)\s+(?:наблюдения|мониторинги)\s*[?.!]*$",
)

_WATCH_DELETE_PATTERNS = (
    r"(?i)^\s*(?:перестань|прекрати)\s+(?:следить|отслеживать|мониторить)(?:\s+за)?\s*(.*)$",
    r"(?i)^\s*(?:больше\s+)?не\s+(?:следи|отслеживай|мониторь)(?:\s+за)?\s*(.*)$",
    r"(?i)^\s*(?:удали|отмени|выключи)\s+(?:наблюдение|мониторинг)(?:\s+(?:за|про|по))?\s*(.*)$",
)

_WATCH_STOPWORDS = {
    "за", "по", "про", "о", "об", "на", "новые", "новой", "новых", "новости",
    "новостями", "обновления", "обновлениями", "следить", "отслеживать", "мониторить",
    "наблюдение", "мониторинг", "мне", "пожалуйста", "это", "этим", "этой", "этого",
}


def _watch_normalize(value: str) -> str:
    return re.sub(r"[^\w\-]+", " ", (value or "").casefold(), flags=re.UNICODE).strip()


def _watch_tokens(value: str) -> set[str]:
    return {
        token for token in _watch_normalize(value).split()
        if len(token) >= 3 and token not in _WATCH_STOPWORDS
    }


def _watch_format_rows(rows: list[dict]) -> str:
    if not rows:
        return "Сейчас я ни за чем не слежу."
    lines = ["Сейчас слежу за:"]
    for item in rows[:30]:
        hours = int(item["interval_minutes"]) / 60
        interval_text = f"раз в {int(hours)} ч" if hours.is_integer() else f"раз в {item['interval_minutes']} мин"
        lines.append(f"• {item['query']} — {interval_text}")
    return "\n".join(lines)


def _watch_find_by_target(rows: list[dict], target: str) -> tuple[dict | None, list[dict]]:
    target_clean = _watch_normalize(target)
    if not target_clean:
        return (rows[0], []) if len(rows) == 1 else (None, rows)

    # Natural numeric reference: "удали наблюдение 3" / "убери #3".
    id_match = re.fullmatch(r"#?(\d+)", target_clean)
    if id_match:
        wanted = int(id_match.group(1))
        found = next((row for row in rows if int(row["id"]) == wanted), None)
        return found, []

    target_tokens = _watch_tokens(target_clean)
    ranked: list[tuple[float, dict]] = []
    for row in rows:
        query = str(row.get("query") or "")
        query_clean = _watch_normalize(query)
        if target_clean and (target_clean in query_clean or query_clean in target_clean):
            score = 1.0
        else:
            query_tokens = _watch_tokens(query_clean)
            if not target_tokens or not query_tokens:
                score = 0.0
            else:
                overlap = len(target_tokens & query_tokens)
                score = overlap / max(1, len(target_tokens))
                # Prefer a tighter match when several watches share generic words.
                score -= max(0, len(query_tokens - target_tokens) - 3) * 0.02
        ranked.append((score, row))

    ranked.sort(key=lambda pair: pair[0], reverse=True)
    if not ranked or ranked[0][0] < 0.55:
        return None, []

    best_score, best = ranked[0]
    if len(ranked) > 1 and ranked[1][0] >= 0.55 and best_score - ranked[1][0] < 0.20:
        return None, [row for score, row in ranked[:4] if score >= 0.55]
    return best, []


def _watch_remove_job(context: ContextTypes.DEFAULT_TYPE, watch_id: int) -> None:
    if context.job_queue:
        for job in context.job_queue.get_jobs_by_name(f"watch:{watch_id}"):
            job.schedule_removal()


async def maybe_handle_watch_management_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
) -> bool:
    if not update.message or not update.effective_user:
        return False

    raw = re.sub(r"\s+", " ", (text or "")).strip()
    if not raw:
        return False

    user_id = update.effective_user.id

    if any(re.fullmatch(pattern, raw) for pattern in _WATCH_LIST_PATTERNS):
        rows = await asyncio.to_thread(watch_list, user_id)
        await update.message.reply_text(_watch_format_rows(rows))
        return True

    if any(re.fullmatch(pattern, raw) for pattern in _WATCH_DELETE_ALL_PATTERNS):
        rows = await asyncio.to_thread(watch_list, user_id)
        if not rows:
            await update.message.reply_text("Активных наблюдений и так нет.")
            return True
        removed = 0
        for row in rows:
            watch_id = int(row["id"])
            if await asyncio.to_thread(watch_disable, watch_id, user_id):
                _watch_remove_job(context, watch_id)
                removed += 1
        await update.message.reply_text(f"Отключил все наблюдения: {removed}.")
        return True

    target = None
    for pattern in _WATCH_DELETE_PATTERNS:
        match = re.fullmatch(pattern, raw)
        if match:
            target = (match.group(1) or "").strip(" ,.-")
            break
    if target is None:
        return False

    rows = await asyncio.to_thread(watch_list, user_id)
    if not rows:
        await update.message.reply_text("Активных наблюдений нет.")
        return True

    selected, ambiguous = _watch_find_by_target(rows, target)
    if selected is None:
        if ambiguous:
            choices = "\n".join(f"• {item['query']}" for item in ambiguous)
            await update.message.reply_text(
                "Есть несколько похожих наблюдений. Назови тему точнее:\n" + choices
            )
        else:
            await update.message.reply_text(
                "Не нашёл подходящего наблюдения. Можешь сказать: «за чем ты следишь?»"
            )
        return True

    watch_id = int(selected["id"])
    if await asyncio.to_thread(watch_disable, watch_id, user_id):
        _watch_remove_job(context, watch_id)
        await update.message.reply_text(f"Перестал следить за: {selected['query']}")
    else:
        await update.message.reply_text("Это наблюдение уже выключено.")
    return True
