"""Entry point for Telegram Voice Assistant v5.1.

Source is split into ordered readable parts under ./src.  They execute in one
shared namespace, preserving the exact behavior of the tested single-file bot.
"""
from pathlib import Path

SOURCE_DIR = Path(__file__).with_name("src")

for source_file in sorted(SOURCE_DIR.glob("part_*.py")):
    code = compile(
        source_file.read_text(encoding="utf-8"),
        str(source_file),
        "exec",
    )
    exec(code, globals(), globals())
