# Internet search / webpage reading layer.
# Loaded before part_04.py so generate_assistant_reply() can use it.

from urllib.parse import parse_qs, unquote, urlparse

import httpx
from bs4 import BeautifulSoup


WEB_SEARCH_ENABLED = (
    os.getenv("WEB_SEARCH_ENABLED", "1").strip().lower()
    not in {"0", "false", "no", "off"}
)
WEB_SEARCH_RESULTS = max(1, int(os.getenv("WEB_SEARCH_RESULTS", "5")))
WEB_FETCH_PAGES = max(0, int(os.getenv("WEB_FETCH_PAGES", "3")))
WEB_TIMEOUT_SECONDS = max(3.0, float(os.getenv("WEB_TIMEOUT_SECONDS", "10")))
WEB_MAX_PAGE_CHARS = max(1000, int(os.getenv("WEB_MAX_PAGE_CHARS", "5000")))
WEB_MAX_CONTEXT_CHARS = max(3000, int(os.getenv("WEB_MAX_CONTEXT_CHARS", "18000")))

_WEB_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
}


def _extract_urls(text: str) -> list[str]:
    urls = re.findall(r"https?://[^\s<>\]\[()]+", text or "", flags=re.IGNORECASE)
    clean: list[str] = []
    for url in urls:
        url = url.rstrip(".,;:!?\"'")
        if url and url not in clean:
            clean.append(url)
    return clean[:5]


def _looks_like_private_host(url: str) -> bool:
    """Avoid fetching obvious local/private destinations."""
    try:
        host = (urlparse(url).hostname or "").lower().strip(".")
    except Exception:
        return True

    if not host:
        return True
    if host in {"localhost", "localhost.localdomain"}:
        return True
    if host.endswith(".local"):
        return True
    if host.startswith("127.") or host.startswith("10.") or host.startswith("192.168."):
        return True
    if host.startswith("169.254."):
        return True
    if host.startswith("172."):
        try:
            second = int(host.split(".")[1])
            if 16 <= second <= 31:
                return True
        except Exception:
            pass
    if host in {"::1", "0.0.0.0"}:
        return True
    return False


def _unwrap_duckduckgo_url(href: str) -> str:
    href = (href or "").strip()
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    try:
        parsed = urlparse(href)
        qs = parse_qs(parsed.query)
        if "uddg" in qs and qs["uddg"]:
            return unquote(qs["uddg"][0])
    except Exception:
        pass
    return href


def _clean_search_query(user_text: str) -> str:
    text = re.sub(r"https?://\S+", " ", user_text or "", flags=re.IGNORECASE)
    prefixes = (
        "найди в интернете", "поищи в интернете", "посмотри в интернете",
        "найди в сети", "поищи в сети", "загугли", "найди", "поищи",
        "проверь в интернете", "проверь в сети", "посмотри актуальную информацию",
    )
    low = text.lower().strip()
    for prefix in prefixes:
        if low.startswith(prefix):
            text = text[len(prefix):].lstrip(" :,-")
            break
    text = re.sub(r"\s+", " ", text).strip()
    return text[:500]


def should_use_web_search(user_text: str) -> bool:
    """Fast local router: use the web when freshness/search intent is visible."""
    if not WEB_SEARCH_ENABLED:
        return False

    raw = (user_text or "").strip()
    if not raw:
        return False
    low = raw.lower()

    if _extract_urls(raw):
        return True

    explicit = (
        "найди в интернете", "поищи в интернете", "посмотри в интернете",
        "проверь в интернете", "найди в сети", "поищи в сети", "загугли",
        "поиск в интернете", "web search", "internet search",
    )
    if any(x in low for x in explicit):
        return True

    # Requests where stale model knowledge is especially risky.
    freshness = (
        "сегодня", "сейчас", "на данный момент", "актуальн", "последн",
        "свеж", "новост", "курс ", "цена ", "стоимость ", "погода",
        "расписание", "результат матча", "вышел ли", "обновлен", "обновлён",
        "версия ", "релиз ", "патч ", "ваканси", "работает ли сайт",
        "кто сейчас", "что сейчас", "где сейчас",
    )
    if any(x in low for x in freshness):
        return True

    # Broad verbs still count as an explicit request to retrieve information.
    retrieve = (
        "найди информацию", "найди данные", "поищи информацию",
        "проверь информацию", "проверь данные", "посмотри информацию",
    )
    return any(x in low for x in retrieve)


def _duckduckgo_search(query: str) -> list[dict[str, str]]:
    if not query:
        return []

    results: list[dict[str, str]] = []
    with httpx.Client(
        headers=_WEB_HEADERS,
        follow_redirects=True,
        timeout=WEB_TIMEOUT_SECONDS,
    ) as client:
        response = client.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query, "kl": "ru-ru"},
        )
        response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    for block in soup.select(".result"):
        link = block.select_one("a.result__a")
        if link is None:
            continue
        title = link.get_text(" ", strip=True)
        url = _unwrap_duckduckgo_url(link.get("href", ""))
        if not url.startswith(("http://", "https://")) or _looks_like_private_host(url):
            continue
        snippet_node = block.select_one(".result__snippet")
        snippet = snippet_node.get_text(" ", strip=True) if snippet_node else ""
        results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= WEB_SEARCH_RESULTS:
            break

    return results


def _extract_readable_page(url: str) -> tuple[str, str]:
    if _looks_like_private_host(url):
        return "", ""

    with httpx.Client(
        headers=_WEB_HEADERS,
        follow_redirects=True,
        timeout=WEB_TIMEOUT_SECONDS,
    ) as client:
        response = client.get(url)
        response.raise_for_status()

    content_type = (response.headers.get("content-type") or "").lower()
    if "text/html" not in content_type and "text/plain" not in content_type:
        return "", ""

    if "text/plain" in content_type:
        return "", re.sub(r"\s+", " ", response.text).strip()[:WEB_MAX_PAGE_CHARS]

    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "canvas", "form", "nav", "footer"]):
        tag.decompose()

    title = ""
    if soup.title:
        title = soup.title.get_text(" ", strip=True)

    parts: list[str] = []
    for node in soup.select("article p, main p, p, article li, main li, h1, h2, h3"):
        text = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
        if len(text) < 35:
            continue
        if text not in parts:
            parts.append(text)
        if sum(len(x) + 1 for x in parts) >= WEB_MAX_PAGE_CHARS:
            break

    body = "\n".join(parts)[:WEB_MAX_PAGE_CHARS]
    return title, body


def build_web_context(user_text: str) -> tuple[str, list[dict[str, str]]]:
    """Return compact web evidence and sources for the main LLM request."""
    if not should_use_web_search(user_text):
        return "", []

    direct_urls = _extract_urls(user_text)
    sources: list[dict[str, str]] = []

    if direct_urls:
        for url in direct_urls:
            if not _looks_like_private_host(url):
                sources.append({"title": url, "url": url, "snippet": ""})
    else:
        query = _clean_search_query(user_text)
        try:
            sources = _duckduckgo_search(query)
        except Exception:
            logger.exception("Web search failed for query=%r", query)
            return "", []

    if not sources:
        return "", []

    context_parts: list[str] = [
        "ИНФОРМАЦИЯ ИЗ ИНТЕРНЕТА. Используй её как внешние источники. "
        "Не выдумывай факты, которых здесь нет. Если источники расходятся, скажи об этом."
    ]

    for idx, source in enumerate(sources, start=1):
        block = [
            f"[{idx}] {source.get('title') or source['url']}",
            f"URL: {source['url']}",
        ]
        if source.get("snippet"):
            block.append(f"Поисковый фрагмент: {source['snippet']}")
        context_parts.append("\n".join(block))

    # Read a few best pages for richer evidence. Search snippets remain as fallback.
    for idx, source in enumerate(sources[:WEB_FETCH_PAGES], start=1):
        try:
            page_title, page_text = _extract_readable_page(source["url"])
        except Exception as exc:
            logger.info("Web page fetch skipped: %s (%s)", source["url"], type(exc).__name__)
            continue
        if page_text:
            context_parts.append(
                f"СОДЕРЖИМОЕ ИСТОЧНИКА [{idx}]"
                + (f" — {page_title}" if page_title else "")
                + f":\n{page_text}"
            )

    context = "\n\n".join(context_parts)
    if len(context) > WEB_MAX_CONTEXT_CHARS:
        context = context[:WEB_MAX_CONTEXT_CHARS] + "\n[контекст обрезан по лимиту]"

    logger.info(
        "Web context ready: sources=%d chars=%d",
        len(sources),
        len(context),
    )
    return context, sources


def append_web_sources(answer: str, sources: list[dict[str, str]]) -> str:
    if not sources:
        return answer

    lines = []
    for idx, source in enumerate(sources[:5], start=1):
        title = (source.get("title") or "Источник").strip()
        lines.append(f"[{idx}] {title}: {source['url']}")

    return answer.rstrip() + "\n\nИсточники:\n" + "\n".join(lines)
