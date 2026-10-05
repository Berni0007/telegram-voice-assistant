import asyncio
import io
import logging
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
import uuid
import wave
from pathlib import Path

try:
    import winsound
except ImportError:  # Linux/server hosting
    winsound = None
from datetime import datetime, timezone

import grpc
import imageio_ffmpeg
import riva.client
from openai import OpenAI
from dotenv import load_dotenv
from riva.client.proto.riva_audio_pb2 import AudioEncoding
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "").strip()

# Text brain of the assistant. NVIDIA's hosted API is OpenAI-compatible.
NVIDIA_LLM_BASE_URL = os.getenv(
    "NVIDIA_LLM_BASE_URL",
    "https://integrate.api.nvidia.com/v1",
).strip()

LLM_MODEL = os.getenv(
    "LLM_MODEL",
    "nvidia/nemotron-3.5-lightning-30b-a3b",
).strip()

LLM_SYSTEM_PROMPT = os.getenv(
    "LLM_SYSTEM_PROMPT",
    (
        "Ты русскоязычный голосовой ассистент. "
        "Давай только готовый ответ пользователю. "
        "Никогда не показывай рассуждения, внутренний анализ, план ответа "
        "или thinking process. "
        "Отвечай по смыслу, а не повторяй сообщение пользователя. "
        "Отвечай естественно, кратко и по делу. "
        "Для обычного голосового ответа старайся укладываться в 2-5 предложений. "
        "Не добавляй markdown, если он не нужен."
    ),
).strip()

PERSONALITY_ENGINE_PROMPT = os.getenv(
    "PERSONALITY_ENGINE_PROMPT",
    (
        "ХАРАКТЕР И СТИЛЬ СОБЕСЕДНИКА:\n"
        "Ты не служба поддержки и не справочник. Ты спокойный, живой, "
        "внимательный собеседник и практичный помощник.\n"
        "Говори по-русски естественно, как человек в обычном разговоре. "
        "Не начинай ответы шаблонами вроде «Конечно!», «Рад помочь», "
        "«Понимаю вас», если это не требуется по смыслу.\n"
        "Не пересказывай пользователю его же сообщение. Сразу реагируй на суть.\n"
        "Не соглашайся автоматически. Если есть основания считать иначе — "
        "спокойно скажи об этом и объясни почему.\n"
        "Подстраивай тон под ситуацию: техническая задача — конкретно и без воды; "
        "обычный разговор — свободнее и теплее; совет — с собственной оценкой "
        "и аргументами; раздражение пользователя — без канцелярита и без "
        "пустых извинений.\n"
        "Не изображай человеческие чувства, биографию или личный опыт. "
        "При этом речь должна оставаться естественной, а не роботизированной.\n"
        "Иногда уместна короткая живая реакция или лёгкий юмор, но не в каждом ответе.\n"
        "Задавай максимум один встречный вопрос и только когда он действительно "
        "помогает продолжить разговор, уточнить важное или раскрыть тему. "
        "Если задача уже решена — не заканчивай ответ дежурным вопросом.\n"
        "Используй память естественно. Можно ненавязчиво напомнить о предыдущем "
        "разговоре или незавершённой теме, если это действительно относится к "
        "текущему сообщению. Не демонстрируй память ради самой памяти.\n"
        "Для простых вопросов отвечай коротко. Если пользователь явно просит "
        "разобраться подробно — можно отвечать развёрнуто."
    ),
).strip()

SPEECH_REWRITE_ENABLED = (
    os.getenv("SPEECH_REWRITE_ENABLED", "1").strip().lower()
    not in {"0", "false", "no", "off"}
)


# Persistent local conversation memory.
MEMORY_DB_PATH = Path(
    os.getenv(
        "BOT_MEMORY_DB",
        str(Path(__file__).with_name("bot_memory.db")),
    )
).resolve()

MEMORY_RECENT_MESSAGES = int(
    os.getenv("MEMORY_RECENT_MESSAGES", "24")
)
MEMORY_SUMMARY_TRIGGER = int(
    os.getenv("MEMORY_SUMMARY_TRIGGER", "36")
)
MEMORY_KEEP_UNSUMMARIZED = int(
    os.getenv("MEMORY_KEEP_UNSUMMARIZED", "12")
)


NVIDIA_TTS_SERVER = os.getenv(
    "NVIDIA_TTS_SERVER",
    "grpc.nvcf.nvidia.com:443",
).strip()

NVIDIA_TTS_FUNCTION_ID = os.getenv(
    "NVIDIA_TTS_FUNCTION_ID",
    "ddacc747-1269-4fab-bfd9-8f593dead106",
).strip()

# NVIDIA Parakeet RNNT multilingual ASR cloud endpoint.
# Official NVIDIA Build function-id for parakeet-1.1b-rnnt-multilingual-asr.
NVIDIA_ASR_SERVER = os.getenv(
    "NVIDIA_ASR_SERVER",
    "grpc.nvcf.nvidia.com:443",
).strip()

NVIDIA_ASR_FUNCTION_ID = os.getenv(
    "NVIDIA_ASR_FUNCTION_ID",
    "71203149-d3b7-4460-8231-1be2543a1fca",
).strip()

ASR_LANGUAGE = os.getenv("ASR_LANGUAGE", "ru-RU").strip() or "ru-RU"

TTS_LANGUAGE = os.getenv("TTS_LANGUAGE", "ru-RU").strip() or "ru-RU"

# Optional: leave blank and Riva selects the first available voice
# matching the language code.
TTS_VOICE = os.getenv("TTS_VOICE", "").strip() or None
TTS_SAMPLE_RATE = int(os.getenv("TTS_SAMPLE_RATE", "22050"))

# Chatterbox hosted endpoint currently rejects long single requests.
# Keep each synthesis request comfortably below the observed 500-char limit.
TTS_CHUNK_MAX_CHARS = int(os.getenv("TTS_CHUNK_MAX_CHARS", "180"))
TTS_CHUNK_MAX_WORDS = int(os.getenv("TTS_CHUNK_MAX_WORDS", "24"))
TTS_CHUNK_RETRIES = int(os.getenv("TTS_CHUNK_RETRIES", "3"))

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN not set in .env")
if not NVIDIA_API_KEY:
    raise RuntimeError("NVIDIA_API_KEY not set in .env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("telegram-chatterbox")


def build_tts_service() -> riva.client.SpeechSynthesisService:
    auth = riva.client.Auth(
        use_ssl=True,
        uri=NVIDIA_TTS_SERVER,
        metadata_args=[
            ["function-id", NVIDIA_TTS_FUNCTION_ID],
            ["authorization", f"Bearer {NVIDIA_API_KEY}"],
        ],
    )
    return riva.client.SpeechSynthesisService(auth)


def build_asr_service() -> riva.client.ASRService:
    auth = riva.client.Auth(
        use_ssl=True,
        uri=NVIDIA_ASR_SERVER,
        metadata_args=[
            ["function-id", NVIDIA_ASR_FUNCTION_ID],
            ["authorization", f"Bearer {NVIDIA_API_KEY}"],
        ],
    )
    return riva.client.ASRService(auth)


tts_service = build_tts_service()
asr_service = build_asr_service()

llm_client = OpenAI(
    base_url=NVIDIA_LLM_BASE_URL,
    api_key=NVIDIA_API_KEY,
)

