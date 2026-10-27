from __future__ import annotations

from decimal import Decimal

import pytest

from freight_agent.domain.exceptions import ValidationFailureError
from freight_agent.domain.money import normalize_currency, parse_money
from freight_agent.domain.models import Charge
from freight_agent.services.validation import compute_total_known_cost


def test_parse_money_text_and_rejects():
    assert parse_money("3,200", field="x") == Decimal("3200")
    assert parse_money(None, field="x") is None          # unknown stays None, never guessed
    assert parse_money("0", field="x") == Decimal("0")
    with pytest.raises(ValidationFailureError):
        parse_money("-5", field="x")                     # negative not allowed
    with pytest.raises(ValidationFailureError):
        parse_money(3.5, field="x")                      # floats rejected for money
    with pytest.raises(ValidationFailureError):
        parse_money("abc", field="x")


def test_normalize_currency():
    assert normalize_currency(" eur ") == "EUR"
    with pytest.raises(ValidationFailureError):
        normalize_currency("Dollars")


def test_total_excludes_charges_already_in_base():
    total, notes = compute_total_known_cost(
        Decimal("100"), "EUR",
        [Charge(label="THC", amount=Decimal("20"), currency="EUR", included_in_base_price=True)])
    assert total == Decimal("100")


def test_total_blocks_on_foreign_currency_charge_no_fx():
    total, notes = compute_total_known_cost(
        Decimal("100"), "EUR",
        [Charge(label="Doc fee", amount=Decimal("50"), currency="USD",
                included_in_base_price=False)])
    assert total is None            # no silent FX conversion
    assert any("FX" in n or "summed" in n for n in notes)


def test_total_blocks_on_unknown_mandatory_charge_not_zero():
    total, notes = compute_total_known_cost(
        Decimal("100"), "EUR",
        [Charge(label="Warehouse delivery", amount=None, currency="EUR", mandatory=True)])
    assert total is None
    assert any("not zero" in n for n in notes)   # unpriced charge is NOT treated as zero


def test_total_sums_confirmed_additional_same_currency():
    total, _ = compute_total_known_cost(
        Decimal("3200"), "EUR",
        [Charge(label="THC", amount=Decimal("340"), currency="EUR",
                included_in_base_price=False, mandatory=True),
         Charge(label="BL fee", amount=Decimal("85"), currency="EUR",
                included_in_base_price=False)])
    assert total == Decimal("3625")
