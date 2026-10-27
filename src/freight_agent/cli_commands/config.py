from __future__ import annotations

from .common import Table, console, settings_or_exit, typed_errors


@typed_errors
def show() -> None:
    settings = settings_or_exit()
    table = Table(title="effective configuration (from env / .env)")
    table.add_column("key")
    table.add_column("value")
    for key in (
        "ollama_base_url", "ollama_model", "classification_confidence_threshold",
        "wait_window_minutes", "max_file_size_mb", "max_text_chars",
        "llm_timeout_seconds", "llm_max_retries", "workspace_dir", "database_url",
    ):
        value = getattr(settings, key)
        marker = " [red](unset)[/red]" if key == "ollama_model" and not value else ""
        table.add_row(key.upper(), f"{value}{marker}")
    console.print(table)
