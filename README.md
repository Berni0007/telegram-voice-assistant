# Telegram Voice Assistant RU

Приватный Telegram-бот с голосовым вводом, NVIDIA AI и постоянной памятью.

## Что умеет

- принимает текст и голосовые сообщения;
- распознаёт русскую речь через NVIDIA Parakeet RNNT Multilingual ASR;
- отвечает через `nvidia/nemotron-3.5-lightning-30b-a3b`;
- озвучивает ответы через NVIDIA Chatterbox Multilingual `ru-RU`;
- хранит историю и долгосрочную память в SQLite;
- сохраняет память после перезапуска;
- Personality Engine адаптирует стиль ответа под ситуацию;
- длинные TTS-ответы автоматически разбиваются на безопасные фрагменты;
- текст ответа в Telegram не удаляется, даже если TTS/сеть позже дали ошибку.

## Команды

- `/start` — информация о боте
- `/mode` — режим ответа
- `/voice` — стиль русского голоса
- `/new` — новый диалог с сохранением важного контекста
- `/memory` — состояние памяти
- `/forget` — очистить память пользователя
- `/voices` — техническая информация о TTS

## Структура

`bot.py` — точка входа. Проверенный код v5.1 хранится в `src/part_*.py` и загружается в одном общем namespace в исходном порядке.

## Локальный запуск

Требуется Python 3.11+.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Заполните в `.env`:

```env
TELEGRAM_BOT_TOKEN=...
NVIDIA_API_KEY=...
```

Запуск:

```powershell
.\.venv\Scripts\python.exe .\bot.py
```

## Docker / хостинг

Сборка:

```bash
docker build -t telegram-voice-assistant .
```

Запуск с постоянной памятью:

```bash
docker run --rm \
  --env-file .env \
  -v "$(pwd)/data:/data" \
  telegram-voice-assistant
```

На Linux локальное воспроизведение через Windows `winsound` автоматически отключается. Голосовое сообщение в Telegram продолжает отправляться.

Для хоста рекомендуется:

```env
BOT_MEMORY_DB=/data/bot_memory.db
```

и постоянный volume, примонтированный к `/data`.

## Безопасность

В репозиторий **не должны попадать**:

- `.env`;
- Telegram Bot Token;
- NVIDIA API Key;
- `bot_memory.db`;
- голосовые/временные аудиофайлы;
- логи.

Для примера настроек используется только `.env.example`.

## Версия

`5.1.0` — persistent memory + Personality Engine + voice ASR/TTS pipeline.
