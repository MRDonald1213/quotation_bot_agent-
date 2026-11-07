"""Deterministic money helpers: currency conversion for ranking and for USD display.

This workspace is local-only, so there is no live rate feed. Rates come from a bundled
reference table with an as-of date and can be overridden pair by pair with FX_RATES_JSON
in .env. A rate that cannot be resolved is reported as missing; it is never guessed.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation

from ..config import Settings
from ..domain.exceptions import ValidationFailureError

RATES_AS_OF = "2026-11-03"

# Reference rate of one unit of currency in USD, rounded to 8 decimals.
BUNDLED_USD_RATES: dict[str, Decimal] = {
    "USD": Decimal("1"),
    "EUR": Decimal("1.08"),
    "GBP": Decimal("1.27"),
    "CHF": Decimal("1.11"),
    "SEK": Decimal("0.095"),
    "NOK": Decimal("0.094"),
    "DKK": Decimal("0.153"),
    "PLN": Decimal("0.258"),
    "CZK": Decimal("0.0435"),
    "HUF": Decimal("0.0027"),
    "RON": Decimal("0.22"),
    "BGN": Decimal("0.55"),
    "UAH": Decimal("0.024"),
    "RUB": Decimal("0.11"),
    "BYN": Decimal("0.30"),
    "TRY": Decimal("0.029"),
    "ILS": Decimal("0.27"),
    "AED": Decimal("0.2723"),
    "SAR": Decimal("0.2667"),
    "QAR": Decimal("0.2747"),
    "KWD": Decimal("3.25"),
    "AZN": Decimal("0.588"),
    "GEL": Decimal("0.37"),
    "AMD": Decimal("0.0026"),
    "KZT": Decimal("0.0021"),
    "CNY": Decimal("0.141"),
    "HKD": Decimal("0.128"),
    "JPY": Decimal("0.0068"),
    "KRW": Decimal("0.00072"),
    "TWD": Decimal("0.031"),
    "SGD": Decimal("0.78"),
    "MYR": Decimal("0.225"),
    "THB": Decimal("0.028"),
    "IDR": Decimal("0.000061"),
    "VND": Decimal("0.00004"),
    "PHP": Decimal("0.0175"),
    "INR": Decimal("0.0113"),
    "PKR": Decimal("0.0036"),
    "BDT": Decimal("0.0083"),
    "CAD": Decimal("0.73"),
    "MXN": Decimal("0.052"),
    "BRL": Decimal("0.185"),
    "ARS": Decimal("0.001"),
    "ZAR": Decimal("0.055"),
    "NGN": Decimal("0.00065"),
    "EGP": Decimal("0.02"),
    "AUD": Decimal("0.66"),
    "NZD": Decimal("0.60"),
}

_SYMBOLS = {"€": "EUR", "£": "GBP", "$": "USD", "¥": "JPY", "₽": "RUB", "₺": "TRY", "₹": "INR"}


def normalize_currency(value: str | None) -> str | None:
    """A three-letter code from whatever the document wrote: "EUR", "€ 1,200", "USD 1 200"."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    for token in re.sub(r"[^A-Za-z]", " ", text).upper().split():
        if len(token) == 3 and token.isalpha():
            return token
    symbol = next((ch for ch in text if ch in _SYMBOLS), None)
    return _SYMBOLS[symbol] if symbol else None


def to_decimal(value: object) -> Decimal | None:
    """Money arrives as text (SQLite convention) or a number; anything unreadable is None."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    text = str(value).strip().replace(" ", " ").replace("\u00a0", " ")
    text = re.sub(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", lambda m: m.group(0).replace(",", ""), text)
    # "1 200" and "1 200 500" are thousands separators in European freight paperwork, not two numbers
    while re.search(r"(?<=\d) (?=\d{3}(?:\D|$))", text):
        text = re.sub(r"(?<=\d) (?=\d{3}(?:\D|$))", "", text)
    # drop unit and currency noise so "1 200 EUR / kg" reads as 1200 rather than as 1
    text = re.sub(r"(?i)\b(usd|eur|gbp|chf|cny|azn|kg|kgs|m3|cbm|ton|tons|per|unit|rate|from|to)\b", " ", text)
    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return Decimal(match.group(0))
    except InvalidOperation:
        return None


def configured_rates(settings: Settings) -> dict[str, dict]:
    """Explicit `FROM->TO` entries from FX_RATES_JSON, overriding the bundled table."""
    raw = (settings.fx_rates_json or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationFailureError(f"FX_RATES_JSON is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValidationFailureError("FX_RATES_JSON must be a JSON object of 'FROM->TO' rate entries")
    table: dict[str, dict] = {}
    for pair, spec in data.items():
        rate_value = spec.get("rate") if isinstance(spec, dict) else spec
        as_of = spec.get("as_of") if isinstance(spec, dict) else None
        rate = to_decimal(rate_value)
        if rate is None or rate <= 0:
            raise ValidationFailureError(f"FX entry {pair!r} has no usable rate")
        table[str(pair).strip().upper()] = {"rate": rate, "as_of": as_of or "configured"}
    return table


def fx_table(settings: Settings) -> dict[str, dict]:
    """Every pair this workspace can resolve: bundled CUR->USD plus explicit overrides."""
    table = {f"{code}->USD": {"rate": rate, "as_of": RATES_AS_OF}
             for code, rate in BUNDLED_USD_RATES.items() if code != "USD"}
    table.update(configured_rates(settings))
    return table


def rate_between(source: str | None, target: str | None,
                 settings: Settings) -> tuple[Decimal | None, str | None]:
    """Return (rate, how_it_was_resolved). Unresolvable pairs return (None, reason)."""
    source = normalize_currency(source)
    target = normalize_currency(target)
    if not source or not target:
        return None, "the quote does not state a currency"
    if source == target:
        return Decimal("1"), None
    table = fx_table(settings)
    explicit = table.get(f"{source}->{target}")
    if explicit:
        return explicit["rate"], f"{source}→{target} {explicit['rate']} ({explicit['as_of']})"
    source_to_usd = table.get(f"{source}->USD")
    if source == "USD":
        source_to_usd = {"rate": Decimal("1"), "as_of": "base currency"}
    if target == "USD" and source_to_usd:
        return source_to_usd["rate"], (f"{source}→USD {source_to_usd['rate']} "
                                       f"(bundled reference rate, {source_to_usd['as_of']})")
    target_to_usd = table.get(f"{target}->USD")
    if source_to_usd and target_to_usd and target_to_usd["rate"] > 0:
        cross = (source_to_usd["rate"] / target_to_usd["rate"]).quantize(Decimal("0.000001"))
        return cross, (f"{source}→{target} {cross} derived through USD from the bundled "
                       f"reference rates ({RATES_AS_OF})")
    return None, f"no {source}→{target} rate is available in the local rate table"


def convert(amount: Decimal | None, source: str | None, target: str,
            settings: Settings) -> tuple[Decimal | None, str | None]:
    """Convert a money amount, never inventing a rate: None means 'not convertible here'."""
    if amount is None:
        return None, None
    rate, note = rate_between(source, target, settings)
    if rate is None:
        return None, f"{amount} {source or '?'} not converted to {target}: {note}"
    return (amount * rate).quantize(Decimal("0.01")), note


def usd(amount: Decimal | str | None, currency: str | None,
        settings: Settings) -> tuple[Decimal | None, str | None]:
    """The USD view of one quoted amount, with the resolution note shown beside it."""
    return convert(to_decimal(amount), currency, "USD", settings)


def money_view(amount: Decimal | str | None, currency: str | None,
               settings: Settings) -> dict:
    """Original plus USD, always both, so traceability survives the conversion."""
    value = to_decimal(amount)
    code = normalize_currency(currency)
    converted, note = usd(value, code, settings)
    return {
        "amount": None if value is None else str(value),
        "currency": code,
        "usd_amount": None if converted is None else str(converted),
        "fx_note": note,
        "display": format_amount(converted, "USD") if converted is not None else None,
        "original_display": format_amount(value, code) if value is not None else None,
    }


def format_amount(amount: Decimal | None, currency: str | None) -> str:
    if amount is None:
        return "not stated"
    code = currency or ""
    return f"{code + ' ' if code else ''}{amount:,.2f}"


__all__ = ["BUNDLED_USD_RATES", "RATES_AS_OF", "configured_rates", "convert", "fx_table",
           "format_amount", "money_view", "normalize_currency", "rate_between", "to_decimal", "usd"]
