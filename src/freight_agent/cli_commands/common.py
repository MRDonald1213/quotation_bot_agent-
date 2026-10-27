from __future__ import annotations

from functools import wraps
from typing import Callable

import typer
from rich.console import Console
from rich.table import Table

from ..config import Settings, get_settings
from ..domain.exceptions import ConfigurationError, FreightAgentError

console = Console()
err_console = Console(stderr=True)


def typed_errors(fn: Callable[..., object]) -> Callable[..., None]:
    """Catch typed domain errors, print actionable message, return non-zero exit code."""

    @wraps(fn)
    def wrapper(*args: object, **kwargs: object) -> None:
        try:
            fn(*args, **kwargs)
        except typer.Exit:
            raise
        except FreightAgentError as exc:
            err_console.print(f"[red]error:[/red] {exc}")
            raise typer.Exit(code=exc.exit_code) from None
        except Exception as exc:  # noqa: BLE001
            err_console.print(f"[red]unexpected error:[/red] {type(exc).__name__}: {exc}")
            raise typer.Exit(code=1) from None

    return wrapper


def settings_or_exit() -> Settings:
    try:
        return get_settings()
    except Exception as exc:  # noqa: BLE001
        raise ConfigurationError(f"invalid configuration: {exc}") from exc


__all__ = ["console", "err_console", "typed_errors", "settings_or_exit", "Table", "typer"]
