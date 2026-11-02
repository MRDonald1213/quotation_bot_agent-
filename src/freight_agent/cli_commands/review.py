"""`freight review` — serve the local web app (FastAPI on 127.0.0.1) for workflows A, B and C."""
from __future__ import annotations

import webbrowser

import typer

from ..domain.exceptions import ConfigurationError
from .common import console, settings_or_exit, typed_errors


@typed_errors
def review(
    port: int = typer.Option(8765, "--port", "-p", help="Local port; the server binds to 127.0.0.1 only."),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open the page in the default browser."),
) -> None:
    """Serve the local review app: upload, processing, review, approve, import and comparison."""
    settings = settings_or_exit()
    import uvicorn
    from ..api.app import create_app

    try:
        app = create_app(settings)
    except ConfigurationError as exc:
        raise exc
    url = f"http://127.0.0.1:{port}/"
    console.print(f"review app: [bold]{url}[/bold]  (Ctrl-C to stop)")
    console.print("[dim]local only: document text stays on this machine and goes to Ollama; "
                  "an email leaves only when you approve a draft and press Send yourself[/dim]")
    if open_browser:
        webbrowser.open(url)
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    except OSError as exc:
        raise ConfigurationError(f"cannot start the review app on port {port}: {exc}") from exc
    console.print("[dim]review app closed[/dim]")


__all__ = ["review"]
