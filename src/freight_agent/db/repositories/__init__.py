from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Iterator, TypeVar
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ...domain.enums import (
    ClassificationStatus,
    ComparisonStatus,
    DeliveryScope,
    ProcessingStatus,
    ReviewStatus,
    ShipmentStatus,
    SourceType,
)
from ...domain.exceptions import DatabaseError, NotFoundError
from ...domain.models import (
    AuditEvent,
    Carrier,
    Charge,
    ClarificationDraft,
    DomainModel,
    FieldEvidence,
    Gap,
    IntakeItem,
    QuestionSet,
    Quotation,
    QuotationVersion,
    Shipment,
    SupplierReply,
)
from ...domain.money import money_to_text, text_to_money
from .. import models as m


def _u(value: str | UUID) -> str:
    return str(value) if not isinstance(value, str) else value


def _uid(value: str) -> UUID:
    return UUID(value)


def _audit_uid(value: str) -> UUID | str:
    """Keep malformed legacy audit identifiers readable without rewriting history."""
    try:
        return UUID(value)
    except (ValueError, TypeError, AttributeError):
        return value


# ------------------------------------------------------------------ shipments

def _route_category(origin_country: str | None, destination_country: str | None) -> str | None:
    if origin_country and destination_country:
        return f"{origin_country}->{destination_country}"
    return None


def shipment_to_row(s: Shipment) -> m.ShipmentRow:
    return m.ShipmentRow(
        id=_u(s.id), reference=s.reference, name=s.name, origin=s.origin,
        destination=s.destination, cargo_description=s.cargo_description,
        origin_country=s.origin_country, destination_country=s.destination_country,
        route_category=_route_category(s.origin_country, s.destination_country),
        source_intake_item_id=_u(s.source_intake_item_id) if s.source_intake_item_id else None,
        route_evidence_json=s.route_evidence_json,
        approved_at=s.approved_at.isoformat() if s.approved_at else None,
        weight_kg=money_to_text(s.weight_kg), volume_m3=money_to_text(s.volume_m3),
        required_delivery_scope=str(s.required_delivery_scope),
        target_currency=s.target_currency, notes=s.notes, status=str(s.status),
        created_at=s.created_at.isoformat(),
        updated_at=s.updated_at.isoformat(),
    )


def shipment_from_row(r: m.ShipmentRow) -> Shipment:
    return Shipment(
        id=_uid(r.id), reference=r.reference, name=r.name, origin=r.origin,
        destination=r.destination, cargo_description=r.cargo_description,
        origin_country=r.origin_country, destination_country=r.destination_country,
        route_category=_route_category(r.origin_country, r.destination_country),
        source_intake_item_id=_uid(r.source_intake_item_id) if r.source_intake_item_id else None,
        route_evidence_json=r.route_evidence_json,
        approved_at=m.iso_to_dt(r.approved_at),
        weight_kg=text_to_money(r.weight_kg), volume_m3=text_to_money(r.volume_m3),
        required_delivery_scope=DeliveryScope(r.required_delivery_scope),
        target_currency=r.target_currency, notes=r.notes, status=ShipmentStatus(r.status),
        created_at=m.iso_to_dt(r.created_at) or Shipment.model_fields["created_at"].default_factory(),
        updated_at=m.iso_to_dt(r.updated_at) or Shipment.model_fields["updated_at"].default_factory(),
    )


# --------------------------------------------------------------------- intake

def intake_to_row(i: IntakeItem, classification_json: str | None = None) -> m.IntakeItemRow:
    return m.IntakeItemRow(
        id=_u(i.id), shipment_id=_u(i.shipment_id) if i.shipment_id else None,
        source_type=str(i.source_type), original_filename=i.original_filename,
        stored_path=i.stored_path, content_sha256=i.content_sha256, sender=i.sender,
        subject=i.subject, received_at=i.received_at.isoformat() if i.received_at else None,
        raw_text=i.raw_text, file_size_bytes=i.file_size_bytes,
        page_or_sheet_info=i.page_or_sheet_info,
        classification_status=str(i.classification_status),
        classification_reason=i.classification_reason,
        classification_confidence=i.classification_confidence,
        classification_json=classification_json,
        processing_status=str(i.processing_status),
        created_at=i.created_at.isoformat(),
    )


def intake_from_row(r: m.IntakeItemRow) -> IntakeItem:
    return IntakeItem(
        id=_uid(r.id), shipment_id=_uid(r.shipment_id) if r.shipment_id else None,
        source_type=SourceType(r.source_type), original_filename=r.original_filename,
        stored_path=r.stored_path, content_sha256=r.content_sha256, sender=r.sender,
        subject=r.subject,
        received_at=m.iso_to_dt(r.received_at), raw_text=r.raw_text,
        file_size_bytes=r.file_size_bytes, page_or_sheet_info=r.page_or_sheet_info,
        classification_status=ClassificationStatus(r.classification_status),
        classification_reason=r.classification_reason,
        classification_confidence=r.classification_confidence,
        processing_status=ProcessingStatus(r.processing_status),
        created_at=m.iso_to_dt(r.created_at) or IntakeItem.model_fields["created_at"].default_factory(),
    )


# ------------------------------------------------------------------ quotation

def quotation_to_row(q: Quotation) -> m.QuotationRow:
    def _b(v: bool | None) -> int | None:
        return None if v is None else int(v)
    return m.QuotationRow(
        id=_u(q.id), shipment_id=_u(q.shipment_id),
        carrier_id=_u(q.carrier_id) if q.carrier_id else None,
        intake_item_id=_u(q.intake_item_id), quote_reference=q.quote_reference,
        details_json=q.details.model_dump_json(), disposition=q.disposition,
        disposition_reason=q.disposition_reason, archived_at=q.archived_at.isoformat() if q.archived_at else None,
        currency=q.currency, base_price=money_to_text(q.base_price),
        charges_json=json.dumps([c.model_dump(mode="json") for c in q.known_additional_charges]),
        included_services_json=json.dumps(q.included_services),
        excluded_services_json=json.dumps(q.excluded_services),
        assumptions_json=json.dumps(q.assumptions),
        origin=q.origin, destination=q.destination,
        transit_days_min=q.transit_days_min, transit_days_max=q.transit_days_max,
        valid_until=m.date_to_iso(q.valid_until), delivery_scope=str(q.delivery_scope) if q.delivery_scope else None,
        insurance_included=_b(q.insurance_included), customs_included=_b(q.customs_included),
        total_known_cost=money_to_text(q.total_known_cost),
        comparison_status=str(q.comparison_status),
        review_status=str(q.review_status),
        reviewed_at=q.reviewed_at.isoformat() if q.reviewed_at else None,
        imported_at=q.imported_at.isoformat() if q.imported_at else None,
        review_notes=q.review_notes,
        model_name=q.model_name, prompt_version=q.prompt_version,
        processed_at=q.processed_at.isoformat() if q.processed_at else None,
        created_at=q.created_at.isoformat(), updated_at=q.updated_at.isoformat(),
        version=q.version,
    )


def quotation_from_row(r: m.QuotationRow) -> Quotation:
    def _b(v: int | None) -> bool | None:
        return None if v is None else bool(v)
    return Quotation(
        id=_uid(r.id), shipment_id=_uid(r.shipment_id),
        carrier_id=_uid(r.carrier_id) if r.carrier_id else None,
        intake_item_id=_uid(r.intake_item_id), quote_reference=r.quote_reference,
        details=json.loads(r.details_json or "{}"), disposition=r.disposition,
        disposition_reason=r.disposition_reason, archived_at=m.iso_to_dt(r.archived_at),
        currency=r.currency, base_price=text_to_money(r.base_price),
        known_additional_charges=[Charge.model_validate(c) for c in json.loads(r.charges_json)],
        included_services=json.loads(r.included_services_json),
        excluded_services=json.loads(r.excluded_services_json),
        assumptions=json.loads(r.assumptions_json),
        origin=r.origin, destination=r.destination,
        transit_days_min=r.transit_days_min, transit_days_max=r.transit_days_max,
        valid_until=m.iso_to_date(r.valid_until),
        delivery_scope=DeliveryScope(r.delivery_scope) if r.delivery_scope else None,
        insurance_included=_b(r.insurance_included), customs_included=_b(r.customs_included),
        total_known_cost=text_to_money(r.total_known_cost),
        comparison_status=ComparisonStatus(r.comparison_status),
        review_status=ReviewStatus(r.review_status or "extracted"),
        reviewed_at=m.iso_to_dt(r.reviewed_at), imported_at=m.iso_to_dt(r.imported_at),
        review_notes=r.review_notes,
        model_name=r.model_name, prompt_version=r.prompt_version,
        processed_at=m.iso_to_dt(r.processed_at),
        created_at=m.iso_to_dt(r.created_at) or Quotation.model_fields["created_at"].default_factory(),
        updated_at=m.iso_to_dt(r.updated_at) or Quotation.model_fields["updated_at"].default_factory(),
        version=r.version,
    )


_Row = TypeVar("_Row", bound=m.Base)
_Model = TypeVar("_Model", bound=DomainModel)


def _workflow_to_row(entity: DomainModel, row_type: type[_Row]) -> _Row:
    values = entity.model_dump(mode="json")
    if isinstance(entity, QuestionSet):
        values["questions_json"] = json.dumps(values.pop("questions"))
    return row_type(**values)


def _workflow_from_row(row: m.Base, model_type: type[_Model]) -> _Model:
    values = {column.key: getattr(row, column.key) for column in row.__table__.columns}
    if model_type is QuestionSet:
        values["questions"] = json.loads(values.pop("questions_json"))
    return model_type.model_validate(values)


class Repos:
    """Thin repository facade over SQLAlchemy; one Session per unit of work."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self._atomic_depth = 0
        self._atomic_failed = False

    def _save(self) -> None:
        if not self._atomic_depth:
            self.session.commit()
            return
        if self._atomic_failed:
            raise DatabaseError("atomic transaction already rolled back")
        try:
            self.session.flush()
        except BaseException:
            self._atomic_failed = True
            self.session.rollback()
            raise

    @contextmanager
    def atomic(self) -> Iterator[Repos]:
        """Join one transaction; any nested failure makes it rollback-only."""
        outermost = self._atomic_depth == 0
        self._atomic_depth += 1
        try:
            yield self
            if outermost:
                if self._atomic_failed:
                    raise DatabaseError("atomic transaction already rolled back")
                self.session.commit()
        except BaseException:
            self._atomic_failed = True
            self.session.rollback()
            raise
        finally:
            self._atomic_depth -= 1
            if outermost:
                self._atomic_failed = False

    def _update_workflow(self, entity: DomainModel, row_type: type[_Row], label: str) -> None:
        row = self.session.get(row_type, _u(entity.id))
        if row is None:
            raise NotFoundError(f"{label} not found: {entity.id}")
        replacement = _workflow_to_row(entity, row_type)
        for attr in set(replacement.__table__.columns.keys()) - {"id"}:
            setattr(row, attr, getattr(replacement, attr))
        self._save()

    # -- shipments
    def add_shipment(self, s: Shipment) -> None:
        self.session.add(shipment_to_row(s))
        self._save()

    def get_shipment(self, id_or_reference: str) -> Shipment:
        if id_or_reference.strip().lower() == "last":
            # autonomy: `freight shipment approve last` targets the newest shipment
            row = self.session.scalar(
                select(m.ShipmentRow)
                .order_by(m.ShipmentRow.created_at.desc(), text("rowid DESC"))
                .limit(1)
            )
            if row is None:
                raise NotFoundError("no shipments recorded yet")
            return shipment_from_row(row)
        row = self.session.get(m.ShipmentRow, id_or_reference)
        if row is None:
            # Try prefix matching for truncated IDs
            row = self.session.scalar(
                select(m.ShipmentRow).where(m.ShipmentRow.id.startswith(id_or_reference))
            )
        if row is None:
            # Try exact reference match
            row = self.session.scalar(select(m.ShipmentRow).where(m.ShipmentRow.reference == id_or_reference))
        if row is None:
            raise NotFoundError(f"shipment not found: {id_or_reference}")
        return shipment_from_row(row)

    def list_shipments(self) -> list[Shipment]:
        rows = self.session.scalars(select(m.ShipmentRow).order_by(m.ShipmentRow.created_at)).all()
        return [shipment_from_row(r) for r in rows]

    def update_shipment(self, s: Shipment) -> None:
        row = self.session.get(m.ShipmentRow, _u(s.id))
        if row is None:
            raise NotFoundError(f"shipment not found: {s.id}")
        replacement = shipment_to_row(s)
        for attr in set(replacement.__table__.columns.keys()) - {"id", "created_at"}:
            setattr(row, attr, getattr(replacement, attr))
        self._save()

    # -- carriers
    def get_or_create_carrier(self, name: str) -> Carrier:
        row = self.session.scalar(select(m.CarrierRow).where(m.CarrierRow.name == name))
        if row is None:
            c = Carrier(name=name)
            self.session.add(m.CarrierRow(
                id=_u(c.id), name=c.name, portal_url=c.portal_url,
                contact_email=c.contact_email, notes=c.notes,
            ))
            self._save()
            return c
        return Carrier(id=_uid(row.id), name=row.name, portal_url=row.portal_url,
                       contact_email=row.contact_email, notes=row.notes)

    def get_carrier(self, carrier_id: str | UUID) -> Carrier | None:
        row = self.session.get(m.CarrierRow, _u(carrier_id))
        return None if row is None else Carrier(
            id=_uid(row.id), name=row.name, portal_url=row.portal_url,
            contact_email=row.contact_email, notes=row.notes,
        )

    def list_carriers(self) -> list[Carrier]:
        rows = self.session.scalars(select(m.CarrierRow).order_by(m.CarrierRow.name)).all()
        return [Carrier(id=_uid(r.id), name=r.name, portal_url=r.portal_url,
                        contact_email=r.contact_email, notes=r.notes) for r in rows]

    # -- intake
    def add_intake(self, i: IntakeItem, classification_json: str | None = None) -> None:
        self.session.add(intake_to_row(i, classification_json))
        self._save()

    def find_intake_by_sha(self, sha256: str) -> IntakeItem | None:
        row = self.session.scalar(
            select(m.IntakeItemRow).where(m.IntakeItemRow.content_sha256 == sha256)
            .order_by(m.IntakeItemRow.created_at)
        )
        return None if row is None else intake_from_row(row)

    def get_intake(self, intake_id: str) -> IntakeItem:
        if intake_id.strip().lower() == "last":
            # autonomy: `freight intake show last` targets the most recently added intake
            row = self.session.scalar(
                select(m.IntakeItemRow)
                .order_by(m.IntakeItemRow.created_at.desc(), text("rowid DESC"))
                .limit(1)
            )
            if row is None:
                raise NotFoundError("no intake items recorded yet")
            return intake_from_row(row)
        row = self.session.get(m.IntakeItemRow, intake_id)
        if row is None:
            # Try prefix matching for truncated IDs
            row = self.session.scalar(
                select(m.IntakeItemRow).where(m.IntakeItemRow.id.startswith(intake_id))
            )
        if row is None:
            raise NotFoundError(f"intake item not found: {intake_id}")
        return intake_from_row(row)

    def list_intakes(self, shipment_id: str | None, status: str | None) -> list[IntakeItem]:
        stmt = select(m.IntakeItemRow).order_by(m.IntakeItemRow.created_at)
        if shipment_id:
            stmt = stmt.where(m.IntakeItemRow.shipment_id == shipment_id)
        if status:
            stmt = stmt.where(m.IntakeItemRow.classification_status == status)
        return [intake_from_row(r) for r in self.session.scalars(stmt).all()]

    def update_intake(self, i: IntakeItem, classification_json: str | None = None) -> None:
        row = self.session.get(m.IntakeItemRow, _u(i.id))
        if row is None:
            raise NotFoundError(f"intake item not found: {i.id}")
        row.classification_status = str(i.classification_status)
        row.classification_reason = i.classification_reason
        row.classification_confidence = i.classification_confidence
        row.processing_status = str(i.processing_status)
        row.shipment_id = _u(i.shipment_id) if i.shipment_id else None
        if classification_json is not None:
            row.classification_json = classification_json
        self._save()

    def delete_intake_cascade(self, intake_id: str) -> None:
        """Delete an intake item and all associated data (quotations, evidence, gaps, drafts, replies)."""
        with self.atomic():
            for proposal in self.session.scalars(select(m.DestinationProposalRow).where(m.DestinationProposalRow.intake_id == _u(intake_id))).all():
                self.session.delete(proposal)
            # Find all quotations for this intake
            quotes = self.session.scalars(
                select(m.QuotationRow).where(m.QuotationRow.intake_item_id == _u(intake_id))
            ).all()
            quote_ids = [q.id for q in quotes]

            for row_type in (m.DestinationImportRow, m.ClarificationTurnRow):
                for related in self.session.scalars(select(row_type).where(row_type.quotation_id.in_(quote_ids))).all():
                    self.session.delete(related)
            for source in self.session.scalars(select(m.SourceDocumentRow).where(m.SourceDocumentRow.intake_item_id == _u(intake_id))).all():
                self.session.delete(source)

            # Delete supplier replies that reference this intake
            for reply in self.session.scalars(
                select(m.SupplierReplyRow).where((m.SupplierReplyRow.intake_item_id == _u(intake_id)) | m.SupplierReplyRow.quotation_id.in_(quote_ids))
            ).all():
                self.session.delete(reply)

            # Delete quotation versions
            for qid in quote_ids:
                for version in self.session.scalars(
                    select(m.QuotationVersionRow).where(m.QuotationVersionRow.quotation_id == qid)
                ).all():
                    self.session.delete(version)

            # Delete evidence and gaps for each quotation
            for qid in quote_ids:
                for evidence in self.session.scalars(
                    select(m.FieldEvidenceRow).where(m.FieldEvidenceRow.quotation_id == qid)
                ).all():
                    self.session.delete(evidence)
                for gap in self.session.scalars(
                    select(m.GapRow).where(m.GapRow.quotation_id == qid)
                ).all():
                    self.session.delete(gap)

            # Delete question sets and drafts that reference these quotations
            for qid in quote_ids:
                for qset in self.session.scalars(
                    select(m.QuestionSetRow).where(m.QuestionSetRow.quotation_id == qid)
                ).all():
                    # Delete drafts for this question set
                    for draft in self.session.scalars(
                        select(m.ClarificationDraftRow).where(m.ClarificationDraftRow.question_set_id == qset.id)
                    ).all():
                        self.session.delete(draft)
                    self.session.delete(qset)

            # Delete quotations
            for qid in quote_ids:
                for quote in self.session.scalars(
                    select(m.QuotationRow).where(m.QuotationRow.id == qid)
                ).all():
                    self.session.delete(quote)

            # Finally delete the intake item
            row = self.session.get(m.IntakeItemRow, _u(intake_id))
            if row is None:
                raise NotFoundError(f"intake item not found: {intake_id}")
            self.session.delete(row)

    # -- quotations
    def add_quotation(self, q: Quotation) -> None:
        self.session.add(quotation_to_row(q))
        self._save()

    def get_quotation(self, quote_id: str) -> Quotation:
        row = self.session.get(m.QuotationRow, quote_id)
        if row is None:
            # Try prefix matching for truncated IDs
            row = self.session.scalar(
                select(m.QuotationRow).where(m.QuotationRow.id.startswith(quote_id))
            )
        if row is None:
            raise NotFoundError(f"quotation not found: {quote_id}")
        return quotation_from_row(row)

    def list_quotations(self, shipment_id: str | None = None) -> list[Quotation]:
        stmt = select(m.QuotationRow).order_by(m.QuotationRow.created_at)
        if shipment_id:
            stmt = stmt.where(m.QuotationRow.shipment_id == shipment_id)
        return [quotation_from_row(r) for r in self.session.scalars(stmt).all()]

    def quotation_for_intake(self, intake_id: str) -> Quotation | None:
        row = self.session.scalar(
            select(m.QuotationRow).where(m.QuotationRow.intake_item_id == _u(intake_id))
        )
        return None if row is None else quotation_from_row(row)

    def update_quotation(self, q: Quotation) -> None:
        existing = self.session.get(m.QuotationRow, _u(q.id))
        if existing is None:
            raise NotFoundError(f"quotation not found: {q.id}")
        new_row = quotation_to_row(q)
        for attr in set(new_row.__table__.columns.keys()) - {"id"}:
            setattr(existing, attr, getattr(new_row, attr))
        self._save()

    # -- Workflow B
    def add_question_set(self, question_set: QuestionSet) -> None:
        self.session.add(_workflow_to_row(question_set, m.QuestionSetRow))
        self._save()

    def get_question_set(self, question_set_id: str | UUID) -> QuestionSet:
        row = self.session.get(m.QuestionSetRow, _u(question_set_id))
        if row is None:
            # Try prefix matching for truncated IDs
            row = self.session.scalar(
                select(m.QuestionSetRow).where(m.QuestionSetRow.id.startswith(_u(question_set_id)))
            )
        if row is None:
            raise NotFoundError(f"question set not found: {question_set_id}")
        return _workflow_from_row(row, QuestionSet)

    def list_question_sets(self, shipment_id: str | UUID) -> list[QuestionSet]:
        rows = self.session.scalars(
            select(m.QuestionSetRow).where(m.QuestionSetRow.shipment_id == _u(shipment_id))
            .order_by(m.QuestionSetRow.created_at, m.QuestionSetRow.id)
        ).all()
        return [_workflow_from_row(row, QuestionSet) for row in rows]

    def update_question_set(self, question_set: QuestionSet) -> None:
        self._update_workflow(question_set, m.QuestionSetRow, "question set")

    def add_draft(self, draft: ClarificationDraft) -> None:
        self.session.add(_workflow_to_row(draft, m.ClarificationDraftRow))
        self._save()

    def get_draft(self, draft_id: str | UUID) -> ClarificationDraft:
        row = self.session.get(m.ClarificationDraftRow, _u(draft_id))
        if row is None:
            # Try prefix matching for truncated IDs
            row = self.session.scalar(
                select(m.ClarificationDraftRow).where(m.ClarificationDraftRow.id.startswith(_u(draft_id)))
            )
        if row is None:
            raise NotFoundError(f"draft not found: {draft_id}")
        return _workflow_from_row(row, ClarificationDraft)

    def list_drafts(self, shipment_id: str | UUID) -> list[ClarificationDraft]:
        rows = self.session.scalars(
            select(m.ClarificationDraftRow).where(m.ClarificationDraftRow.shipment_id == _u(shipment_id))
            .order_by(m.ClarificationDraftRow.created_at, m.ClarificationDraftRow.id)
        ).all()
        return [_workflow_from_row(row, ClarificationDraft) for row in rows]

    def update_draft(self, draft: ClarificationDraft) -> None:
        self._update_workflow(draft, m.ClarificationDraftRow, "draft")

    def add_reply(self, reply: SupplierReply) -> None:
        self.session.add(_workflow_to_row(reply, m.SupplierReplyRow))
        self._save()

    def update_reply(self, reply: SupplierReply) -> None:
        self._update_workflow(reply, m.SupplierReplyRow, "supplier reply")

    def get_reply(self, reply_id: str | UUID) -> SupplierReply:
        row = self.session.get(m.SupplierReplyRow, _u(reply_id))
        if row is None:
            # Try prefix matching for truncated IDs
            row = self.session.scalar(
                select(m.SupplierReplyRow).where(m.SupplierReplyRow.id.startswith(_u(reply_id)))
            )
        if row is None:
            raise NotFoundError(f"supplier reply not found: {reply_id}")
        return _workflow_from_row(row, SupplierReply)

    def reply_for_intake(self, intake_item_id: str | UUID) -> SupplierReply | None:
        row = self.session.scalar(
            select(m.SupplierReplyRow).where(m.SupplierReplyRow.intake_item_id == _u(intake_item_id))
        )
        return None if row is None else _workflow_from_row(row, SupplierReply)

    def list_replies(self, shipment_id: str | UUID) -> list[SupplierReply]:
        rows = self.session.scalars(
            select(m.SupplierReplyRow).where(m.SupplierReplyRow.shipment_id == _u(shipment_id))
            .order_by(m.SupplierReplyRow.created_at, m.SupplierReplyRow.id)
        ).all()
        return [_workflow_from_row(row, SupplierReply) for row in rows]

    def record_quotation_version(
        self, quote: Quotation, evidence: list[FieldEvidence],
        source_intake_item_id: UUID | None = None,
    ) -> QuotationVersion:
        stmt = select(m.QuotationVersionRow).where(
            m.QuotationVersionRow.quotation_id == _u(quote.id),
            m.QuotationVersionRow.version == quote.version,
        )
        existing = self.session.scalar(stmt)
        if existing is not None:
            return _workflow_from_row(existing, QuotationVersion)
        version = QuotationVersion(
            quotation_id=quote.id, version=quote.version,
            source_intake_item_id=source_intake_item_id or quote.intake_item_id,
            snapshot_json=quote.model_dump_json(),
            evidence_json=json.dumps([item.model_dump(mode="json") for item in evidence]),
        )
        # The unique key also makes concurrent/repeated recording immutable.
        self.session.execute(
            sqlite_insert(m.QuotationVersionRow).values(**version.model_dump(mode="json"))
            .on_conflict_do_nothing(index_elements=["quotation_id", "version"])
        )
        self._save()
        row = self.session.scalar(stmt)
        if row is None:
            raise DatabaseError(f"quotation version not recorded: {quote.id}/{quote.version}")
        return _workflow_from_row(row, QuotationVersion)

    def versions_for_quotation(self, quote_id: str | UUID) -> list[QuotationVersion]:
        rows = self.session.scalars(
            select(m.QuotationVersionRow).where(m.QuotationVersionRow.quotation_id == _u(quote_id))
            .order_by(m.QuotationVersionRow.version)
        ).all()
        return [_workflow_from_row(row, QuotationVersion) for row in rows]

    # -- evidence
    def add_evidence(self, items: list[FieldEvidence]) -> None:
        for e in items:
            self.session.add(m.FieldEvidenceRow(
                id=_u(e.id), quotation_id=_u(e.quotation_id), field_name=e.field_name,
                value_as_extracted=e.value_as_extracted, source_filename=e.source_filename,
                page_or_sheet=e.page_or_sheet, snippet=e.snippet, confidence=e.confidence,
                explanation=e.explanation,
                validation_status=str(e.validation_status), created_at=e.created_at.isoformat(),
            ))
        self._save()

    def evidence_for_quotation(self, quotation_id: str) -> list[FieldEvidence]:
        rows = self.session.scalars(
            select(m.FieldEvidenceRow).where(m.FieldEvidenceRow.quotation_id == _u(quotation_id))
        ).all()
        return [FieldEvidence(
            id=_uid(r.id), quotation_id=_uid(r.quotation_id), field_name=r.field_name,
            value_as_extracted=r.value_as_extracted, source_filename=r.source_filename,
            page_or_sheet=r.page_or_sheet, snippet=r.snippet, confidence=r.confidence,
            explanation=r.explanation,
            validation_status=str(r.validation_status),  # type: ignore[arg-type]
            created_at=m.iso_to_dt(r.created_at) or FieldEvidence.model_fields["created_at"].default_factory(),
        ) for r in rows]

    def get_evidence(self, evidence_id: str) -> FieldEvidence:
        row = self.session.get(m.FieldEvidenceRow, _u(evidence_id))
        if row is None:
            raise NotFoundError(f"evidence not found: {evidence_id}")
        return FieldEvidence(
            id=_uid(row.id), quotation_id=_uid(row.quotation_id), field_name=row.field_name,
            value_as_extracted=row.value_as_extracted, source_filename=row.source_filename,
            page_or_sheet=row.page_or_sheet, snippet=row.snippet, confidence=row.confidence,
            explanation=row.explanation,
            validation_status=str(row.validation_status),  # type: ignore[arg-type]
            created_at=m.iso_to_dt(row.created_at) or FieldEvidence.model_fields["created_at"].default_factory(),
        )

    def update_evidence(self, e: FieldEvidence) -> None:
        row = self.session.get(m.FieldEvidenceRow, _u(e.id))
        if row is None:
            raise NotFoundError(f"evidence not found: {e.id}")
        row.validation_status = str(e.validation_status)
        self._save()

    # -- gaps
    def add_gaps(self, gaps: list[Gap]) -> None:
        for g in gaps:
            self.session.add(m.GapRow(
                id=_u(g.id), shipment_id=_u(g.shipment_id),
                quotation_id=_u(g.quotation_id) if g.quotation_id else None,
                field_name=g.field_name, gap_type=str(g.gap_type), severity=str(g.severity),
                description=g.description, question_text=g.question_text,
                status=str(g.status), created_at=g.created_at.isoformat(),
                resolved_at=g.resolved_at.isoformat() if g.resolved_at else None,
            ))
        self._save()

    def update_gap(self, gap: Gap) -> None:
        self._update_workflow(gap, m.GapRow, "gap")

    def get_gap(self, gap_id: str) -> Gap:
        row = self.session.get(m.GapRow, gap_id)
        if row is None:
            row = self.session.scalar(
                select(m.GapRow).where(m.GapRow.id.startswith(gap_id))
            )
        if row is None:
            raise NotFoundError(f"gap not found: {gap_id}")
        return Gap(
            id=_uid(row.id), shipment_id=_uid(row.shipment_id),
            quotation_id=_uid(row.quotation_id) if row.quotation_id else None,
            field_name=row.field_name, gap_type=row.gap_type,  # type: ignore[arg-type]
            severity=row.severity,  # type: ignore[arg-type]
            description=row.description, question_text=row.question_text,
            status=row.status,  # type: ignore[arg-type]
            created_at=m.iso_to_dt(row.created_at) or Gap.model_fields["created_at"].default_factory(),
            resolved_at=m.iso_to_dt(row.resolved_at),
        )

    def list_gaps(self, shipment_id: str) -> list[Gap]:
        rows = self.session.scalars(
            select(m.GapRow).where(m.GapRow.shipment_id == _u(shipment_id))
            .order_by(m.GapRow.created_at)
        ).all()
        return [Gap(
            id=_uid(r.id), shipment_id=_uid(r.shipment_id),
            quotation_id=_uid(r.quotation_id) if r.quotation_id else None,
            field_name=r.field_name, gap_type=r.gap_type,  # type: ignore[arg-type]
            severity=r.severity,  # type: ignore[arg-type]
            description=r.description, question_text=r.question_text,
            status=r.status,  # type: ignore[arg-type]
            created_at=m.iso_to_dt(r.created_at) or Gap.model_fields["created_at"].default_factory(),
            resolved_at=m.iso_to_dt(r.resolved_at),
        ) for r in rows]

    def open_gaps_for_quotation(self, quotation_id: str) -> list[Gap]:
        return [g for g in self.session.scalars(
            select(m.GapRow).where(
                m.GapRow.quotation_id == _u(quotation_id), m.GapRow.status == "open")
        ).all()]

    def delete_evidence_for_quotation(self, quotation_id: str) -> None:
        for row in self.session.scalars(
            select(m.FieldEvidenceRow).where(m.FieldEvidenceRow.quotation_id == _u(quotation_id))
        ).all():
            self.session.delete(row)
        self._save()

    def delete_gaps_for_quotation(self, quotation_id: str) -> None:
        for row in self.session.scalars(
            select(m.GapRow).where(m.GapRow.quotation_id == _u(quotation_id))
        ).all():
            self.session.delete(row)
        self._save()

    # -- audit
    def add_audit(self, event: AuditEvent) -> None:
        self.session.add(m.AuditEventRow(
            id=_u(event.id), entity_type=event.entity_type, entity_id=_u(event.entity_id),
            action=event.action, details_json=event.details_json,
            created_at=event.created_at.isoformat(),
        ))
        self._save()

    def list_recent_audit(self, limit: int = 20) -> list[AuditEvent]:
        rows = self.session.scalars(
            select(m.AuditEventRow).order_by(m.AuditEventRow.created_at.desc(), text("rowid DESC")).limit(limit)
        ).all()
        return [AuditEvent(id=_audit_uid(r.id), entity_type=r.entity_type, entity_id=_audit_uid(r.entity_id),
                           action=r.action, details_json=r.details_json,
                           created_at=m.iso_to_dt(r.created_at) or AuditEvent.model_fields["created_at"].default_factory())
                for r in rows]

    def audit_for(self, entity_id: str) -> list[AuditEvent]:
        rows = self.session.scalars(
            select(m.AuditEventRow).where(m.AuditEventRow.entity_id == _u(entity_id))
        ).all()
        return [AuditEvent(id=_audit_uid(r.id), entity_type=r.entity_type, entity_id=_audit_uid(r.entity_id),
                           action=r.action, details_json=r.details_json,
                           created_at=m.iso_to_dt(r.created_at) or AuditEvent.model_fields["created_at"].default_factory())
                for r in rows]
