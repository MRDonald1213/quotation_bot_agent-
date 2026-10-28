from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timezone
from decimal import Decimal

import pycountry
from sqlalchemy.exc import IntegrityError

from ..db.engine import get_session
from ..db.repositories import Repos
from ..config import Settings
from ..domain.enums import DeliveryScope, ShipmentStatus
from ..domain.exceptions import ValidationFailureError
from ..domain.models import AuditEvent, IntakeItem, QuoteExtraction, Shipment
from .common import with_session
from .validation import country_from_location, normalize_country, snippet_grounded


def create_shipment(settings: Settings, *, reference: str, name: str, origin: str,
                    destination: str, cargo_description: str,
                    weight_kg: Decimal | None = None, volume_m3: Decimal | None = None,
                    required_delivery_scope: DeliveryScope = DeliveryScope.unknown,
                    target_currency: str = "EUR", notes: str | None = None,
                    origin_country: str | None = None, destination_country: str | None = None) -> Shipment:
    if not reference.strip() or not name.strip():
        raise ValidationFailureError("reference and name are required")
    if not (len(target_currency) == 3 and target_currency.isalpha() and target_currency.isupper()):
        raise ValidationFailureError(f"target_currency must be an uppercase ISO-4217 code, got {target_currency!r}")
    shipment = Shipment(
        reference=reference.strip(), name=name.strip(), origin=origin, destination=destination,
        origin_country=normalize_country(origin_country) if origin_country else country_from_location(origin),
        destination_country=normalize_country(destination_country) if destination_country else country_from_location(destination),
        cargo_description=cargo_description, weight_kg=weight_kg, volume_m3=volume_m3,
        required_delivery_scope=required_delivery_scope, target_currency=target_currency, notes=notes,
        approved_at=datetime.now(timezone.utc).replace(microsecond=0),
    )
    with with_session(settings) as repos:
        try:
            repos.add_shipment(shipment)
        except IntegrityError as exc:
            if "reference" in str(exc.orig):
                raise ValidationFailureError(
                    f"shipment reference {shipment.reference!r} already exists; "
                    "reuse it via `freight shipment list` or pick a new one") from None
            raise
        repos.add_audit(AuditEvent(entity_type="shipment", entity_id=shipment.id,
                                   action="created", details_json=f'{{"reference": "{shipment.reference}"}}'))
    return shipment


def list_shipments(settings: Settings) -> list[Shipment]:
    with with_session(settings) as repos:
        return repos.list_shipments()


def get_shipment(settings: Settings, id_or_reference: str) -> Shipment:
    with with_session(settings) as repos:
        return repos.get_shipment(id_or_reference)


def close_shipment(settings: Settings, id_or_reference: str) -> Shipment:
    with with_session(settings) as repos:
        shipment = repos.get_shipment(id_or_reference)
        shipment.status = ShipmentStatus.closed
        repos.update_shipment(shipment)
        repos.add_audit(AuditEvent(entity_type="shipment", entity_id=shipment.id, action="closed"))
        return shipment


def _location_key(location: str, country_code: str | None) -> str:
    value = unicodedata.normalize("NFKC", location).casefold()
    value = " ".join(re.findall(r"\w+", value))
    country = pycountry.countries.get(alpha_2=country_code) if country_code else None
    if country:
        names = {country.name, getattr(country, "official_name", country.name),
                 getattr(country, "common_name", country.name), country.alpha_2, country.alpha_3}
        for name in sorted(names, key=len, reverse=True):
            normalized = " ".join(re.findall(r"\w+", name.casefold()))
            value = re.sub(r"(?<!\w)" + re.escape(normalized) + r"(?!\w)", country.alpha_2.casefold(), value)
    return " ".join(value.split())


def _next_reference(repos: Repos) -> str:
    """A short sequential reference: procurement staff read these aloud, UUIDs are unusable."""
    year = datetime.now(timezone.utc).year
    taken = {s.reference for s in repos.list_shipments()}
    number = len(taken) + 1
    while f"SHP-{year}-{number:03d}" in taken:
        number += 1
    return f"SHP-{year}-{number:03d}"


_ADDRESS_NOISE = re.compile(
    r"\b(warehouse|terminal|depot|zone|street|str\.?|avenue|ave\.?|road|rd\.?|building|bldg\.?|"
    r"block|floor|industrial|logistics|office|no\.?)\b", re.IGNORECASE)


def short_place(value: str | None) -> str:
    """City-level label for one route endpoint — the full delivery address stays in `origin`."""
    if not value:
        return "?"
    parts = [part.strip() for part in re.split(r"[,;]|\s{2,}", value) if part.strip()]
    for part in parts:
        if len(part) <= 30 and not _ADDRESS_NOISE.search(part):
            return part
    return (parts[0][:30] if parts else value[:30]).strip()


def route_label(origin: str | None, destination: str | None) -> str:
    return f"{short_place(origin)} → {short_place(destination)}"


def resolve_document_shipment(repos: Repos, item: IntakeItem, extraction: QuoteExtraction,
                              explicit: Shipment | None = None, reference: str | None = None
                              ) -> tuple[Shipment | None, bool, list[str]]:
    reviewed = set(extraction.fields_requiring_review)
    origin_country = extraction.origin_country or country_from_location(extraction.origin or "")
    destination_country = extraction.destination_country or country_from_location(extraction.destination or "")
    candidates = repos.list_shipments()
    selected = explicit or (repos.get_shipment(str(item.shipment_id)) if item.shipment_id else None)
    if selected is None and reference:
        selected = next((s for s in candidates if s.reference == reference), None)
    if selected is not None:
        if selected.status == ShipmentStatus.closed:
            return None, False, ["Referenced shipment is closed; review the shipment association."]
        for field, country in (("origin", origin_country), ("destination", destination_country)):
            document_value = getattr(extraction, field)
            shipment_value = getattr(selected, field)
            existing_country = getattr(selected, field + "_country") or country_from_location(shipment_value)
            if document_value and field not in reviewed and (
                    (country and existing_country and country != existing_country)
                    or _location_key(document_value, country) != _location_key(shipment_value, existing_country)):
                return None, False, [
                    f"Route conflict: document {field} {document_value!r} differs from shipment "
                    f"{selected.reference} {shipment_value!r}. Existing records were not reassigned.",
                ]
        selected = selected.model_copy(update={
            "origin_country": selected.origin_country or country_from_location(selected.origin),
            "destination_country": selected.destination_country or country_from_location(selected.destination),
        })
        return selected, False, [f"Matched shipment: {selected.reference}"]
    if not extraction.origin or not extraction.destination or {"origin", "destination"} & reviewed:
        return None, False, ["Origin/destination are missing or ungrounded; review the document before creating a shipment."]
    matches = [s for s in candidates if s.status != ShipmentStatus.closed
               and origin_country and destination_country
               and (s.origin_country or country_from_location(s.origin)) == origin_country
               and (s.destination_country or country_from_location(s.destination)) == destination_country
               and _location_key(s.origin, origin_country) == _location_key(extraction.origin, origin_country)
               and _location_key(s.destination, destination_country) == _location_key(extraction.destination, destination_country)]
    if len(matches) > 1:
        return None, False, ["Several shipments match this route; select the correct shipment with --shipment."]
    if matches:
        return matches[0].model_copy(update={
            "origin_country": origin_country, "destination_country": destination_country,
        }), False, [f"Automatically matched route to shipment: {matches[0].reference}"]
    fields = {"origin", "destination", "origin_country", "destination_country",
              "cargo_description", "weight_kg", "volume_m3"}
    evidence = [dict(e.model_dump(mode="json"), source_filename=item.original_filename)
                for e in extraction.field_evidence if e.field_name in fields
                and snippet_grounded(e.snippet, item.raw_text)]
    shipment = Shipment(
        reference=_next_reference(repos),
        name=route_label(extraction.origin, extraction.destination)[:200],
        origin=extraction.origin, destination=extraction.destination,
        origin_country=origin_country, destination_country=destination_country,
        route_category=f"{origin_country}->{destination_country}" if origin_country and destination_country else None,
        cargo_description=extraction.cargo_description if "cargo_description" not in reviewed and extraction.cargo_description else "",
        weight_kg=extraction.weight_kg if "weight_kg" not in reviewed else None,
        volume_m3=extraction.volume_m3 if "volume_m3" not in reviewed else None,
        target_currency=extraction.currency or "EUR", status=ShipmentStatus.needs_review,
        source_intake_item_id=item.id, route_evidence_json=json.dumps(evidence, ensure_ascii=False),
    )
    return shipment, True, [
        f"New shipment proposed: {shipment.reference}; category: {shipment.route_category or 'unknown'}.",
        "Human review required: freight shipment approve last -y",
    ]


def edit_proposed_shipment(settings: Settings, id_or_reference: str, **changes) -> Shipment:
    permitted = {"origin", "destination", "origin_country", "destination_country", "cargo_description",
                 "weight_kg", "volume_m3", "name", "target_currency"}
    if not changes or set(changes) - permitted:
        raise ValidationFailureError("Supply at least one supported shipment correction.")
    with with_session(settings) as repos, repos.atomic():
        shipment = repos.get_shipment(id_or_reference)
        if shipment.status != ShipmentStatus.needs_review:
            raise ValidationFailureError("Only unapproved shipment proposals can be corrected.")
        for field in ("origin_country", "destination_country"):
            if field in changes:
                changes[field] = normalize_country(changes[field])
            elif field.removesuffix("_country") in changes:
                changes[field] = country_from_location(changes[field.removesuffix("_country")])
        for field in ("origin", "destination", "name"):
            if field in changes and not changes[field].strip():
                raise ValidationFailureError(f"{field} cannot be empty.")
        for field in ("weight_kg", "volume_m3"):
            if field in changes and (not changes[field].is_finite() or changes[field] <= 0):
                raise ValidationFailureError(f"{field} must be positive.")
        currency = changes.get("target_currency")
        if currency is not None and (len(currency) != 3 or not currency.isalpha() or not currency.isupper()):
            raise ValidationFailureError("Currency must be an uppercase three-letter code.")
        shipment = shipment.model_copy(update={**changes, "updated_at": datetime.now(timezone.utc).replace(microsecond=0)})
        repos.update_shipment(shipment)
        repos.add_audit(AuditEvent(entity_type="shipment", entity_id=shipment.id, action="proposal_corrected",
                                   details_json=json.dumps({"fields": sorted(changes)})))
        return shipment


def approve_proposed_shipment(settings: Settings, id_or_reference: str,
                              expected_updated_at: datetime) -> Shipment:
    with with_session(settings) as repos, repos.atomic():
        shipment = repos.get_shipment(id_or_reference)
        if shipment.updated_at != expected_updated_at:
            raise ValidationFailureError("Shipment changed during review; review the current proposal again.")
        if shipment.status != ShipmentStatus.needs_review:
            raise ValidationFailureError("Shipment is not an unapproved proposal.")
        if not shipment.origin.strip() or not shipment.destination.strip() or not shipment.origin_country or not shipment.destination_country:
            raise ValidationFailureError("Confirm both route addresses and countries before approval; use freight shipment edit.")
        normalize_country(shipment.origin_country)
        normalize_country(shipment.destination_country)
        shipment.status = ShipmentStatus.open
        shipment.approved_at = datetime.now(timezone.utc).replace(microsecond=0)
        shipment.updated_at = shipment.approved_at
        repos.update_shipment(shipment)
        repos.add_audit(AuditEvent(entity_type="shipment", entity_id=shipment.id, action="proposal_approved",
                                   details_json=json.dumps({"category": f"{shipment.origin_country}->{shipment.destination_country}"})))
        return shipment


def reject_proposed_shipment(settings: Settings, id_or_reference: str, reason: str | None = None) -> Shipment:
    """Close a proposed route so it never matches future documents (audited)."""
    with with_session(settings) as repos, repos.atomic():
        shipment = repos.get_shipment(id_or_reference)
        if shipment.status != ShipmentStatus.needs_review:
            raise ValidationFailureError("Only unapproved proposals can be rejected.")
        shipment.status = ShipmentStatus.closed
        shipment.updated_at = datetime.now(timezone.utc).replace(microsecond=0)
        repos.update_shipment(shipment)
        repos.add_audit(AuditEvent(entity_type="shipment", entity_id=shipment.id, action="proposal_rejected",
                                   details_json=json.dumps({"reason": reason})))
        return shipment
