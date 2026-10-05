# Resolve persistent memory path before MemoryStore is created in part_02.py.
# Priority:
#   1) explicit BOT_MEMORY_DB
#   2) host-provided DATA_DIR (Bothost uses /app/data)
#   3) /app/data when present on Linux containers
#   4) local project directory for desktop development

_explicit_memory_db = os.getenv("BOT_MEMORY_DB", "").strip()
_host_data_dir = os.getenv("DATA_DIR", "").strip()

if _explicit_memory_db:
    MEMORY_DB_PATH = Path(_explicit_memory_db).expanduser().resolve()
elif _host_data_dir:
    MEMORY_DB_PATH = (Path(_host_data_dir).expanduser() / "bot_memory.db").resolve()
elif os.name != "nt" and Path("/app/data").is_dir():
    MEMORY_DB_PATH = Path("/app/data/bot_memory.db").resolve()
else:
    MEMORY_DB_PATH = Path(__file__).with_name("bot_memory.db").resolve()

MEMORY_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
