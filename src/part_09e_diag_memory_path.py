# Make /diag show the actual resolved SQLite path, not only the raw env value.
_run_host_diagnostics_v521 = run_host_diagnostics


def run_host_diagnostics() -> str:
    report = _run_host_diagnostics_v521()
    raw = os.getenv("BOT_MEMORY_DB", "").strip() or "(default)"
    resolved = str(MEMORY_DB_PATH)
    report = report.replace(
        f"BOT_MEMORY_DB: {raw}",
        f"BOT_MEMORY_DB: {raw}\nMEMORY_DB_PATH: {resolved}",
        1,
    )
    return report[:3900]
