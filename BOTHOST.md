# Bothost deployment

## Project

- Platform: Telegram
- Repository: `Berni0007/telegram-voice-assistant`
- Branch: `main`
- Main file: `bot.py`
- Custom Dockerfile: `Dockerfile`

## Required environment variables

Do not store real values in GitHub.

```env
NVIDIA_API_KEY=<your NVIDIA API key>
BOT_MEMORY_DB=/app/data/bot_memory.db
```

For Telegram token, Bothost injects `TELEGRAM_BOT_TOKEN` from the Bot Token field for Telegram projects. If needed, it can also be added manually as an environment variable.

## Recommended variables

```env
NVIDIA_LLM_BASE_URL=https://integrate.api.nvidia.com/v1
LLM_MODEL=nvidia/nemotron-3.5-lightning-30b-a3b

NVIDIA_TTS_SERVER=grpc.nvcf.nvidia.com:443
NVIDIA_TTS_FUNCTION_ID=ddacc747-1269-4fab-bfd9-8f593dead106
TTS_LANGUAGE=ru-RU
TTS_VOICE=Chatterbox-Multilingual.ru-RU.Male
TTS_SAMPLE_RATE=22050
TTS_CHUNK_MAX_CHARS=180
TTS_CHUNK_MAX_WORDS=24
TTS_CHUNK_RETRIES=3

NVIDIA_ASR_SERVER=grpc.nvcf.nvidia.com:443
NVIDIA_ASR_FUNCTION_ID=71203149-d3b7-4460-8231-1be2543a1fca
ASR_LANGUAGE=ru-RU

MEMORY_RECENT_MESSAGES=24
MEMORY_SUMMARY_TRIGGER=36
MEMORY_KEEP_UNSUMMARIZED=12
SPEECH_REWRITE_ENABLED=1
```

## Persistent memory

Bothost preserves `/app/data` between Git deploys. The bot stores SQLite here:

```text
/app/data/bot_memory.db
```

Do not move the database back to the repository root on Bothost.

## Deployment

1. Connect the private GitHub repository.
2. Use branch `main`.
3. Enable/use the custom `Dockerfile`.
4. Set main file to `bot.py` if the panel asks for it.
5. Add the NVIDIA API key in Environment Variables.
6. Put the Telegram token into the Telegram Bot Token field.
7. Deploy.

The bot uses long polling, so no domain, webhook or public port is required.

## Important

Only one running instance may use the same Telegram bot token. If the Windows copy is still running when Bothost starts, Telegram will return `409 Conflict` for `getUpdates`. Stop the local `bot.py` before enabling the hosted instance.
