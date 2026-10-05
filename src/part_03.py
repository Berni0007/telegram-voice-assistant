MAX_PENDING_VOICE_ANSWERS = 100

# NVIDIA Chatterbox Multilingual cloud has one built-in default speaker
# per locale. For ru-RU we tune supported prosody/emotion instead of
# pretending that multiple cloud speakers exist.
DEFAULT_RU_VOICE = os.getenv(
    "TTS_VOICE",
    "Chatterbox-Multilingual.ru-RU.Male",
).strip() or "Chatterbox-Multilingual.ru-RU.Male"

user_tts_exaggeration: dict[int, float] = {}

VOICE_STYLES = {
    "calm": ("Спокойный", 0.30),
    "neutral": ("Нейтральный", 0.50),
    "expressive": ("Выразительный", 0.70),
    "emotional": ("Эмоциональный", 1.00),
}

VOICE_TEST_TEXT = (
    "Привет! Это тест русского голоса. "
    "Сегодня хорошая погода, и я говорю спокойно и естественно."
)


def get_output_mode(user_id: int) -> str:
    return user_output_mode.get(user_id, "pc_auto")


def get_tts_voice(user_id: int | None = None) -> str:
    return DEFAULT_RU_VOICE


def get_tts_exaggeration(user_id: int | None = None) -> float:
    if user_id is not None and user_id in user_tts_exaggeration:
        return user_tts_exaggeration[user_id]
    return 0.50


def mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔊 Авто + Telegram",
                    callback_data="mode:pc_auto",
                ),
                InlineKeyboardButton(
                    "▶️ По кнопке",
                    callback_data="mode:manual",
                ),
            ]
        ]
    )


def remember_manual_voice(user_id: int, answer: str) -> str:
    token = uuid.uuid4().hex[:12]
    pending_voice_answers[token] = (user_id, answer)

    # Keep memory bounded.
    while len(pending_voice_answers) > MAX_PENDING_VOICE_ANSWERS:
        first_key = next(iter(pending_voice_answers))
        pending_voice_answers.pop(first_key, None)

    return token


def play_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "▶️ Проиграть ответ",
                    callback_data=f"play:{token}",
                )
            ]
        ]
    )


def clean_llm_answer(content: str) -> str:
    """
    Defense in depth: reasoning is disabled at request level, but if a backend
    ever leaks a <think> block, remove it before Telegram/TTS.
    """
    answer = (content or "").strip()

    # Standard reasoning delimiters.
    answer = re.sub(
        r"<think>.*?</think>",
        "",
        answer,
        flags=re.IGNORECASE | re.DOTALL,
    ).strip()

    # Some backends may leave only a closing delimiter.
    answer = re.sub(
        r"^.*?</think>\s*",
        "",
        answer,
        flags=re.IGNORECASE | re.DOTALL,
    ).strip()

    return answer


def infer_interaction_context(user_text: str) -> str:
    """
    Lightweight local hint for the main LLM call.
    This is deliberately heuristic: it nudges tone without adding another
    NVIDIA request or slowing every message down.
    """
    raw = (user_text or "").strip()
    low = raw.lower()

    frustrated_markers = (
        "не работает", "сломал", "сломалось", "ошибка", "косяк", "накосячил",
        "бесит", "задолбал", "тупит", "херня", "идиот", "плохо работает",
    )
    emotional_markers = (
        "устал", "тяжело", "грустно", "настроение", "переживаю", "боюсь",
        "рад", "доволен", "обидно", "одиноко",
    )
    advice_markers = (
        "как лучше", "что думаешь", "как считаешь", "стоит ли", "посоветуй",
        "что бы ты выбрал", "какой вариант",
    )
    task_markers = (
        "сделай", "исправь", "настрой", "добавь", "убери", "замени",
        "проверь", "найди", "создай", "напиши", "запусти",
    )
    technical_markers = (
        "traceback", "error", "python", "powershell", "api", "json", "bot.py",
        "docker", "github", "sqlite", "telegram", "nvidia", "сервер", "лог",
    )

    if any(x in low for x in frustrated_markers):
        mode = "исправление проблемы / пользователь раздражён"
        guidance = (
            "Сначала дай полезное действие или понятный вывод. "
            "Не отвечай корпоративными извинениями и не морализируй."
        )
    elif any(x in low for x in emotional_markers):
        mode = "личный/эмоциональный разговор"
        guidance = (
            "Отвечай мягче и человечнее, но без искусственной психологии "
            "и без шаблонных заверений."
        )
    elif any(x in low for x in advice_markers):
        mode = "совет / совместное решение"
        guidance = (
            "Дай собственную оценку, назови главный компромисс и не прячься "
            "за нейтральным списком вариантов."
        )
    elif any(x in low for x in task_markers) or any(x in low for x in technical_markers):
        mode = "задача / техническая работа"
        guidance = (
            "Будь максимально конкретным. Результат и следующий шаг важнее "
            "разговорных украшений."
        )
    elif len(raw) <= 80 and "?" not in raw:
        mode = "обычный короткий разговор"
        guidance = (
            "Отвечай естественно и не превращай короткую реплику в лекцию."
        )
    else:
        mode = "обычный разговор / вопрос"
        guidance = (
            "Поддерживай живой диалог и соразмеряй длину ответа с вопросом."
        )

    return (
        "ТЕКУЩИЙ РЕЖИМ ОБЩЕНИЯ (это слабая подсказка, контекст важнее):\n"
        f"{mode}.\n{guidance}"
    )


def prepare_speech_text(text: str) -> str:
    """
    Convert the written Telegram answer into something better suited for TTS.

    Important: this never changes the written answer or the stored memory.
    It only cleans the copy sent to Chatterbox.
    """
    if not SPEECH_REWRITE_ENABLED:
        return (text or "").strip()

    spoken = (text or "").strip()
    if not spoken:
        return spoken

    # Markdown links: speak the label, not the URL.
    spoken = re.sub(r"\[([^\]]+)\]\((?:https?://|www\.)[^)]+\)", r"\1", spoken)

    # Do not make TTS read code/commands character by character.
    had_code_block = bool(re.search(r"```[\s\S]*?```", spoken))
    spoken = re.sub(r"```[\s\S]*?```", "", spoken)

    # Inline code: keep human-readable content but remove backticks.
    spoken = re.sub(r"`([^`]+)`", r"\1", spoken)

    # Remove raw links from speech.
    spoken = re.sub(r"https?://\S+|www\.\S+", "ссылка", spoken)

    # Markdown decoration / headings / bullets -> natural pauses.
    spoken = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", spoken)
    spoken = re.sub(r"(?m)^\s*[-*•]\s+", "", spoken)
    spoken = spoken.replace("**", "").replace("__", "")
    spoken = re.sub(r"(?m)^\s*\d+[.)]\s+", "", spoken)

    # Common technical words sound more naturally in Russian TTS this way.
    substitutions = {
        r"\bNVIDIA\b": "Энвидиа",
        r"\bNemotron\b": "Немотрон",
        r"\bChatterbox\b": "Чаттербокс",
        r"\bTelegram\b": "Телеграм",
        r"\bGitHub\b": "Гитхаб",
        r"\bAPI\b": "эй-пи-ай",
        r"\bTTS\b": "ти-ти-эс",
        r"\bASR\b": "эй-эс-ар",
        r"\bGPU\b": "джи-пи-ю",
        r"\bCPU\b": "си-пи-ю",
        r"\bRTX\b": "эр-ти-экс",
        r"\bSQL\b": "эс-кью-эл",
        r"\bSQLite\b": "эс-кью-лайт",
    }
    for pattern, replacement in substitutions.items():
        spoken = re.sub(pattern, replacement, spoken, flags=re.IGNORECASE)

    # Make list-like line breaks sound like sentence pauses.
    spoken = re.sub(r"\s*\n+\s*", ". ", spoken)
    spoken = re.sub(r"\s+", " ", spoken)
    spoken = re.sub(r"\.{2,}", ".", spoken)
    spoken = re.sub(r"\s+([,.;:!?])", r"\1", spoken).strip()

    if had_code_block:
        tail = " Код и команды я оставил в текстовом сообщении."
        if "код и команды" not in spoken.lower():
            spoken = (spoken.rstrip(". ") + "." + tail).strip()

    return spoken


