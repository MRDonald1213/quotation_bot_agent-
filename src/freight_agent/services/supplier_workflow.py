from __future__ import annotations

import csv
import json
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from decimal import Decimal, InvalidOperation
from email.utils import parseaddr
from typing import Callable
from uuid import uuid4

from sqlalchemy import select

from ..config import Settings
from ..db.engine import get_session
from ..db.models import (AuditEventRow, CarrierRow, FieldEvidenceRow, GapRow, GmailAccountRow,
                         GmailMessageRow, IntakeItemRow, QuotationRow, SupplierChatRow,
                         SupplierDatabaseRow, SupplierQuotationActionRow, utcnow_iso)
from ..domain.exceptions import FreightAgentError, NotFoundError, ValidationFailureError

logger = logging.getLogger("freight_agent.supplier_workflow")

PLACEHOLDER_VALUE = re.compile(r"(?:\[[^\]]*\]|<[^<>]*>|\{[^}]*\}|\.\.\.+)[\s.,;:-]*")
PLACEHOLDER_WORDS = {"", "n/a", "na", "tbd", "none", "null", "unknown", "-", "tbc"}


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    from .supplier_databases import _parse_csv

    headers, rows, _malformed = _parse_csv(path)
    return headers, rows


def _database_text(database: SupplierDatabaseRow) -> str:
    schema = json.loads(database.schema_json)
    aliases = json.loads(database.routing_aliases_json)
    return " ".join([database.name, database.description or "", database.category or "", *(aliases), *(field.get("name", "") for field in schema)]).lower()


def route_databases(settings: Settings, text: str) -> tuple[list[dict], str | None, float]:
    """Route every confirmed quotation to the one canonical supplier master."""
    from .supplier_databases import (SUPPLIER_MASTER_FILE, connect_supplier_database_path,
                                     refresh_canonical_supplier_schema)

    with get_session(settings) as session:
        row = session.scalar(select(SupplierDatabaseRow).where(
            SupplierDatabaseRow.status == "active",
            SupplierDatabaseRow.csv_path == str(SUPPLIER_MASTER_FILE.resolve()),
        ).order_by(SupplierDatabaseRow.created_at).limit(1))
        database_id = row.id if row else None
        database_name = row.name if row else None
    if database_id is None and SUPPLIER_MASTER_FILE.is_file():
        try:
            connected = connect_supplier_database_path(
                settings, str(SUPPLIER_MASTER_FILE), name="Supplier Master Database",
                description="One unified supplier and freight quotation master table.",
                category="Supplier master", routing_aliases=["freight quotation", "supplier quote", "carrier offer"],
            )
            database_id, database_name = connected["id"], connected["name"]
        except Exception as exc:
            logger.warning("canonical supplier master could not be connected: %s", type(exc).__name__)
            database_id = database_name = None
    elif database_id is not None:
        refresh_canonical_supplier_schema(settings, database_id)
        with get_session(settings) as session:
            refreshed = session.get(SupplierDatabaseRow, database_id)
            database_name = refreshed.name if refreshed else database_name
    if database_id is None:
        with get_session(settings) as session:
            others = session.scalars(select(SupplierDatabaseRow).where(
                SupplierDatabaseRow.status == "active")).all()
            for other in others:
                other.status = "disconnected"
                other.updated_at = utcnow_iso()
                session.add(AuditEventRow(id=str(uuid4()), entity_type="supplier_database", entity_id=other.id,
                    action="retired_missing_canonical_master",
                    details_json=json.dumps({"canonical_file": str(SUPPLIER_MASTER_FILE)}), created_at=other.updated_at))
            if others:
                session.commit()
        return [], None, 0.0
    with get_session(settings) as session:
        others = session.scalars(select(SupplierDatabaseRow).where(
            SupplierDatabaseRow.status == "active", SupplierDatabaseRow.id != database_id
        )).all()
        for other in others:
            other.status = "disconnected"
            other.updated_at = utcnow_iso()
            session.add(AuditEventRow(id=str(uuid4()), entity_type="supplier_database", entity_id=other.id,
                action="superseded_by_single_supplier_master",
                details_json=json.dumps({"master_database_id": database_id}), created_at=other.updated_at))
        if others:
            session.commit()
    routing = [{"database_id": database_id, "name": database_name, "score": 1.0,
                "reason": "Single canonical supplier master selected; supplier and quotation fields share one schema."}]
    return routing, database_id, 1.0


def _normalize_aliases(field: dict) -> list[str]:
    names = [field["name"].replace("_", " "), field.get("label", "")]
    if field["name"] == "Supplier_Name":
        names.extend(["Supplier", "Vendor", "Company"])
    if field["name"] == "Primary_Contact_Email":
        names.extend(["Email", "Contact Email"])
    if field["name"] == "Category":
        names.append("Service")
    canonical_aliases = {
        "Quotation_Valid_Until": ["Valid until", "Valid through", "Expiry date", "Expiration date"],
        "Quoted_Price": ["Quoted price", "Total price", "Freight price", "Price"],
    }
    names.extend(canonical_aliases.get(field["name"], []))
    names.extend(field.get("aliases", []) or [])
    return list(dict.fromkeys(name.strip().lower() for name in names if name.strip()))


def _extract_field(field: dict, text: str) -> tuple[str | None, str | None]:
    lowered = text.lower()
    aliases = _normalize_aliases(field)
    for alias in sorted(aliases, key=len, reverse=True):
        escaped = re.escape(alias)
        patterns = [
            rf"(?im)^\s*{escaped}\s*[:=\-]\s*(.+?)\s*$",
            rf"(?is)\b{escaped}\b[^\n\d]{{0,20}}([€£$¥]?\s*[\d][\d,\.]*(?:\s*/\s*[A-Za-z]+)?)",
            rf"(?is)\b{escaped}\b[^\n:{{0,40}}]{{0,40}}[:\-=]\s*([^\n]+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                value = match.group(1).strip().strip(".;")
                if value:
                    snippet = text[max(0, match.start()-40):min(len(text), match.end()+40)].replace("\n", " ").strip()
                    return value, snippet[:300]
    return None, None


def _validate_field(field: dict, value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    # a form template writes "Company Name: [Provider Name]"; that is an empty field, not a value
    if PLACEHOLDER_VALUE.fullmatch(value) or value.strip("-–—/\\. ").casefold() in PLACEHOLDER_WORDS:
        return None
    field_type = field.get("type", "text")
    if field_type in {"number", "currency"}:
        numeric_match = re.search(r"(?:[€£$¥]\s*)?([0-9][0-9,]*(?:\.[0-9]+)?)", value)
        if not numeric_match:
            return None
        candidate = numeric_match.group(1).replace(",", "")
        try:
            Decimal(candidate)
        except InvalidOperation:
            return None
        value = candidate
    if field_type == "date":
        if re.match(r"^\d{4}-\d{2}-\d{2}$", value):
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                return None
    return value


# Column meaning, matched on whole words so "OTD_Rating_Pct" is never read as a price.
_CONCEPT_TERMS: dict[str, tuple[str, ...]] = {
    "supplier_name": ("carrier name", "supplier name", "vendor name", "company name",
                      "carrier", "supplier", "vendor", "company", "operator"),
    "contact_email": ("email address", "contact email", "e-mail", "email", "contact"),
    "pricing_unit": ("pricing unit", "price basis", "unit", "basis"),
    "origin": ("origin", "pickup", "loading location"),
    "destination": ("destination", "delivery location"),
    "fuel_type": ("fuel type",),
    "contact_person": ("contact person", "contact name"),
    "contact_phone": ("phone", "telephone", "contact phone"),
    "dimensions": ("dimensions", "cargo dimensions"),
    "category": ("category", "transport mode", "transport type", "service type"),
    "lane": ("lanes", "lane", "routes", "route", "regions", "region", "corridor"),
    "price": ("unit price", "base price", "freight rate", "rate per", "freight", "price",
              "rate", "cost", "tariff"),
    "currency": ("currency",),
    "charge": ("surcharge", "fuel adjustment", "handling fee", "documentation fee"),
    "transit_days": ("transit days", "transit time", "lead time", "delivery time", "transit"),
    "valid_until": ("valid until", "validity", "expiration date", "expiry", "valid"),
    "delivery_scope": ("delivery scope", "incoterm", "door to door", "port to port",
                       "ddp", "dap", "exw", "fob", "cif", "scope"),
    "insurance_included": ("insurance", "cargo cover"),
    "customs_included": ("customs", "clearance", "duty"),
    "included_services": ("included services", "services included", "inclusions"),
    "excluded_services": ("excluded services", "services excluded", "exclusions"),
    "weight": ("gross weight", "weight", "tonnage"),
    "volume": ("volume", "cbm", "cubic"),
    "quote_reference": ("quote reference", "quotation number", "quote number", "reference"),
}

# Only offer details a supplier can answer belong in a clarification email.
ASKABLE_CONCEPTS: frozenset[str] = frozenset({
    "price", "currency", "transit_days", "valid_until", "delivery_scope", "insurance_included",
    "customs_included", "included_services", "excluded_services", "weight", "volume",
})
# Asked even when the connected database marks no column required.
_CRITICAL_CONCEPTS: frozenset[str] = frozenset({
    "price", "currency", "transit_days", "valid_until", "delivery_scope",
    "insurance_included", "customs_included",
})

def _field_text(field: dict) -> str:
    return " ".join([field["name"].replace("_", " "), str(field.get("label", "")),
                     str(field.get("description", "")), *(field.get("aliases") or [])]).lower()


def _concept_of(field: dict) -> str | None:
    """The meaning behind a column name, or None when the column is internal administration."""
    text = _field_text(field)
    tokens = set(re.findall(r"[a-z0-9]+", text))
    if tokens & {"id", "code", "no"} and not tokens & {"name", "reference", "number"}:
        return "record_id"
    best: tuple[str, int] | None = None
    for concept, phrases in _CONCEPT_TERMS.items():
        for phrase in phrases:
            if len(phrase) > (best[1] if best else 0) and re.search(rf"\b{re.escape(phrase)}\b", text):
                best = (concept, len(phrase))
                break
    return best[0] if best else None


def _yes_no(value: int | None) -> str | None:
    return None if value is None else ("yes" if value else "no")


def _listed(raw: str | None) -> str:
    items = [str(item).strip() for item in json.loads(raw or "[]") if str(item).strip()]
    return ", ".join(items)


def _quotation_facts_unfiltered(session, quote: QuotationRow | None) -> dict[str, tuple[str, str]]:
    """What workflow A already established for this document, keyed by meaning instead of column.

    Every value carries its grounded evidence snippet, so a supplier is never asked to repeat
    something we can already read from their own quotation.
    """
    if quote is None:
        return {}
    from . import money
    from .quotation_naming import place

    snippets = {row.field_name: row.snippet for row in
                session.scalars(select(FieldEvidenceRow).where(FieldEvidenceRow.quotation_id == quote.id))}

    def grounded(field: str, fallback: str) -> str:
        return snippets.get(field) or fallback

    facts: dict[str, tuple[str, str]] = {}
    carrier = session.get(CarrierRow, quote.carrier_id) if quote.carrier_id else None
    if carrier:
        facts["supplier_name"] = (carrier.name, "Supplier record matched to this quotation")
    if quote.base_price:
        amount = money.to_decimal(quote.base_price)
        facts["price"] = ((money.format_amount(amount, quote.currency) if amount is not None
                           else str(quote.base_price)),
                          grounded("base_price", "Base price of this quotation"))
    charges = _listed(quote.charges_json)
    if charges:
        facts["charge"] = (charges, grounded("charges", "Additional charges of this quotation"))
    if quote.currency:
        facts["currency"] = (quote.currency, grounded("currency", "Currency of this quotation"))
    if quote.transit_days_min or quote.transit_days_max:
        low, high = quote.transit_days_min, quote.transit_days_max
        facts["transit_days"] = ((f"{low}-{high} days" if low and high and low != high
                                  else f"{low or high} days"),
                                 grounded("transit_days", "Transit time of this quotation"))
    if quote.valid_until:
        facts["valid_until"] = (quote.valid_until, grounded("valid_until", "Validity of this quotation"))
    if quote.delivery_scope:
        facts["delivery_scope"] = (str(quote.delivery_scope).replace("_", " "),
                                   grounded("delivery_scope", "Delivery scope of this quotation"))
    if _yes_no(quote.insurance_included):
        facts["insurance_included"] = (_yes_no(quote.insurance_included),
                                       grounded("insurance_included", "Insurance coverage of this quotation"))
    if _yes_no(quote.customs_included):
        facts["customs_included"] = (_yes_no(quote.customs_included),
                                     grounded("customs_included", "Customs handling of this quotation"))
    for concept, stored in (("origin", quote.origin), ("destination", quote.destination)):
        if stored:
            facts[concept] = (stored, grounded(concept, f"{concept.capitalize()} of this quotation"))
    included, excluded = _listed(quote.included_services_json), _listed(quote.excluded_services_json)
    if included:
        facts["included_services"] = (included, grounded("included_services", "Services covered by this quotation"))
    if excluded:
        facts["excluded_services"] = (excluded, grounded("excluded_services", "Services not covered by this quotation"))
    if quote.quote_reference:
        facts["quote_reference"] = (quote.quote_reference, grounded("quote_reference", "Reference of this quotation"))
    if quote.origin or quote.destination:
        facts["lane"] = (f"{place(quote.origin)} to {place(quote.destination)}", "Route read from this quotation")
    details = json.loads(quote.details_json or "{}")
    mapping = {"supplier_id": "record_id", "transport_type": "category", "weight_kg": "weight", "volume_m3": "volume"}
    for field, value in details.items():
        if value is not None:
            facts[mapping.get(field, field)] = (str(value), grounded(field, ""))
    return facts


def _quotation_facts(session, quote: QuotationRow | None) -> dict[str, tuple[str, str]]:
    """Only accepted evidence grounded in a linked source can supply extracted facts."""
    if quote is None:
        return {}
    from .validation import snippet_grounded
    from ..db.models import SupplierReplyRow

    source_ids = {quote.intake_item_id}
    source_ids.update(session.scalars(select(SupplierReplyRow.intake_item_id).where(
        SupplierReplyRow.quotation_id == quote.id)).all())
    sources = session.scalars(select(IntakeItemRow).where(IntakeItemRow.id.in_(source_ids))).all()
    from ..db.models import SourceDocumentRow
    parts = session.scalars(select(SourceDocumentRow).where(SourceDocumentRow.intake_item_id.in_(source_ids))).all()
    accepted = {}
    for evidence in session.scalars(select(FieldEvidenceRow).where(
            FieldEvidenceRow.quotation_id == quote.id, FieldEvidenceRow.validation_status == "accepted")):
        if any(source.original_filename == evidence.source_filename
               and snippet_grounded(evidence.snippet, source.raw_text) for source in sources) or any(
                   part.filename == evidence.source_filename and snippet_grounded(evidence.snippet, part.text) for part in parts):
            accepted[evidence.field_name] = evidence.snippet
    field_map = {"supplier_name": "carrier_name", "price": "base_price",
                 "charge": "charges", "transit_days": "transit_days", "record_id": "supplier_id",
                 "category": "transport_type", "weight": "weight_kg", "volume": "volume_m3"}
    result = {}
    for concept, (value, _) in _quotation_facts_unfiltered(session, quote).items():
        source_field = field_map.get(concept, concept)
        snippet = accepted.get(source_field)
        if concept == "transit_days":
            snippet = snippet or accepted.get("transit_days_min") or accepted.get("transit_days_max")
        if snippet:
            result[concept] = (value, snippet)
    return result


def _match_database_record(schema: list[dict], proposed: dict[str, str], text: str,
                           database: SupplierDatabaseRow) -> tuple[dict[str, str], dict[str, str], dict[str, str] | None]:
    database_path = Path(database.csv_path)
    if not database_path.is_file():
        return {}, {}, None
    headers, rows = _read_csv(database_path)
    schema_names = {field["name"] for field in schema}
    supplier_fields = {
        "Supplier_ID", "Supplier_Name", "Category", "Primary_Lanes_Regions",
        "Service_Contract_Type", "Compliance_Status", "Supplier_Default_Payment_Terms",
        "Primary_Contact_Email", "OTD_Rating_Pct", "Billing_Accuracy_Pct",
    } & schema_names
    proposed_supplier = str(proposed.get("Supplier_Name") or "").strip().lower()
    if not proposed_supplier:
        for key, value in proposed.items():
            if "supplier" in key.lower() or "company" in key.lower() or "vendor" in key.lower():
                proposed_supplier = str(value).strip().lower()
                break
    if not proposed_supplier:
        source_lower = text.lower()
        for row in rows:
            supplier = str(row.get("Supplier_Name") or row.get("supplier_name") or "").strip()
            if supplier and supplier.lower() in source_lower:
                matched = {key: str(row.get(key) or "").strip() for key in supplier_fields if str(row.get(key) or "").strip()}
                return matched, {
                    key: f"Matched supplier master field for {supplier}; not quotation evidence"
                    for key in matched
                }, row

    normalize = lambda value: re.sub(r"[^a-z0-9]", "", str(value or "").casefold())
    matches = []
    for row in rows:
        supplier = str(row.get("Supplier_Name") or row.get("supplier_name") or "").strip().lower()
        if supplier and proposed_supplier and normalize(supplier) == normalize(proposed_supplier):
            matches.append(row)
    if not matches:
        return {}, {}, None
    unique_suppliers = {str(row.get("Supplier_ID") or row.get("supplier_id") or "") for row in matches}
    unique_suppliers.discard("")
    if len(unique_suppliers) > 1:
        return {}, {}, None
    match = next((row for row in matches if not str(row.get("Quotation_ID") or "").strip()), matches[0])
    matched = {key: str(match.get(key) or "").strip() for key in supplier_fields if str(match.get(key) or "").strip()}
    evidence = {key: f"Matched supplier master field for {match.get('Supplier_Name', '')}; not quotation evidence" for key in matched}
    return matched, evidence, match


def _action_payloads(session, actions: list[SupplierQuotationActionRow]) -> list[dict]:
    from ..db.models import IntakeItemRow
    from .quotation_naming import name_for_intake

    database_ids = {action.database_id for action in actions if action.database_id}
    names = {row.id: row.name for row in session.scalars(
        select(SupplierDatabaseRow).where(SupplierDatabaseRow.id.in_(database_ids))).all()} if database_ids else {}
    intake_ids = {action.intake_id for action in actions}
    files = {row.id: row.original_filename for row in session.scalars(
        select(IntakeItemRow).where(IntakeItemRow.id.in_(intake_ids))).all()} if intake_ids else {}
    labels = {intake_id: name_for_intake(session, intake_id) for intake_id in files}
    payloads = [{"id": action.id, "intake_id": action.intake_id, "file": files.get(action.intake_id),
             "name": labels.get(action.intake_id) or files.get(action.intake_id),
             "database_id": action.database_id, "database_name": names.get(action.database_id) if action.database_id else None,
             "status": action.status, "routing": json.loads(action.routing_json),
             "proposed_row": json.loads(action.proposed_row_json), "missing_fields": json.loads(action.missing_fields_json),
             "evidence": json.loads(action.evidence_json), "notes": action.notes,
             "created_csv_row": json.loads(action.created_csv_row_json) if action.created_csv_row_json else None,
             "created_at": action.created_at, "updated_at": action.updated_at}
            for action in actions]
    for payload in payloads:
        payload["database_matches"] = {
            key: value for key, value in payload["proposed_row"].items()
            if str(payload["evidence"].get(key, "")).startswith("Matched existing")
        }
        payload["extracted_fields"] = {
            key: value for key, value in payload["proposed_row"].items()
            if key not in payload["database_matches"] and payload["evidence"].get(key)
            and not str(payload["evidence"][key]).startswith("Operator correction")
        }
        # A draft asks the offer's open gaps, not the empty columns of the supplier record.
        try:
            payload["questions"] = _open_questions(session, payload)
        except Exception:  # noqa: BLE001 — a listing must still render if one action cannot be re-read
            payload["questions"] = []
    return payloads


def list_actions(settings: Settings) -> list[dict]:
    with get_session(settings) as session:
        actions = session.scalars(select(SupplierQuotationActionRow).order_by(SupplierQuotationActionRow.created_at.desc())).all()
        return _action_payloads(session, list(actions))


def get_action(settings: Settings, action_id: str) -> dict:
    for action in list_actions(settings):
        if action["id"] == action_id:
            return action
    raise NotFoundError("supplier quotation action not found")


def action_for_intake(settings: Settings, intake_id: str) -> dict | None:
    """The latest analysis of one inbound document, for the page that shows the email."""
    with get_session(settings) as session:
        actions = session.scalars(select(SupplierQuotationActionRow)
                                  .where(SupplierQuotationActionRow.intake_id == intake_id)
                                  .order_by(SupplierQuotationActionRow.created_at.desc())).all()
        payloads = _action_payloads(session, list(actions))
    return payloads[0] if payloads else None


def _mark_intake(session, intake, *, processing: str) -> None:
    """Keep the document row honest: the page reads this state, not the action table.

    Classification belongs to the freight-lane workflow and is deliberately left alone here.
    """
    intake.processing_status = processing
    session.add(intake)


def _inbound_sender(session, intake) -> tuple[str | None, GmailMessageRow | None]:
    """The address the quotation arrived from, plus the Gmail message behind it when there is one."""
    message = session.get(GmailMessageRow, intake.gmail_message_id) if intake.gmail_message_id else None
    for raw in ((message.sender if message else None), intake.sender):
        _, address = parseaddr(str(raw or ""))
        address = (address or "").strip().lower()
        if address and re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", address):
            return address, message
    return None, message


def process_supplier_quotation(settings: Settings, intake_id: str, database_id: str | None = None) -> dict:
    with get_session(settings) as session:
        intake = session.get(IntakeItemRow, intake_id)
        if intake is None:
            raise NotFoundError("quotation document not found")
        text = intake.raw_text
        if database_id:
            database = session.get(SupplierDatabaseRow, database_id)
            if database is None or database.status != "active":
                raise NotFoundError("supplier database not found")
            routing = [{"database_id": database.id, "name": database.name, "score": 1.0, "reason": "Confirmed manually for this quotation"}]
            chosen_id = database.id
        else:
            routing, chosen_id, _ = route_databases(settings, text)
            database = session.get(SupplierDatabaseRow, chosen_id) if chosen_id else None

        now = utcnow_iso()
        existing = session.scalar(select(SupplierQuotationActionRow).where(SupplierQuotationActionRow.intake_id == intake_id))
        if existing:
            action = existing
        else:
            action = SupplierQuotationActionRow(id=str(uuid4()), intake_id=intake_id, created_at=now)
        source_quote = session.scalar(select(QuotationRow).where(QuotationRow.intake_item_id == intake_id))
        if existing is not None and existing.status == "record_created" and not (
                source_quote and source_quote.version > 1 and source_quote.review_status == "extracted"):
            return get_action(settings, existing.id)
        if source_quote and source_quote.review_status == "approved":
            source_quote.review_status = "extracted"
            source_quote.reviewed_at = None

        if database is None:
            action.database_id = None
            action.status = "needs_human_review"
            action.proposed_row_json = "{}"
            action.missing_fields_json = "[]"
            action.evidence_json = "{}"
            action.notes = "No unique database could be identified. Confirm a connected supplier database before analysis."
            action.routing_json = json.dumps(routing)
            action.updated_at = now
            session.add(action)
            _mark_intake(session, intake, processing="needs_review")
            session.add(AuditEventRow(id=str(uuid4()), entity_type="supplier_quotation_action", entity_id=action.id,
                                      action="database_routing_pending", details_json=json.dumps(routing), created_at=now))
            session.commit()
            return get_action(settings, action.id)

        schema = json.loads(database.schema_json)
        # Workflow A already read the offer by meaning; the label scan is only the fallback.
        facts = _quotation_facts(session, session.scalar(
            select(QuotationRow).where(QuotationRow.intake_item_id == intake_id)))
        proposed: dict[str, str] = {}
        evidence: dict[str, str] = {}
        missing: list[dict[str, str]] = []

        for field in schema:
            name = field["name"]
            label = field.get("label", name)
            concept = _concept_of(field)
            raw, snippet = facts.get(concept, (None, None)) if concept else (None, None)
            value = _validate_field(field, raw) if raw else None
            if not value:
                raw, snippet = _extract_field(field, text)
                value = _validate_field(field, raw) if raw else None
            if value:
                proposed[name] = value
                evidence[name] = snippet or "Source value found in quotation"
                continue
            required = bool(field.get("required"))
            ask_supplier = concept in ASKABLE_CONCEPTS and (required or concept in _CRITICAL_CONCEPTS)
            missing.append({
                "field": name, "label": label, "required": required, "ask_supplier": ask_supplier,
                "reason": (f"{label} is an offer detail this comparison needs and the quotation does not state it"
                           if ask_supplier else
                           "Internal field of the supplier record; completed by our team, never asked of the supplier"),
            })

        matched_row, matched_evidence, existing_match = _match_database_record(schema, proposed, text, database)
        for name, value in matched_row.items():
            proposed.setdefault(name, value)
            evidence.setdefault(name, matched_evidence.get(name, "Matched existing supplier record"))
        if existing_match:
            supplier_name = existing_match.get("Supplier_Name") or existing_match.get("supplier_name")
            if supplier_name:
                notes_match = f"Existing supplier record matched: {supplier_name}"
            else:
                notes_match = "Existing supplier record matched in the connected database"
        else:
            notes_match = "No existing supplier record matched"

        askable_missing = [item for item in missing if item["ask_supplier"]]
        required_missing = [item for item in missing if item["required"]]
        # Open gaps of the offer are questions too, whatever the columns of the supplier record say.
        open_points = _open_questions(session, {"intake_id": intake_id, "missing_fields": askable_missing})

        if not proposed:
            status = "needs_human_review"
            notes = f"{notes_match}. Nothing in this document could be matched to the supplier record."
        elif open_points:
            status = "needs_clarification"
            notes = (f"{notes_match}. Only the supplier can answer "
                     + ", ".join(str(point.get("label") or point.get("field")) for point in open_points) + ".")
        elif required_missing:
            status = "needs_human_review"
            notes = (f"{notes_match}. Internal field(s) are still empty for this record: "
                     + ", ".join(item["label"] for item in required_missing) + ".")
        else:
            status = "ready_for_approval"
            notes = (f"{notes_match}. Nothing the supplier needs to answer is missing"
                     + (f"; {len(missing)} internal field(s) can be completed on approval." if missing else "."))
        action.database_id = database.id
        action.status = status
        action.routing_json = json.dumps(routing)
        action.proposed_row_json = json.dumps(proposed)
        action.missing_fields_json = json.dumps(missing)
        action.evidence_json = json.dumps(evidence)
        action.notes = notes
        action.created_csv_row_json = None
        action.updated_at = now
        session.add(action)
        _mark_intake(session, intake,
                     processing="processed" if status == "ready_for_approval" else "needs_review")
        session.add(AuditEventRow(id=str(uuid4()), entity_type="supplier_quotation_action", entity_id=action.id,
                                  action="proposed",
                                  details_json=json.dumps({"database_id": database.id, "missing_count": len(missing),
                                                           "askable_count": len(askable_missing), "status": status}),
                                  created_at=now))
        session.commit()
        return get_action(settings, action.id)


def update_proposal(settings: Settings, action_id: str, proposed_row: dict[str, str], notes: str | None = None) -> dict:
    with get_session(settings) as session:
        action = session.get(SupplierQuotationActionRow, action_id)
        if action is None:
            raise NotFoundError("supplier quotation action not found")
        if action.status == "record_created":
            raise ValidationFailureError("an approved record cannot be changed after CSV creation")
        previous = json.loads(action.proposed_row_json or "{}")
        evidence = json.loads(action.evidence_json or "{}")
        for field, value in proposed_row.items():
            if previous.get(field) != value:
                evidence[field] = "Operator correction; not extracted from quotation"
        action.proposed_row_json = json.dumps(proposed_row)
        action.evidence_json = json.dumps(evidence)
        database = session.get(SupplierDatabaseRow, action.database_id) if action.database_id else None
        if database:
            missing = [{"field": f["name"], "label": f.get("label", f["name"]), "required": True,
                        "ask_supplier": False, "reason": "Destination requires this internal field"}
                       for f in json.loads(database.schema_json) if f.get("required") and not str(proposed_row.get(f["name"]) or "").strip()]
            action.missing_fields_json = json.dumps(missing)
            questions = _open_questions(session, {"intake_id": action.intake_id, "missing_fields": []})
            action.status = "needs_clarification" if questions else "needs_human_review" if missing else "ready_for_approval"
        quote = session.scalar(select(QuotationRow).where(QuotationRow.intake_item_id == action.intake_id))
        if quote and quote.review_status == "approved":
            quote.review_status = "extracted"
            quote.reviewed_at = None
        action.notes = notes or action.notes
        action.updated_at = utcnow_iso()
        session.add(AuditEventRow(id=str(uuid4()), entity_type="supplier_quotation_action", entity_id=action.id,
            action="proposal_corrected", details_json=json.dumps({"fields": list(proposed_row), "notes": notes}), created_at=utcnow_iso()))
        session.add(action)
        session.commit()
    return get_action(settings, action_id)


def _append_row(settings: Settings, database: SupplierDatabaseRow, row: dict[str, str]) -> tuple[dict[str, str], str]:
    path = Path(database.csv_path)
    if not path.is_file():
        raise NotFoundError("connected CSV file is missing")
    headers, existing = _read_csv(path)
    schema_names = [field["name"] for field in json.loads(database.schema_json)]
    if set(headers) != set(schema_names):
        raise ValidationFailureError("connected CSV header no longer matches its saved schema")
    normalized = {header: str(row.get(header) or "").strip() for header in headers}
    normalized_key = tuple(normalized[name] for name in headers)
    for record in existing:
        if tuple(str(record.get(name) or "").strip() for name in headers) == normalized_key:
            return normalized, "already present"

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(path.stem + f".backup-{timestamp}" + path.suffix)
    shutil.copy2(path, backup)
    try:
        with path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=headers)
            if path.stat().st_size == 0:
                writer.writeheader()
            writer.writerow(normalized)
            handle.flush()
    except Exception:
        shutil.copy2(backup, path)
        raise
    return normalized, str(backup)


def generate_proposal_clarification_drafts(settings: Settings, proposal_id: str,
                                           proposal: dict) -> list[dict]:
    """Persist one supplier-confirmation and one missing-information draft per proposal."""
    from ..db.models import DestinationProposalRow, SupplierEmailDraftRow
    from .gmail_drafts import _address, _validate_content

    result = proposal
    has_previous_reply = bool(result.get("reply_history"))
    schema = {str(field.get("name")): field for field in proposal.get("schema", [])}
    values = proposal.get("values", {}) or {}
    confidence = proposal.get("confidence", {}) or {}
    explanations = proposal.get("explanations", {}) or {}
    evidence = proposal.get("evidence", {}) or {}
    uncertain_names = [name for name in (proposal.get("uncertain") or []) if name in schema]
    missing_names = [name for name in (proposal.get("missing") or []) if name in schema]
    field_specs = (
        ("supplier_confirmation", "Supplier confirmation", uncertain_names),
        ("missing_information", "Missing quotation information", missing_names),
    )

    with get_session(settings) as session:
        proposal_row = session.get(DestinationProposalRow, proposal_id)
        if proposal_row is None:
            raise NotFoundError("Quotation proposal not found")
        intake = session.get(IntakeItemRow, proposal_row.intake_id)
        message = session.get(GmailMessageRow, intake.gmail_message_id) if intake and intake.gmail_message_id else None
        account = session.get(GmailAccountRow, message.account_id) if message else None
        recipient = _address(message.sender if message else None)
        if not recipient:
            recipient = _address(str(values.get("Supplier_Contact_Email") or values.get("Primary_Contact_Email") or ""))
        if not account or not recipient:
            # This workflow is entered from mailbox intake. A file without a linked
            # mailbox sender cannot produce a sendable supplier email yet.
            return []
        subject_root = str(values.get("Quotation_Reference") or values.get("Quotation_ID") or
                           (intake.subject if intake else "Freight quotation"))
        supplier_name = str(values.get("Supplier_Name") or recipient)
        generated: list[dict] = []
        existing_rows = session.query(SupplierEmailDraftRow).filter(
            SupplierEmailDraftRow.destination_proposal_id == proposal_id).order_by(
                SupplierEmailDraftRow.created_at.desc()).all()
        now = utcnow_iso()
        for draft_type, title, field_names in field_specs:
            active = next((row for status in ("draft", "approved", "cancelled") for row in existing_rows
                           if row.draft_type == draft_type and row.status == status), None)
            if not field_names:
                if active and active.status in {"draft", "approved"}:
                    active.status = "cancelled"
                    active.updated_at = now
                    session.add(AuditEventRow(id=str(uuid4()), entity_type="supplier_email_draft",
                        entity_id=active.id, action="superseded_after_supplier_reply",
                        details_json=json.dumps({"destination_proposal_id": proposal_id, "draft_type": draft_type}),
                        created_at=now))
                continue
            # A saved draft is user-owned content. Keep it while the proposal is
            # unchanged; a new supplier reply advances the proposal timestamp and
            # allows the unanswered fields to be refreshed.
            if active and active.status == "draft" and active.updated_at >= proposal_row.updated_at:
                generated.append({"id": active.id, "draft_type": draft_type, "subject": active.subject,
                                  "recipient": active.recipient, "status": "draft"})
                continue
            lines = []
            for index, name in enumerate(field_names, start=1):
                field = schema[name]
                label = str(field.get("label") or name.replace("_", " "))
                if draft_type == "supplier_confirmation":
                    candidate = str(values.get(name) or "candidate not extracted")
                    score = confidence.get(name)
                    score_text = f" ({round(float(score) * 100)}% confidence)" if score is not None else ""
                    reason = str(explanations.get(name) or "The quotation wording is ambiguous.")
                    source_excerpt = str(evidence.get(name) or "")
                    lines.append(f"{index}. {label}: {candidate}{score_text}. {reason}" +
                                 (f' Source excerpt: “{source_excerpt}”' if source_excerpt else "") +
                                 f"\n   Could you please confirm the {label.casefold()} for this quotation?")
                else:
                    lines.append(f"{index}. {label}: Could you please provide or confirm this information?")
            if draft_type == "supplier_confirmation":
                if has_previous_reply:
                    intro = (f"Thank you for your reply to our earlier questions about quotation {subject_root} from {supplier_name}. "
                             "Your previous reply did not fully resolve the questions below. We have reanalyzed the offer, "
                             "but these details are still unclear or inconsistent with the quotation. "
                             "Please provide a more detailed answer to each point so we can complete the review:")
                else:
                    intro = (f"We are reviewing quotation {subject_root} from {supplier_name}. "
                             "The quotation contains the following details, but the source is not conclusive:")
            else:
                if has_previous_reply:
                    intro = (f"Thank you for your reply to our earlier questions about quotation {subject_root} from {supplier_name}. "
                             "Your previous reply did not fully answer the questions below. We have reanalyzed the offer, "
                             "but these details are still missing or unclear. Please provide a more detailed response "
                             "to each point so we can complete the quotation review:")
                else:
                    intro = (f"We are reviewing quotation {subject_root} from {supplier_name}. "
                             "The following required details were not found in the quotation:")
            body = "Hello,\n\n" + intro + "\n\n" + "\n\n".join(lines) + \
                   "\n\nThank you for your help.\n\nKind regards,\nFreight AI Agent"
            subject = f"{'Follow-up: ' if has_previous_reply else ''}{title}: {subject_root}"
            _validate_content(recipient, "", subject, body)
            if active and active.status == "approved" and active.recipient == recipient and \
                    active.subject == subject and active.body == body:
                generated.append({"id": active.id, "draft_type": draft_type, "subject": active.subject,
                                  "recipient": active.recipient, "status": "approved"})
                continue
            if active is None:
                active = SupplierEmailDraftRow(
                    id=str(uuid4()), destination_proposal_id=proposal_id, draft_type=draft_type,
                    requested_fields_json=json.dumps(field_names),
                    gmail_account_id=account.id, gmail_thread_id=message.gmail_thread_id,
                    bound_recipient=recipient, recipient=recipient, cc="", subject=subject, body=body,
                    status="draft", created_at=now, updated_at=now,
                )
                session.add(active)
                action = "created"
            else:
                active.status = "draft"
                active.requested_fields_json = json.dumps(field_names)
                active.approved_recipient = None
                active.immutable_approved_body = None
                active.approved_subject = None
                active.gmail_account_id = account.id
                active.gmail_thread_id = message.gmail_thread_id
                active.bound_recipient = recipient
                active.recipient = recipient
                active.cc = ""
                active.subject = subject
                active.body = body
                active.updated_at = now
                action = "refreshed"
            session.add(AuditEventRow(id=str(uuid4()), entity_type="supplier_email_draft",
                entity_id=active.id, action=action,
                details_json=json.dumps({"destination_proposal_id": proposal_id, "draft_type": draft_type,
                                         "fields": field_names}), created_at=now))
            generated.append({"id": active.id, "draft_type": draft_type, "subject": subject,
                              "recipient": recipient, "status": "draft"})
        session.commit()
        return generated


def approve_action(settings: Settings, action_id: str, corrected_row: dict[str, str] | None = None, notes: str | None = None) -> dict:
    """Approval records a verdict only; destination writes require a separate import."""
    with get_session(settings) as session:
        action = session.get(SupplierQuotationActionRow, action_id)
        if action is None:
            raise NotFoundError("Supplier quotation action not found")
        if action.status in {"approved", "record_created"}:
            return get_action(settings, action_id)
        if action.status != "ready_for_approval":
            raise ValidationFailureError("Resolve missing information before approval")
        if corrected_row:
            proposal = json.loads(action.proposed_row_json)
            proposal.update(corrected_row)
            action.proposed_row_json = json.dumps(proposal)
        action.status = "approved"
        action.updated_at = utcnow_iso()
        action.notes = notes or "Approved; import is a separate action"
        session.add(AuditEventRow(id=str(uuid4()), entity_type="supplier_quotation_action", entity_id=action.id,
            action="human_approved", details_json="{}", created_at=utcnow_iso()))
        session.commit()
    return get_action(settings, action_id)


def import_action(settings: Settings, action_id: str, corrected_row: dict[str, str] | None = None, notes: str | None = None) -> dict:
    from sqlalchemy import text
    with get_session(settings) as session:
        # Serialize CSV changes with the SQLite writer lock, including cross-process imports.
        session.execute(text("BEGIN IMMEDIATE"))
        action = session.get(SupplierQuotationActionRow, action_id)
        if action is None:
            raise NotFoundError("supplier quotation action not found")
        if action.status == "record_created":
            return get_action(settings, action_id)
        if action.status != "approved":
            raise ValidationFailureError("Approve the proposal before importing")
        source_quote = session.scalar(select(QuotationRow).where(QuotationRow.intake_item_id == action.intake_id))
        if source_quote and (source_quote.disposition or source_quote.review_status not in {"approved", "imported"}):
            raise ValidationFailureError("Approve the canonical quotation before importing the destination record")
        if not action.database_id:
            raise ValidationFailureError("confirm a supplier database before import")
        database = session.get(SupplierDatabaseRow, action.database_id)
        if database is None or database.status != "active":
            raise NotFoundError("supplier database not found")
        proposed = json.loads(action.proposed_row_json)
        if corrected_row and any(proposed.get(field) != value for field, value in corrected_row.items()):
            raise ValidationFailureError("Changed destination values require a new approval before import")
        missing = json.loads(action.missing_fields_json)
        if missing:
            schema_names = {field["name"]: field for field in json.loads(database.schema_json)}
            unresolved = [item for item in missing if item.get("required", True) and item["field"] in schema_names
                          and not str(proposed.get(item["field"]) or "").strip()]
            if unresolved:
                raise ValidationFailureError("required fields remain unresolved: " + ", ".join(item["label"] for item in unresolved))
        for field in json.loads(database.schema_json):
            value = str(proposed.get(field["name"]) or "").strip()
            if value and _validate_field(field, value) is None:
                raise ValidationFailureError(f"Invalid destination field: {field.get('label') or field['name']}")
        normalized, backup = _append_row(settings, database, proposed)
        now = utcnow_iso()
        action.status = "record_created"
        action.proposed_row_json = json.dumps(normalized)
        action.created_csv_row_json = json.dumps(normalized)
        action.notes = notes or "Record approved and appended to the connected CSV after human approval."
        action.updated_at = now
        database.record_count = _read_csv(Path(database.csv_path))[1].__len__()
        database.updated_at = now
        session.add(database)
        session.add(action)
        session.add(AuditEventRow(id=str(uuid4()), entity_type="supplier_quotation_action", entity_id=action.id,
                                  action="csv_record_created", details_json=json.dumps({"backup": backup, "row": normalized}), created_at=now))
        session.commit()
    return get_action(settings, action_id)


def _valid_address(value: str | None) -> str | None:
    _, address = parseaddr(str(value or ""))
    address = (address or "").strip().lower()
    return address if re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", address) else None


def _open_questions(session, action: dict) -> list[dict]:
    """The offer details this quotation genuinely lacks, in the order a supplier should read them.

    Open gaps from the validated extraction come first: they are already filtered to
    comparison-critical fields and already exclude waived and answered points. Only when no
    extraction exists does the connected database's own askable columns contribute questions.
    """
    from .gap_analysis import ASKABLE_FIELDS, askable_question, canonical_field

    questions: list[dict] = []
    seen: set[str] = set()
    quote = session.scalar(select(QuotationRow).where(QuotationRow.intake_item_id == action["intake_id"]))
    if quote is not None:
        for gap in session.scalars(select(GapRow).where(GapRow.quotation_id == quote.id,
                                                        GapRow.status == "open")).all():
            field = canonical_field(gap.field_name)
            if field not in ASKABLE_FIELDS or gap.severity == "informational" or field in seen:
                continue
            seen.add(field)
            questions.append({"field": field,
                              "label": None,
                              "reason": gap.description,
                              "question": (gap.question_text or askable_question(field)).strip()})
    for item in action.get("missing_fields") or []:
        if not item.get("ask_supplier") or item["field"] in seen:
            continue
        seen.add(item["field"])
        concept = _concept_of({"name": item["field"], "label": item.get("label", "")})
        questions.append({**item, "question": askable_question(concept or item["field"])})
    return questions


def _template_text(supplier_name: str, questions: list[dict]) -> tuple[str, str]:
    """Plain business English used when the local model cannot word the request."""
    salutation = f"Dear {supplier_name}," if supplier_name else "Hello,"
    lines = [f"{index}. {q.get('question') or q.get('label') or q.get('field')}."
             for index, q in enumerate(questions, 1)]
    body = (f"{salutation}\n\nThank you for your quotation. So that we can compare and book it, "
            "could you confirm these points:\n\n" + "\n".join(lines)
            + "\n\nA short reply with these points is enough.\n\nKind regards")
    subject = f"Questions about your quotation - {supplier_name}" if supplier_name else "Questions about your quotation"
    return subject[:120], body


def _draft_text(settings: Settings, supplier_name: str, questions: list[dict],
                extracted: dict[str, str]) -> tuple[str, str, str | None]:
    from ..agents.supplier_draft_agent import word_draft
    from ..domain.exceptions import ModelUnavailableError

    try:
        wordings = word_draft(settings, supplier_name=supplier_name, missing=questions, extracted=extracted)
        return wordings.subject.strip(), wordings.body.strip(), None
    except ModelUnavailableError as exc:
        subject, body = _template_text(supplier_name, questions)
        return subject, body, str(exc)


def _append_action_note(settings: Settings, action_id: str, note: str) -> None:
    with get_session(settings) as session:
        action = session.get(SupplierQuotationActionRow, action_id)
        if action is None:
            return
        action.notes = f"{action.notes or ''} {note}".strip()
        action.updated_at = utcnow_iso()
        session.add(action)
        session.commit()


_SUPPLIER_KEY = re.compile(r"(supplier|carrier|vendor|company)", re.I)
_RECORD_ID_VALUE = re.compile(r"(?:\d+|[A-Z]{2,}-\d+)")


def _supplier_name(proposed: dict[str, str]) -> str:
    """The trading name to write to, chosen by column meaning rather than a fixed column name."""
    ranked = sorted([(0 if "name" in key.lower() else 1, key) for key in proposed if _SUPPLIER_KEY.search(key)],
                    key=lambda item: item[0])
    for _, key in ranked:
        value = str(proposed[key]).strip()
        if value and "@" not in value and not _RECORD_ID_VALUE.fullmatch(value):
            return value
    return ""


def _write_supplier_draft(settings: Settings, action: dict, questions: list[dict]) -> dict:
    from .gmail_drafts import upsert_action_draft

    with get_session(settings) as session:
        account = session.query(GmailAccountRow).filter(GmailAccountRow.status == "connected").order_by(GmailAccountRow.updated_at.desc()).first()
        if account is None:
            raise ValidationFailureError("Connect a Gmail account before creating an email draft")
        account_id = account.id
        intake = session.get(IntakeItemRow, action["intake_id"])
        if intake is None:
            raise NotFoundError("quotation document not found")
        inbound_sender, message = _inbound_sender(session, intake)
        thread_id = message.gmail_thread_id if message else None

    proposed = action.get("proposed_row") or {}
    # A reply always goes back to whoever sent us the quotation, never to an address quoted inside it.
    recipient = inbound_sender or _valid_address(proposed.get("Primary_Contact_Email"))
    if not recipient:
        raise ValidationFailureError("This quotation has no sender address to reply to, so no draft was created.")
    subject, body, wording_note = _draft_text(settings, _supplier_name(proposed), questions, proposed)
    draft = upsert_action_draft(settings, supplier_action_id=action["id"], recipient=recipient,
                                bound_recipient=inbound_sender, cc="", subject=subject, body=body,
                                gmail_account_id=account_id, gmail_thread_id=thread_id)
    if wording_note:
        _append_action_note(settings, action["id"], wording_note)
    return {**draft, "wording_note": wording_note, "questions": questions}


def create_supplier_review_draft(settings: Settings, action_id: str) -> dict:
    action = get_action(settings, action_id)
    if action["status"] == "record_created":
        raise ValidationFailureError("this quotation is already recorded, so no clarification is needed")
    with get_session(settings) as session:
        questions = _open_questions(session, action)
    if not questions:
        raise ValidationFailureError(
            "Nothing needs to be asked of this supplier — every comparison field is already on the quotation.")
    return _write_supplier_draft(settings, action, questions)


def _classifier_agreed_it_is_an_offer(settings: Settings, intake_id: str) -> bool:
    """True when the parked document's own classification says this is a freight quotation.

    The mailbox already decided the email is an offer before the document was opened. A document
    classifier that agrees but scores under the review threshold must not leave the operator with an
    empty price line; the model's agreement clears the gate, its score does not.
    """
    with get_session(settings) as session:
        row = session.get(IntakeItemRow, intake_id)
        if row is None or (row.classification_status or "") != "needs_review":
            return False
        try:
            return bool(json.loads(row.classification_json or "{}").get("is_freight_quote"))
        except (TypeError, ValueError):
            return False


def _ensure_quotation_extracted(settings: Settings, intake_id: str,
                                progress: Callable[[str], None] | None = None) -> tuple[str | None, list[str]]:
    """Run workflow A here when this document has no structured quotation yet, so nobody has to start it.

    A failed or uncertain extraction leaves a readable review result. Database schema matching
    must not masquerade as a validated quotation when canonical extraction did not succeed.
    """
    from .extraction import extract_and_store_quote

    def stored() -> str | None:
        with get_session(settings) as session:
            row = session.scalar(select(QuotationRow).where(QuotationRow.intake_item_id == intake_id))
            return row.id if row else None

    quote_id = stored()
    if quote_id is not None:
        return quote_id, []
    if progress:
        progress("extracting the quotation fields with the local model…")
    override = False  # Uncertain classification requires an explicit human override.
    try:
        summary = extract_and_store_quote(settings, intake_id, force=override)
    except FreightAgentError as exc:
        return None, [f"Field extraction did not complete: {exc}"]
    except Exception as exc:  # noqa: BLE001 — the local model failing must still leave a readable result
        logger.warning("quotation extraction failed for %s: %s", intake_id, type(exc).__name__, exc_info=True)
        return None, [f"The local model did not complete the extraction ({type(exc).__name__}); "
                      "review the source before retrying."]
    return (str(summary.quotation_id) if summary.quotation_id else None), list(summary.notes)


def run_quotation_pipeline(settings: Settings, intake_id: str, database_id: str | None = None,
                           progress: Callable[[str], None] | None = None) -> dict:
    """Route first, then extract independently against every matching destination schema."""
    from .destination_proposals import process
    return process(settings, intake_id, database_id, progress)


def set_action_status(settings: Settings, action_id: str, status: str, notes: str | None = None) -> dict:
    allowed = {"needs_clarification", "needs_human_review", "ready_for_approval", "rejected"}
    if status not in allowed:
        raise ValidationFailureError("unsupported supplier quotation status")
    with get_session(settings) as session:
        action = session.get(SupplierQuotationActionRow, action_id)
        if action is None:
            raise NotFoundError("supplier quotation action not found")
        if action.status == "record_created":
            raise ValidationFailureError("a created record cannot change status")
        action.status = status
        action.notes = notes or action.notes
        action.updated_at = utcnow_iso()
        session.add(action)
        session.add(AuditEventRow(id=str(uuid4()), entity_type="supplier_quotation_action", entity_id=action.id,
                                  action="status_changed", details_json=json.dumps({"status": status}), created_at=action.updated_at))
        session.commit()
    return get_action(settings, action_id)


def _numeric(value: str | None) -> Decimal | None:
    if value is None:
        return None
    candidate = re.sub(r"^[€£$¥]", "", value.replace(",", "").strip())
    candidate = candidate.split("/")[0].strip()
    try:
        return Decimal(candidate)
    except InvalidOperation:
        return None


# --------------------------------------------------------------------- chat agent
# Ranking, prices and dates are computed in Python; the model only phrases the answer.

_FREIGHT_TERMS = (
    "quot", "price", "pricing", "rate", "cost", "charge", "carrier", "supplier", "vendor",
    "freight", "shipping", "shipment", "cargo", "container", "truck", "rail", "fcl", "lcl",
    "lane", "route", "transit", "lead time", "delivery", "customs", "clearance", "insurance",
    "ddp", "dap", "exw", "fob", "cif", "door", "port", "warehouse", "currency", "usd", "eur",
    "cny", "valid", "expiry", "expire", "includ", "exclud", "surcharge", "teu", "cbm", "weight",
    "volume", "tonnage", "consign", "booking", "demurrage", "detention", "origin",
    "destination", "compare", "comparison", "cheapest", "fastest", "quickest", "offer", "bid",
    "tender", "forwarder", "logistics", "record", "database", "incoterm",
)
_OFF_TOPIC_REPLY = (
    "I only work with freight quotations, so that one is outside what I can help with. "
    "Ask me which quotation is cheapest, which supplier is fastest, whose offer stays valid "
    "longest, or which option I would put forward.")


def _on_topic(question_l: str, fleet: list[dict]) -> bool:
    if any(term in question_l for term in _FREIGHT_TERMS):
        return True
    # "Is TransEast in our records?" names a supplier instead of a freight column.
    return any(item["supplier"] and item["supplier"].lower() in question_l for item in fleet)


def _intent(question_l: str) -> str:
    def has(*words: str) -> bool:
        return any(word in question_l for word in words)

    if has("valid", "expiry", "expire") and has("long", "latest", "until", "period", "still", "time"):
        return "validity"
    if has("cheap", "lowest", "minimum", "budget") and has("fast", "quick", "delivery"):
        return "value"
    if has("slow"):
        return "slowest"
    if has("fast", "quick", "shortest", "speedy", "least time"):
        return "fastest"
    if has("expensive", "highest", "dearest", "costliest"):
        return "dearest"
    if has("cheap", "lowest", "minimum", "budget", "price"):
        return "cheapest"
    if has("best", "recommend", "should", "winner", "top", "favourite", "favorite", "worth"):
        return "best"
    if has("how many", "how much", "count", "list", "what do we have", "which suppliers"):
        return "inventory"
    return "records"


def _fleet(settings: Settings, session) -> list[dict]:
    """The current version of every quotation we hold, with its USD view pre-computed."""
    from . import money
    from .quotation_naming import name_for_quotation, place

    latest: dict[str, QuotationRow] = {}
    for row in session.scalars(select(QuotationRow)).all():
        current = latest.get(row.intake_item_id)
        if current is None or int(row.version or 1) > int(current.version or 1):
            latest[row.intake_item_id] = row
    fleet = []
    for quote in latest.values():
        carrier = session.get(CarrierRow, quote.carrier_id) if quote.carrier_id else None
        total = money.to_decimal(quote.total_known_cost)
        base = money.to_decimal(quote.base_price)
        usd_total, _ = money.usd(total, quote.currency, settings)
        usd_base, _ = money.usd(base, quote.currency, settings)
        try:
            valid_until = datetime.strptime(quote.valid_until, "%Y-%m-%d").date() if quote.valid_until else None
        except ValueError:
            valid_until = None
        name = name_for_quotation(session, quote.id)
        fleet.append({
            "id": quote.id, "name": name,
            "supplier": (carrier.name if carrier else None) or name.split(" — ")[0],
            "route": _route_phrase(place(quote.origin), place(quote.destination)),
            "currency": money.normalize_currency(quote.currency), "base": base, "total": total,
            "usd_base": usd_base, "usd_total": usd_total,
            "transit": quote.transit_days_max if quote.transit_days_max is not None else quote.transit_days_min,
            "valid_until": valid_until, "scope": quote.delivery_scope,
            "insurance": _yes_no(quote.insurance_included), "customs": _yes_no(quote.customs_included),
            "comparable": quote.comparison_status == "comparable",
        })
    return fleet


def _route_phrase(origin: str, destination: str) -> str:
    if origin and destination:
        return f"{origin} to {destination}"
    return origin or destination or "route not stated"


def _usd(value: Decimal) -> str:
    from . import money
    return money.format_amount(value, "USD")


def _priced(item: dict) -> str:
    """The quote in its own currency, with the USD reference beside it."""
    from . import money
    if item["total"] is None or item["usd_total"] is None:
        return "price not stated" if item["base"] is None else _priced_base(item)
    original = money.format_amount(item["total"], item["currency"])
    if (item["currency"] or "").upper() == "USD":
        return original
    return f"{original} — {_usd(item['usd_total'])}"


def _priced_base(item: dict) -> str:
    from . import money
    if item["base"] is None or item["usd_base"] is None:
        return "price not stated"
    original = money.format_amount(item["base"], item["currency"])
    if (item["currency"] or "").upper() == "USD":
        return original
    return f"{original} — {_usd(item['usd_base'])}"


def _cite(item: dict, reason: str) -> dict:
    return {"kind": "quotation", "quotation_id": item["id"], "name": item["name"], "reason": reason}


def _ranked_by_price(fleet: list[dict], highest: bool = False) -> list[dict]:
    priced = [f for f in fleet if f["usd_total"] is not None]
    return sorted(priced, key=lambda f: f["usd_total"], reverse=highest)


def _sentence(clauses: list[str]) -> str:
    """Join findings into proper sentences — the plain text is also the model's contract."""
    kept = [clause.strip().rstrip(".") for clause in clauses if clause and clause.strip()]
    return ". ".join(kept) + "." if kept else ""


def _finding_price(fleet: list[dict], *, highest: bool) -> tuple[str, list[dict]]:
    ranked = _ranked_by_price(fleet, highest)
    if not ranked:
        return ("No quotation on file has a price we can compare yet, so I cannot rank them."), []
    winner = ranked[0]
    word = "highest" if highest else "lowest"
    cites = [_cite(winner, f"{word} known total in USD")]
    clauses = [f"{winner['name']} is the {word} price at {_priced(winner)}"
               + (f" on {winner['route']}" if winner["route"] else "")]
    runners = ranked[1:3]
    if runners:
        clauses.append("Then " + ", ".join(f"{r['name']} at {_priced(r)}" for r in runners))
        cites += [_cite(r, "next priced quotation") for r in runners]
    unpriced = len(fleet) - len(ranked)
    if unpriced:
        clauses.append(f"{unpriced} quotation(s) have no usable price and were left out")
    return _sentence(clauses), cites


def _finding_best(fleet: list[dict]) -> tuple[str, list[dict]]:
    ranked = [f for f in _ranked_by_price(fleet) if f["comparable"]]
    if not ranked:
        return (f"I cannot put an option forward yet. None of the {len(fleet)} quotation(s) on file "
                "is complete enough to compare — a price, the delivery scope or the currency is still "
                "missing from each one."), []
    first = ranked[0]
    cites = [_cite(first, "lowest comparable total in USD")]
    clauses = [f"{first['name']} looks like the best option at {_priced(first)}"
               + (f" for {first['route']}" if first["route"] else "")]
    if len(ranked) > 1 and ranked[1]["usd_total"] is not None and first["usd_total"] is not None:
        gap = ranked[1]["usd_total"] - first["usd_total"]
        clauses.append(f"{ranked[1]['name']} is second, {_usd(gap)} above it")
        cites.append(_cite(ranked[1], "second lowest comparable total"))
    quickest = min((f for f in ranked if f["transit"] is not None), key=lambda f: f["transit"], default=None)
    if quickest is not None and quickest["id"] != first["id"]:
        clauses.append(f"If speed matters more, {quickest['name']} quotes the shortest transit "
                       f"at {quickest['transit']} days")
        cites.append(_cite(quickest, "shortest transit among comparable quotations"))
    extras = [f for f in ranked if f["insurance"] == "yes" and f["id"] != first["id"]]
    if extras and first["insurance"] != "yes":
        clauses.append(f"{extras[0]['name']} is the only one including insurance")
    dropped = len(fleet) - len(ranked)
    if dropped:
        clauses.append(f"{dropped} quotation(s) are not comparable yet and were not ranked")
    return _sentence(clauses), cites


def _finding_value(fleet: list[dict]) -> tuple[str, list[dict]]:
    price_find, price_cites = _finding_price(fleet, highest=False)
    speed_find, speed_cites = _finding_speed(fleet, slowest=False)
    return price_find + " " + speed_find, price_cites + speed_cites


def _finding_speed(fleet: list[dict], *, slowest: bool = False) -> tuple[str, list[dict]]:
    timed = [f for f in fleet if f["transit"] is not None]
    if not timed:
        return ("No quotation on file states a transit time, so I cannot say who is fastest."), []
    ordered = sorted(timed, key=lambda f: f["transit"], reverse=slowest)
    winner = ordered[0]
    word = "slowest" if slowest else "fastest"
    cites = [_cite(winner, f"{word} stated transit")]
    clauses = [f"{winner['name']} is the {word} at {winner['transit']} days"
               + (f" on {winner['route']}" if winner["route"] else "")]
    runners = [o for o in ordered[1:3] if o["transit"] != winner["transit"]]
    if runners:
        clauses.append("Then " + ", ".join(f"{r['name']} at {r['transit']} days" for r in runners))
        cites += [_cite(r, "next stated transit") for r in runners]
    missing = len(fleet) - len(timed)
    if missing:
        clauses.append(f"{missing} quotation(s) did not state a transit time")
    return _sentence(clauses), cites


def _finding_validity(fleet: list[dict]) -> tuple[str, list[dict]]:
    dated = [f for f in fleet if f["valid_until"] is not None]
    if not dated:
        return ("No quotation on file states how long the offer stays valid, so I cannot rank that."), []
    winner = max(dated, key=lambda f: f["valid_until"])
    cites = [_cite(winner, "latest validity date")]
    clauses = [f"{winner['name']} holds longest, valid until {winner['valid_until'].isoformat()}"
               + (f" for {winner['route']}" if winner["route"] else "")]
    soonest = min(dated, key=lambda f: f["valid_until"])
    if soonest["id"] != winner["id"]:
        clauses.append(f"The one to watch is {soonest['name']}, valid only until "
                       f"{soonest['valid_until'].isoformat()}")
        cites.append(_cite(soonest, "earliest validity date"))
    missing = len(fleet) - len(dated)
    if missing:
        clauses.append(f"{missing} quotation(s) gave no expiry")
    return _sentence(clauses), cites


def _finding_inventory(fleet: list[dict]) -> tuple[str, list[dict]]:
    if not fleet:
        return ("There are no quotations on file yet, so there is nothing to compare."), []
    by_supplier: dict[str, int] = {}
    for item in fleet:
        by_supplier[item["supplier"]] = by_supplier.get(item["supplier"], 0) + 1
    priced = len([f for f in fleet if f["usd_total"] is not None])
    comparable = len([f for f in fleet if f["comparable"]])
    found = (f"We hold {len(fleet)} quotation(s) from {len(by_supplier)} supplier(s). "
             f"{priced} carry a usable price and {comparable} are ready to compare.")
    lanes = [f"{item['name']} ({item['route']})" for item in fleet[:6] if item["route"]]
    if lanes:
        found += " On the lanes we have: " + "; ".join(lanes) + "."
    return found, [_cite(f, "quotation on file") for f in fleet[:6]]


def _finding_records(settings: Settings, question_l: str, database_id: str | None) -> tuple[str, list[dict]]:
    """Supplier-master lookups stay keyword-driven: the CSV is the source, and rows are cited by number."""
    with get_session(settings) as session:
        query = select(SupplierDatabaseRow).where(SupplierDatabaseRow.status == "active")
        if database_id:
            query = query.where(SupplierDatabaseRow.id == database_id)
        databases = session.scalars(query.order_by(SupplierDatabaseRow.name)).all()
        if not databases:
            return "No supplier database is connected yet, so I have nothing to look up in one.", []
        if any(word in question_l for word in ("last added", "latest supplier", "most recent", "recently added")):
            latest = databases[-1]
            _, rows = _read_csv(Path(latest.csv_path))
            if not rows:
                return f"{latest.name} has no supplier records yet.", []
            row_number = len(rows) + 1
            return (f"The most recent record is the last row of {latest.name}, on CSV row {row_number}: "
                    f"{_row_summary(rows[-1])}."), [
                {"kind": "database_row", "database_id": latest.id, "database_name": latest.name,
                 "csv_row": row_number, "row": rows[-1], "reason": "most recent record"}]
        price_find, price_cites = _csv_price_rank(question_l, databases)
        if price_find:
            return price_find, price_cites
    stopwords = {"which", "supplier", "suppliers", "quotation", "quotations", "offer", "offers", "from", "transportation", "transport", "records", "show", "list", "have", "does", "with", "that", "what", "about"}
    terms = [term for term in re.findall(r"[a-z0-9]+", question_l) if len(term) >= 3 and term not in stopwords]
    route_match = re.search(r"\bfrom\s+(.+?)\s+to\s+(.+?)[?.]?$", question_l.strip())
    matches = []
    for database in databases:
        _, rows = _read_csv(Path(database.csv_path))
        for row_index, row in enumerate(rows, start=2):
            haystack = " ".join(str(value) for value in row.values()).lower()
            if (route_match and all(place.strip() in haystack for place in route_match.groups())) or (not route_match and terms and any(term in haystack for term in terms)):
                matches.append((database, row_index, row))
    cites = [{"kind": "database_row", "database_id": database.id, "database_name": database.name,
              "csv_row": row_index, "row": row, "reason": "record matching your words"}
             for database, row_index, row in matches[:5]]
    if matches:
        shown = "; ".join(_row_summary(row) for _, _, row in matches[:3])
        return (f"I found {len(matches)} record(s) in {databases[0].name if len(databases) == 1 else 'the connected databases'} "
                f"that match your words: {shown}."), cites
    return ("Nothing in the connected supplier databases matches that. Try a supplier name, a lane or a "
            "service you know is on record."), []


def _row_summary(row: dict[str, str]) -> str:
    named = [(str(key), str(value)) for key, value in row.items()
             if str(value).strip() and str(value).strip().lower() not in PLACEHOLDER_WORDS]
    shown = named[:4]
    return "; ".join(f"{key.replace('_', ' ').lower()}: {value}" for key, value in shown) or "record with empty cells"


def _csv_price_rank(question_l: str, databases: list[SupplierDatabaseRow]) -> tuple[str | None, list[dict]]:
    """Rank the price columns of the supplier master itself, when a question is about the records."""
    wants_low = any(word in question_l for word in ("cheap", "lowest", "minimum", "budget"))
    wants_high = any(word in question_l for word in ("expensive", "highest", "dearest", "costliest"))
    if not (wants_low or wants_high):
        return None, []
    candidates = []
    for database in databases:
        _, rows = _read_csv(Path(database.csv_path))
        for field in json.loads(database.schema_json):
            field_name = str(field.get("name", "")).lower()
            if field.get("type") != "currency" and not any(w in field_name for w in ("price", "rate", "cost")):
                continue
            for row_index, row in enumerate(rows, start=2):
                number = _numeric(row.get(field["name"]))
                if number is not None:
                    candidates.append((number, database, field, row_index, row))
    if not candidates:
        return ("None of the connected supplier databases has a numeric price column to rank.", [])
    bases = set()
    for _, database, field, _, row in candidates:
        normalized = {key.casefold(): str(value).strip().casefold() for key, value in row.items()}
        currency = normalized.get("currency")
        unit = normalized.get("unit") or normalized.get("pricing_unit")
        if not currency or not unit:
            return "A fair price comparison needs an explicit currency and pricing unit for every record.", []
        bases.add((currency, unit, field["name"], normalized.get("origin"), normalized.get("destination")))
    if len(bases) != 1:
        return "These records have different currencies, pricing units, routes or price fields; select a compatible group before ranking.", []
    number, database, field, row_index, row = (max if wants_high else min)(candidates, key=lambda item: item[0])
    word = "highest" if wants_high else "lowest"
    identity = next((str(value) for key, value in row.items() if any(term in key.lower() for term in ("supplier_name", "carrier_name", "vendor_name", "company_name"))), "The selected supplier")
    normalized_row = {key.casefold(): str(value) for key, value in row.items()}
    currency, unit = normalized_row.get("currency", ""), normalized_row.get("unit") or normalized_row.get("pricing_unit", "")
    return (f"{identity} offers the {word} recorded rate at {number} {currency} per {unit} "
            f"among the compatible records in {database.name}. This compares the recorded rate only; quotation eligibility still requires review."), [
        {"kind": "database_row", "database_id": database.id, "database_name": database.name,
         "csv_row": row_index, "row": row, "reason": f"{word} price column value"}]


def chat_supplier_databases(settings: Settings, question: str, database_id: str | None = None) -> dict:
    """Answer a freight question from the quotations we hold; the model only ever supplies tone."""
    from ..agents.chat_agent import phrase
    from ..domain.exceptions import ModelUnavailableError

    if database_id is None:
        from .workspace import compare
        from .common import with_session
        with with_session(settings) as repos:
            shipments = repos.list_shipments()
        if len(shipments) != 1:
            return {"question": question, "answer": "Select a shipment/lane in Comparison or the Supplier Assistant before ranking quotations.",
                    "citations": [], "intent": "select_lane", "wording": "plain"}
        result = compare(settings, str(shipments[0].id), question)
        return {**result, "question": question, "answer": result["explanation"],
                "citations": [{"quotation_id": qid, "reason": result["explanation"]} for qid in result["selected_ids"]], "wording": "plain"}
    question_l = question.lower().strip()
    with get_session(settings) as session:
        fleet = _fleet(settings, session)
        on_topic = _on_topic(question_l, fleet)
    if not on_topic:
        findings, citations, intent = _OFF_TOPIC_REPLY, [], "off_topic"
    else:
        intent = _intent(question_l)
        builders = {
            "best": lambda: _finding_best(fleet),
            "cheapest": lambda: _finding_price(fleet, highest=False),
            "dearest": lambda: _finding_price(fleet, highest=True),
            "fastest": lambda: _finding_speed(fleet),
            "slowest": lambda: _finding_speed(fleet, slowest=True),
            "validity": lambda: _finding_validity(fleet),
            "value": lambda: _finding_value(fleet),
            "inventory": lambda: _finding_inventory(fleet),
        }
        if database_id:
            findings, citations = _finding_records(settings, question_l, database_id)
        elif intent in builders:
            findings, citations = builders[intent]()
        else:
            findings, citations = _finding_records(settings, question_l, database_id)
    wording = "plain"
    answer = findings
    if on_topic and intent != "off_topic" and database_id is None:
        try:
            answer = phrase(settings, question=question, findings=findings)
            wording = "local-model"
        except ModelUnavailableError as exc:
            logger.info("chat wording fell back to the plain answer: %s", exc)
    now = utcnow_iso()
    with get_session(settings) as session:
        chat = SupplierChatRow(id=str(uuid4()), question=question, answer=answer,
                               citations_json=json.dumps(citations), created_at=now)
        session.add(chat)
        session.commit()
    return {"question": question, "answer": answer, "citations": citations,
            "intent": intent, "wording": wording, "plain_answer": findings, "created_at": now}


def chat_history(settings: Settings, limit: int = 30) -> list[dict]:
    with get_session(settings) as session:
        rows = session.scalars(select(SupplierChatRow).order_by(SupplierChatRow.created_at.desc()).limit(max(1, min(limit, 100)))).all()
    return [{"id": row.id, "question": row.question, "answer": row.answer,
             "citations": json.loads(row.citations_json), "created_at": row.created_at} for row in rows]
