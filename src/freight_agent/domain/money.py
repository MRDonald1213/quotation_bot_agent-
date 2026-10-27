from __future__ import annotations

from decimal import Decimal, InvalidOperation

from .exceptions import ValidationFailureError


def parse_money(raw: object, *, field: str, allow_zero: bool = True,
                allow_negative: bool = False) -> Decimal | None:
    """Parse a monetary value from LLM/text input. Unknown stays None — never guessed."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise ValidationFailureError(f"{field}: booleans are not monetary amounts")
    if isinstance(raw, int):
        value = Decimal(raw)
    elif isinstance(raw, float):
        raise ValidationFailureError(f"{field}: floats rejected for money; use text/Decimal")
    elif isinstance(raw, str):
        cleaned = raw.strip().replace(",", "").replace("\u00a0", "")
        if cleaned in ("", "-", "N/A", "n/a", "none", "None"):
            return None
        try:
            value = Decimal(cleaned)
        except InvalidOperation as exc:
            raise ValidationFailureError(f"{field}: {raw!r} is not a valid amount") from exc
    elif isinstance(raw, Decimal):
        value = raw
    else:
        raise ValidationFailureError(f"{field}: unsupported money type {type(raw)}")
    if not value.is_finite():
        raise ValidationFailureError(f"{field}: monetary amounts must be finite")
    if value < 0 and not allow_negative:
        raise ValidationFailureError(
            f"{field}: negative amount {value}; discounts/credits must be flagged explicitly"
        )
    if value == 0 and not allow_zero:
        raise ValidationFailureError(f"{field}: zero amount not allowed")
    return value


def money_to_text(value: Decimal | None) -> str | None:
    """Persist money as text, never binary float."""
    return None if value is None else format(value, "f")


def text_to_money(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def normalize_currency(raw: str | None) -> str | None:
    """Normalize an ISO-4217-looking code; anything else is a validation failure."""
    import re

    if raw is None:
        return None
    code = re.sub(r"[^A-Za-z]", "", raw).upper()
    if not re.fullmatch(r"[A-Z]{3}", code):
        raise ValidationFailureError(f"currency {raw!r} is not a 3-letter ISO-4217 code")
    return code


def format_money(value: Decimal | None, currency: str | None) -> str:
    if value is None:
        return "unknown"
    return f"{value} {currency or '?'}"
