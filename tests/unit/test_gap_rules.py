from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

from freight_agent.domain.enums import ComparisonStatus, DeliveryScope, GapSeverity, GapType
from freight_agent.domain.models import Charge, Quotation, Shipment
from freight_agent.services.gap_analysis import (
    comparison_status_after_gaps,
    detect_gaps_deterministic,
    readiness,
)


def _shipment(**kw) -> Shipment:
    base = dict(reference="S-1", name="n", origin="Baku", destination="Hamburg",
                cargo_description="parts", required_delivery_scope=DeliveryScope.door_to_door,
                target_currency="EUR")
    base.update(kw)
    return Shipment.model_validate(base)


def _quote(shipment, **kw) -> Quotation:
    base = dict(shipment_id=str(shipment.id), intake_item_id=str(uuid4()),
                currency="EUR", base_price=Decimal("1000"),
                total_known_cost=Decimal("1000"),
                comparison_status=ComparisonStatus.comparable,
                delivery_scope=DeliveryScope.door_to_door,
                valid_until=date.today() + timedelta(days=30),
                transit_days_min=10, transit_days_max=14,
                insurance_included=True, customs_included=True,
                quote_reference="Q-1")
    base.update(kw)
    return Quotation.model_validate(base)


def test_missing_critical_fields_produce_critical_gaps():
    s = _shipment()
    q = _quote(s, base_price=None, currency=None)
    gaps = detect_gaps_deterministic(s, q)
    fields = {g.field_name for g in gaps if g.severity == GapSeverity.critical}
    assert {"base_price", "currency"} <= fields
    assert readiness(gaps).value == "not_ready"


def test_scope_mismatch_is_critical_and_never_comparable():
    s = _shipment(required_delivery_scope=DeliveryScope.door_to_door)
    q = _quote(s, delivery_scope=DeliveryScope.port_to_port)
    gaps = detect_gaps_deterministic(s, q)
    assert any(g.gap_type == GapType.scope_mismatch and g.severity == GapSeverity.critical
               for g in gaps)
    assert comparison_status_after_gaps(q, gaps) == ComparisonStatus.incomplete


def test_expired_quote_marks_expired_status():
    s = _shipment()
    q = _quote(s, valid_until=date.today() - timedelta(days=1))
    gaps = detect_gaps_deterministic(s, q)
    assert any(g.gap_type == GapType.expired for g in gaps)
    assert comparison_status_after_gaps(q, gaps) == ComparisonStatus.expired


def test_currency_mismatch_flagged_no_fx():
    s = _shipment(target_currency="EUR")
    q = _quote(s, currency="USD")
    gaps = detect_gaps_deterministic(s, q)
    assert any(g.field_name == "currency" and "FX" in g.description for g in gaps)


def test_unknown_mandatory_charge_blocks_readiness():
    s = _shipment()
    q = _quote(s, known_additional_charges=[
        Charge(label="warehouse delivery", amount=None, currency="EUR", mandatory=True)])
    gaps = detect_gaps_deterministic(s, q)
    assert any(g.field_name == "charges" and g.severity == GapSeverity.critical for g in gaps)
    assert comparison_status_after_gaps(q, gaps) == ComparisonStatus.incomplete


def test_complete_quote_is_comparable_and_ready():
    s = _shipment()
    q = _quote(s)
    gaps = detect_gaps_deterministic(s, q)
    assert gaps == []
    assert readiness(gaps).value == "ready"
    assert comparison_status_after_gaps(q, gaps) == ComparisonStatus.comparable
