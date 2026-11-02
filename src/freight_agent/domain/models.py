from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from .enums import (
    AssociationMethod,
    ClassificationStatus,
    ComparisonReadiness,
    ComparisonStatus,
    DeliveryScope,
    DocumentType,
    DraftStatus,
    GapSeverity,
    GapStatus,
    GapType,
    ProcessingStatus,
    QuestionSetStatus,
    ReviewStatus,
    ShipmentStatus,
    SourceType,
    ValidationStatus,
)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


class DomainModel(BaseModel):
    model_config = ConfigDict(validate_assignment=True)


# ---------------------------------------------------------------- entities (§6)

class Shipment(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    reference: str
    name: str
    origin: str
    destination: str
    origin_country: str | None = None
    destination_country: str | None = None
    route_category: str | None = None
    source_intake_item_id: UUID | None = None
    route_evidence_json: str = "[]"
    approved_at: datetime | None = None
    cargo_description: str
    weight_kg: Decimal | None = None
    volume_m3: Decimal | None = None
    required_delivery_scope: DeliveryScope = DeliveryScope.unknown
    target_currency: str = "EUR"
    notes: str | None = None
    status: ShipmentStatus = ShipmentStatus.open
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class IntakeItem(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    shipment_id: UUID | None = None
    source_type: SourceType = SourceType.upload
    original_filename: str
    stored_path: str
    content_sha256: str
    sender: str | None = None
    subject: str | None = None
    received_at: datetime | None = None
    raw_text: str
    file_size_bytes: int = 0
    page_or_sheet_info: str | None = None
    classification_status: ClassificationStatus = ClassificationStatus.pending
    classification_reason: str | None = None
    classification_confidence: float | None = None
    processing_status: ProcessingStatus = ProcessingStatus.pending
    created_at: datetime = Field(default_factory=_now)


class Carrier(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    name: str
    portal_url: str | None = None
    contact_email: str | None = None
    notes: str | None = None


class Charge(DomainModel):
    label: str
    amount: Decimal | None = None
    currency: str | None = None
    included_in_base_price: bool | None = None
    mandatory: bool | None = None
    evidence_snippet: str | None = None


class Quotation(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    shipment_id: UUID
    carrier_id: UUID | None = None
    intake_item_id: UUID
    quote_reference: str | None = None
    currency: str | None = None
    base_price: Decimal | None = None
    known_additional_charges: list[Charge] = Field(default_factory=list)
    excluded_services: list[str] = Field(default_factory=list)
    included_services: list[str] = Field(default_factory=list)
    origin: str | None = None
    destination: str | None = None
    transit_days_min: int | None = None
    transit_days_max: int | None = None
    valid_until: date | None = None
    delivery_scope: DeliveryScope | None = None
    insurance_included: bool | None = None
    customs_included: bool | None = None
    total_known_cost: Decimal | None = None  # computed deterministically
    comparison_status: ComparisonStatus = ComparisonStatus.needs_review
    assumptions: list[str] = Field(default_factory=list)
    model_name: str | None = None
    prompt_version: str | None = None
    processed_at: datetime | None = None
    review_status: ReviewStatus = ReviewStatus.extracted
    reviewed_at: datetime | None = None
    imported_at: datetime | None = None
    review_notes: str | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    version: int = 1


class FieldEvidence(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    quotation_id: UUID
    field_name: str
    value_as_extracted: str
    source_filename: str
    page_or_sheet: str | None = None
    snippet: str
    confidence: float | None = None
    validation_status: ValidationStatus = ValidationStatus.accepted
    created_at: datetime = Field(default_factory=_now)


class SupplierReply(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    shipment_id: UUID
    carrier_id: UUID | None = None
    quotation_id: UUID
    intake_item_id: UUID
    association_method: AssociationMethod
    created_at: datetime = Field(default_factory=_now)


class QuotationVersion(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    quotation_id: UUID
    version: int
    source_intake_item_id: UUID
    snapshot_json: str
    evidence_json: str
    created_at: datetime = Field(default_factory=_now)


class Gap(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    shipment_id: UUID
    quotation_id: UUID | None = None
    field_name: str
    gap_type: GapType
    severity: GapSeverity
    description: str
    question_text: str | None = None
    status: GapStatus = GapStatus.open
    created_at: datetime = Field(default_factory=_now)
    resolved_at: datetime | None = None


class AuditEvent(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    entity_type: str
    entity_id: UUID
    action: str
    details_json: str = "{}"
    created_at: datetime = Field(default_factory=_now)


# ------------------------------------------------- structured LLM schemas (§7)

class ClassificationResult(DomainModel):
    is_freight_quote: bool
    reason: str
    confidence: float = Field(ge=0.0, le=1.0)
    carrier_name: str | None = None
    shipment_reference: str | None = None
    needs_human_review: bool = False
    document_type: DocumentType = DocumentType.ambiguous


class EvidenceItem(DomainModel):
    field_name: str
    snippet: str
    page_or_sheet: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class QuoteExtraction(DomainModel):
    carrier_name: str | None = None
    quote_reference: str | None = None
    currency: str | None = None
    base_price: Decimal | None = None
    charges: list[Charge] = Field(default_factory=list)
    included_services: list[str] = Field(default_factory=list)
    excluded_services: list[str] = Field(default_factory=list)
    origin: str | None = None
    destination: str | None = None
    origin_country: str | None = None
    destination_country: str | None = None
    cargo_description: str | None = None
    weight_kg: Decimal | None = None
    volume_m3: Decimal | None = None
    transit_days_min: int | None = None
    transit_days_max: int | None = None
    valid_until: date | None = None
    delivery_scope: str | None = None
    insurance_included: bool | None = None
    customs_included: bool | None = None
    assumptions: list[str] = Field(default_factory=list)
    fields_requiring_review: list[str] = Field(default_factory=list)
    field_evidence: list[EvidenceItem] = Field(default_factory=list)


class ClarificationQuestion(DomainModel):
    field_name: str
    why_it_matters: str
    question_text: str
    priority: GapSeverity
    source_context: str  # evidence snippet or "not found"
    target_carrier_id: UUID | None = None
    target_carrier_name: str | None = None


class QuestionSet(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    shipment_id: UUID
    quotation_id: UUID
    carrier_id: UUID | None = None
    quotation_version: int
    questions: list[ClarificationQuestion]
    status: QuestionSetStatus = QuestionSetStatus.draft
    model_name: str | None = None
    prompt_version: str | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    approved_at: datetime | None = None


class ClarificationDraft(DomainModel):
    id: UUID = Field(default_factory=uuid4)
    shipment_id: UUID
    quotation_id: UUID
    carrier_id: UUID | None = None
    question_set_id: UUID
    quotation_version: int
    recipient: str | None = None
    subject: str
    body: str
    status: DraftStatus = DraftStatus.draft
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    approved_at: datetime | None = None


class GapItem(DomainModel):
    field_name: str
    gap_type: GapType
    severity: GapSeverity
    description: str


class GapAnalysisResult(DomainModel):
    gaps: list[GapItem] = Field(default_factory=list)
    clarification_questions: list[ClarificationQuestion] = Field(default_factory=list)
    comparison_readiness: ComparisonReadiness = ComparisonReadiness.not_ready
    summary: str = ""


# ------------------------------------------------------------- app-level DTOs

class SupplierDraftWordings(DomainModel):
    """Supplier-facing email text; validated bounds, never trusted before checking."""
    subject: str = Field(min_length=3, max_length=120)
    body: str = Field(min_length=20, max_length=4000)


class ProcessingSummary(DomainModel):
    intake_item_id: UUID
    shipment_id: UUID | None = None
    shipment_status: ShipmentStatus | None = None
    classification: Optional[ClassificationResult] = None
    quotation_id: UUID | None = None
    created_quote: bool = False
    gap_count: int = 0
    critical_gap_count: int = 0
    comparison_status: ComparisonStatus | None = None
    notes: list[str] = Field(default_factory=list)


class ComparisonRow(DomainModel):
    """One carrier quote normalized into the cross-carrier comparison schema (§10 output)."""
    quotation_id: UUID
    carrier_name: str | None = None
    quote_reference: str | None = None
    currency: str | None = None
    base_price: Decimal | None = None
    known_additional_total: Decimal | None = None
    known_subtotal: Decimal | None = None
    converted_subtotal: Decimal | None = None
    fx_note: str | None = None
    uncertain_charges: list[str] = Field(default_factory=list)
    included_services: list[str] = Field(default_factory=list)
    excluded_services: list[str] = Field(default_factory=list)
    delivery_scope: str = "unknown"
    transit_time: str = "-"
    valid_until: date | None = None
    comparison_status: ComparisonStatus = ComparisonStatus.needs_review
    scope_compatible: bool = True
    unresolved_critical_gaps: list[str] = Field(default_factory=list)
    unresolved_other_gaps: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)


class ComparisonReport(DomainModel):
    shipment_id: UUID
    shipment_reference: str
    route: str
    required_scope: str
    target_currency: str
    generated_at: datetime = Field(default_factory=_now)
    rows: list[ComparisonRow] = Field(default_factory=list)
    recommendation: str | None = None
    recommendation_basis: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
