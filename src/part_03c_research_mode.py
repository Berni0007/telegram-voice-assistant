# Deep research mode: broader multi-query search, more page reads and explicit
# source comparison. Triggered by phrases such as "разберись подробно".

RESEARCH_RESULTS = max(6, int(os.getenv("RESEARCH_RESULTS", "12")))
RESEARCH_FETCH_PAGES = max(3, int(os.getenv("RESEARCH_FETCH_PAGES", "7")))
RESEARCH_MAX_CONTEXT_CHARS = max(12000, int(os.getenv("RESEARCH_MAX_CONTEXT_CHARS", "36000")))

_RESEARCH_MARKERS = (
    "разберись подробно", "разберись глубоко", "исследуй", "глубокий поиск",
    "глубокое исследование", "подробно изучи", "изучи подробно",
    "сравни источники", "проведи исследование", "research mode",
)


def is_research_mode(user_text: str) -> bool:
    low = re.sub(r"\s+", " ", (user_text or "")).strip().lower()
    return any(marker in low for marker in _RESEARCH_MARKERS)


def _research_topic(user_text: str) -> str:
    text = re.sub(r"https?://\S+", " ", user_text or "", flags=re.IGNORECASE)
    for marker in sorted(_RESEARCH_MARKERS, key=len, reverse=True):
        text = re.sub(re.escape(marker), " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip(" ,.-:")
    return (text or _clean_search_query(user_text))[:600]


def _research_query_plan(topic: str) -> list[str]:
    """Ask the LLM for a few complementary web queries; use safe fallback if it fails."""
    fallback = [
        topic,
        f"{topic} официальный источник",
        f"{topic} последние данные обзор",
        f"{topic} сравнение мнения",
    ]
    try:
        response = llm_client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Ты планировщик веб-исследования. Верни только 4 коротких поисковых запроса, "
                        "каждый с новой строки, без нумерации и объяснений. Запросы должны дополнять друг друга: "
                        "основной, официальный/первичный источник, свежие данные, независимый анализ/сравнение."
                    ),
                },
                {"role": "user", "content": topic},
            ],
            temperature=0.25,
            max_tokens=180,
            stream=False,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        raw = clean_llm_answer(response.choices[0].message.content or "")
        queries = []
        for line in raw.splitlines():
            q = re.sub(r"^\s*[-*\d.)]+\s*", "", line).strip()
            if q and q not in queries:
                queries.append(q[:300])
        if len(queries) >= 2:
            return queries[:4]
    except Exception:
        logger.exception("Research query planning failed; using fallback queries")
    return fallback


def build_research_web_context(user_text: str) -> tuple[str, list[dict[str, str]]]:
    if not WEB_SEARCH_ENABLED:
        return "", []

    direct_urls = _extract_urls(user_text)
    topic = _research_topic(user_text)
    gathered: list[dict[str, str]] = []
    seen_urls: set[str] = set()

    if direct_urls:
        for url in direct_urls:
            if not _looks_like_private_host(url) and url not in seen_urls:
                seen_urls.add(url)
                gathered.append({"title": url, "url": url, "snippet": ""})

    for query in _research_query_plan(topic):
        try:
            results = _duckduckgo_search(query)
        except Exception:
            logger.exception("Research web search failed for query=%r", query)
            continue
        for item in results:
            url = item.get("url", "")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            copy = dict(item)
            copy["research_query"] = query
            gathered.append(copy)
            if len(gathered) >= RESEARCH_RESULTS:
                break
        if len(gathered) >= RESEARCH_RESULTS:
            break

    if not gathered:
        return "", []

    parts = [
        "РЕЖИМ ГЛУБОКОГО ИССЛЕДОВАНИЯ. Ниже материалы из нескольких поисковых направлений. "
        "Сопоставляй источники, отделяй подтверждённые факты от мнений, отмечай расхождения и устаревшие данные."
    ]

    for idx, source in enumerate(gathered, start=1):
        block = [f"[{idx}] {source.get('title') or source['url']}", f"URL: {source['url']}"]
        if source.get("research_query"):
            block.append(f"Найдено по запросу: {source['research_query']}")
        if source.get("snippet"):
            block.append(f"Фрагмент поиска: {source['snippet']}")
        parts.append("\n".join(block))

    fetched = 0
    for idx, source in enumerate(gathered, start=1):
        if fetched >= RESEARCH_FETCH_PAGES:
            break
        try:
            page_title, page_text = _extract_readable_page(source["url"])
        except Exception as exc:
            logger.info("Research page fetch skipped: %s (%s)", source["url"], type(exc).__name__)
            continue
        if not page_text:
            continue
        fetched += 1
        parts.append(
            f"СОДЕРЖИМОЕ ИСТОЧНИКА [{idx}]"
            + (f" — {page_title}" if page_title else "")
            + f":\n{page_text}"
        )

    context = "\n\n".join(parts)
    if len(context) > RESEARCH_MAX_CONTEXT_CHARS:
        context = context[:RESEARCH_MAX_CONTEXT_CHARS] + "\n[исследовательский контекст обрезан по лимиту]"

    logger.info(
        "Research context ready: topic=%r sources=%d fetched=%d chars=%d",
        topic,
        len(gathered),
        fetched,
        len(context),
    )
    return context, gathered
