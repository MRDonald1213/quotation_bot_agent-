from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA_VERSION = 11


def utcnow_iso() -> str:
    """ISO-8601 UTC timestamps (spec §6)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def iso_to_dt(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def date_to_iso(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def iso_to_date(value: str | None) -> date | None:
    return None if value is None else date.fromisoformat(value)


class Base(DeclarativeBase):
    pass


class ShipmentRow(Base):
    __tablename__ = "shipments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    reference: Mapped[str] = mapped_column(String(120), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    origin: Mapped[str] = mapped_column(String(200))
    destination: Mapped[str] = mapped_column(String(200))
    origin_country: Mapped[str | None] = mapped_column(String(2))
    destination_country: Mapped[str | None] = mapped_column(String(2))
    route_category: Mapped[str | None] = mapped_column(String(6))
    source_intake_item_id: Mapped[str | None] = mapped_column(String(36))
    route_evidence_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    approved_at: Mapped[str | None] = mapped_column(String(32))
    cargo_description: Mapped[str] = mapped_column(Text)
    weight_kg: Mapped[str | None] = mapped_column(Text)   # Decimal as text
    volume_m3: Mapped[str | None] = mapped_column(Text)
    required_delivery_scope: Mapped[str] = mapped_column(String(32))
    target_currency: Mapped[str] = mapped_column(String(3))
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), index=True)
    created_at: Mapped[str] = mapped_column(String(32))
    updated_at: Mapped[str] = mapped_column(String(32))


class CarrierRow(Base):
    __tablename__ = "carriers"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    portal_url: Mapped[str | None] = mapped_column(Text)
    contact_email: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("name"),)


class IntakeItemRow(Base):
    __tablename__ = "intake_items"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    shipment_id: Mapped[str | None] = mapped_column(
        ForeignKey("shipments.id", ondelete="SET NULL"), index=True
    )
    source_type: Mapped[str] = mapped_column(String(24))
    original_filename: Mapped[str] = mapped_column(String(300))
    stored_path: Mapped[str] = mapped_column(Text)
    content_sha256: Mapped[str] = mapped_column(String(64), index=True)
    sender: Mapped[str | None] = mapped_column(String(300))
    subject: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[str | None] = mapped_column(String(32))
    raw_text: Mapped[str] = mapped_column(Text)
    file_size_bytes: Mapped[int] = mapped_column(Integer)
    page_or_sheet_info: Mapped[str | None] = mapped_column(String(200))
    classification_status: Mapped[str] = mapped_column(String(24), index=True)
    classification_reason: Mapped[str | None] = mapped_column(Text)
    classification_confidence: Mapped[float | None] = mapped_column(Float)
    classification_json: Mapped[str | None] = mapped_column(Text)
    processing_status: Mapped[str] = mapped_column(String(24), index=True)
    gmail_message_id: Mapped[str | None] = mapped_column(
        ForeignKey("gmail_messages.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[str] = mapped_column(String(32))


class QuotationRow(Base):
    details_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    disposition: Mapped[str | None] = mapped_column(String(24))
    disposition_reason: Mapped[str | None] = mapped_column(Text)
    archived_at: Mapped[str | None] = mapped_column(String(32))
    __tablename__ = "quotations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    shipment_id: Mapped[str] = mapped_column(ForeignKey("shipments.id"), index=True)
    carrier_id: Mapped[str | None] = mapped_column(ForeignKey("carriers.id"), index=True)
    intake_item_id: Mapped[str] = mapped_column(ForeignKey("intake_items.id"))
    quote_reference: Mapped[str | None] = mapped_column(String(120))
    currency: Mapped[str | None] = mapped_column(String(3))
    base_price: Mapped[str | None] = mapped_column(Text)  # Decimal as text, never float
    charges_json: Mapped[str] = mapped_column(Text, default="[]")
    included_services_json: Mapped[str] = mapped_column(Text, default="[]")
    excluded_services_json: Mapped[str] = mapped_column(Text, default="[]")
    assumptions_json: Mapped[str] = mapped_column(Text, default="[]")
    origin: Mapped[str | None] = mapped_column(String(200))
    destination: Mapped[str | None] = mapped_column(String(200))
    transit_days_min: Mapped[int | None] = mapped_column(Integer)
    transit_days_max: Mapped[int | None] = mapped_column(Integer)
    valid_until: Mapped[str | None] = mapped_column(String(10), index=True)
    delivery_scope: Mapped[str | None] = mapped_column(String(32))
    insurance_included: Mapped[int | None] = mapped_column(Integer)  # nullable bool as 0/1
    customs_included: Mapped[int | None] = mapped_column(Integer)
    total_known_cost: Mapped[str | None] = mapped_column(Text)
    comparison_status: Mapped[str] = mapped_column(String(24), index=True)
    review_status: Mapped[str] = mapped_column(String(16), index=True, default="extracted")
    reviewed_at: Mapped[str | None] = mapped_column(String(32))
    imported_at: Mapped[str | None] = mapped_column(String(32))
    review_notes: Mapped[str | None] = mapped_column(Text)
    model_name: Mapped[str | None] = mapped_column(String(120))
    prompt_version: Mapped[str | None] = mapped_column(String(60))
    processed_at: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[str] = mapped_column(String(32))
    updated_at: Mapped[str] = mapped_column(String(32))
    version: Mapped[int] = mapped_column(Integer, default=1)


class FieldEvidenceRow(Base):
    __tablename__ = "field_evidence"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    quotation_id: Mapped[str] = mapped_column(ForeignKey("quotations.id", ondelete="CASCADE"), index=True)
    field_name: Mapped[str] = mapped_column(String(64))
    value_as_extracted: Mapped[str] = mapped_column(Text)
    source_filename: Mapped[str] = mapped_column(String(300))
    page_or_sheet: Mapped[str | None] = mapped_column(String(64))
    snippet: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    validation_status: Mapped[str] = mapped_column(String(24))
    created_at: Mapped[str] = mapped_column(String(32))


class GapRow(Base):
    __tablename__ = "gaps"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    shipment_id: Mapped[str] = mapped_column(ForeignKey("shipments.id"), index=True)
    quotation_id: Mapped[str | None] = mapped_column(ForeignKey("quotations.id"), index=True)
    field_name: Mapped[str] = mapped_column(String(64))
    gap_type: Mapped[str] = mapped_column(String(24))
    severity: Mapped[str] = mapped_column(String(16), index=True)
    description: Mapped[str] = mapped_column(Text)
    question_text: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), index=True)
    created_at: Mapped[str] = mapped_column(String(32))
    resolved_at: Mapped[str | None] = mapped_column(String(32))


class AuditEventRow(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(48), index=True)
    entity_id: Mapped[str] = mapped_column(String(36))
    action: Mapped[str] = mapped_column(String(64))
    details_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(32))


class SchemaVersionRow(Base):
    __tablename__ = "schema_version"
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    applied_at: Mapped[str] = mapped_column(String(32))
    description: Mapped[str] = mapped_column(Text)


class SupplierDatabaseRow(Base):
    __tablename__ = "supplier_databases"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str | None] = mapped_column(String(120))
    routing_aliases_json: Mapped[str] = mapped_column(Text, default="[]")
    csv_filename: Mapped[str] = mapped_column(String(300))
    csv_path: Mapped[str] = mapped_column(Text)
    schema_json: Mapped[str] = mapped_column(Text, default="[]")
    record_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    created_at: Mapped[str] = mapped_column(String(32))
    updated_at: Mapped[str] = mapped_column(String(32))


class SupplierQuotationActionRow(Base):
    __tablename__ = "supplier_quotation_actions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    intake_id: Mapped[str] = mapped_column(String(36), ForeignKey("intake_items.id"), index=True, unique=True)
    database_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("supplier_databases.id"), index=True)
    status: Mapped[str] = mapped_column(String(24), default="received", index=True)
    routing_json: Mapped[str] = mapped_column(Text, default="[]")
    proposed_row_json: Mapped[str] = mapped_column(Text, default="{}")
    missing_fields_json: Mapped[str] = mapped_column(Text, default="[]")
    evidence_json: Mapped[str] = mapped_column(Text, default="{}")
    notes: Mapped[str | None] = mapped_column(Text)
    created_csv_row_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(32))
    updated_at: Mapped[str] = mapped_column(String(32))


class SupplierChatRow(Base):
    __tablename__ = "supplier_chat"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    citations_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[str] = mapped_column(String(32))


class SupplierMasterRow(Base):
    __tablename__ = "supplier_master"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    database_id: Mapped[str] = mapped_column(String(36), ForeignKey("supplier_databases.id"), index=True)
    source_row: Mapped[int] = mapped_column(Integer)
    supplier_identity: Mapped[str] = mapped_column(String(300), index=True)
    record_json: Mapped[str] = mapped_column(Text, default="{}")
    source_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[str] = mapped_column(String(32))
    updated_at: Mapped[str] = mapped_column(String(32))


class QuotationRegisterRow(Base):
    __tablename__ = "quotation_register"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    supplier_master_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("supplier_master.id"), index=True)
    database_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("supplier_databases.id"), index=True)
    intake_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("intake_items.id"), index=True)
    gmail_message_id: Mapped[str | None] = mapped_column(String(255), index=True)
    gmail_thread_id: Mapped[str | None] = mapped_column(String(255), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(24), default="pending_review", index=True)
    data_json: Mapped[str] = mapped_column(Text, default="{}")
    evidence_json: Mapped[str] = mapped_column(Text, default="{}")
    source_hash: Mapped[str] = mapped_column(String(64))
    approved_at: Mapped[str | None] = mapped_column(String(32))
    imported_at: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[str] = mapped_column(String(32))
    updated_at: Mapped[str] = mapped_column(String(32))


class GmailAccountRow(Base):
    __tablename__ = "gmail_accounts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    token_path: Mapped[str | None] = mapped_column(Text)
    scopes_json: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[str] = mapped_column(String(24), default="disconnected", index=True)
    history_id: Mapped[str | None] = mapped_column(String(64))
    last_synced_at: Mapped[str | None] = mapped_column(String(32))
    sync_after_at: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[str] = mapped_column(String(32))
    updated_at: Mapped[str] = mapped_column(String(32))


class GmailMessageRow(Base):
    __tablename__ = "gmail_messages"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    gmail_message_id: Mapped[str] = mapped_column(String(255), unique=True)
    gmail_thread_id: Mapped[str | None] = mapped_column(String(255), index=True)
    account_id: Mapped[str] = mapped_column(String(36), ForeignKey("gmail_accounts.id"), index=True)
    sender: Mapped[str] = mapped_column(String(320))
    recipients_json: Mapped[str] = mapped_column(Text, default="[]")
    subject: Mapped[str] = mapped_column(Text, default="")
    body_text: Mapped[str] = mapped_column(Text, default="")
    sent_at: Mapped[str | None] = mapped_column(String(32))
    classification: Mapped[str] = mapped_column(String(32), default="unclassified", index=True)
    processing_status: Mapped[str] = mapped_column(String(32), default="received", index=True)
    source_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[str] = mapped_column(String(32))


class GmailAttachmentRow(Base):
    __tablename__ = "gmail_attachments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    message_id: Mapped[str] = mapped_column(String(36), ForeignKey("gmail_messages.id"), index=True)
    gmail_attachment_id: Mapped[str] = mapped_column(String(255))
    filename: Mapped[str] = mapped_column(String(500))
    mime_type: Mapped[str] = mapped_column(String(200))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    source_hash: Mapped[str] = mapped_column(String(64))
    stored_path: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(32))


class SupplierEmailDraftRow(Base):
    quotation_id: Mapped[str | None] = mapped_column(String(36))
    clarification_turn_id: Mapped[str | None] = mapped_column(String(36))
    __tablename__ = "supplier_email_drafts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    quotation_register_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("quotation_register.id"), index=True)
    supplier_master_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("supplier_master.id"), index=True)
    gmail_account_id: Mapped[str] = mapped_column(String(36), ForeignKey("gmail_accounts.id"), index=True)
    gmail_thread_id: Mapped[str | None] = mapped_column(String(255), index=True)
    gmail_draft_id: Mapped[str | None] = mapped_column(String(255), index=True)
    supplier_action_id: Mapped[str | None] = mapped_column(
        ForeignKey("supplier_quotation_actions.id", ondelete="SET NULL"), index=True
    )
    bound_recipient: Mapped[str | None] = mapped_column(Text)
    approved_recipient: Mapped[str | None] = mapped_column(Text)
    recipient: Mapped[str] = mapped_column(Text)
    cc: Mapped[str] = mapped_column(Text, default="")
    subject: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    immutable_approved_body: Mapped[str | None] = mapped_column(Text)
    approved_subject: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="draft", index=True)
    sent_message_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    sent_at: Mapped[str | None] = mapped_column(String(32))
    idempotency_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    created_at: Mapped[str] = mapped_column(String(32))
    updated_at: Mapped[str] = mapped_column(String(32))


class GmailSupplierReplyRow(Base):
    __tablename__ = "supplier_reply_history"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    gmail_message_id: Mapped[str] = mapped_column(String(36), ForeignKey("gmail_messages.id"), index=True)
    gmail_thread_id: Mapped[str] = mapped_column(String(255), index=True)
    supplier_master_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("supplier_master.id"), index=True)
    quotation_register_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("quotation_register.id"), index=True)
    action: Mapped[str] = mapped_column(String(32), default="received")
    details_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[str] = mapped_column(String(32))


class QuestionSetRow(Base):
    __tablename__ = "question_sets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    shipment_id: Mapped[str] = mapped_column(ForeignKey("shipments.id"), index=True)
    quotation_id: Mapped[str] = mapped_column(ForeignKey("quotations.id"), index=True)
    carrier_id: Mapped[str | None] = mapped_column(ForeignKey("carriers.id"), index=True)
    quotation_version: Mapped[int] = mapped_column(Integer)
    questions_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="draft", index=True)
    model_name: Mapped[str | None] = mapped_column(String(120))
    prompt_version: Mapped[str | None] = mapped_column(String(60))
    created_at: Mapped[str] = mapped_column(String(32))
    updated_at: Mapped[str] = mapped_column(String(32))
    approved_at: Mapped[str | None] = mapped_column(String(32))


class ClarificationDraftRow(Base):
    __tablename__ = "clarification_drafts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    shipment_id: Mapped[str] = mapped_column(ForeignKey("shipments.id"), index=True)
    quotation_id: Mapped[str] = mapped_column(ForeignKey("quotations.id"), index=True)
    carrier_id: Mapped[str | None] = mapped_column(ForeignKey("carriers.id"), index=True)
    question_set_id: Mapped[str] = mapped_column(ForeignKey("question_sets.id"), index=True)
    quotation_version: Mapped[int] = mapped_column(Integer)
    recipient: Mapped[str | None] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="draft", index=True)
    created_at: Mapped[str] = mapped_column(String(32))
    updated_at: Mapped[str] = mapped_column(String(32))
    approved_at: Mapped[str | None] = mapped_column(String(32))


class SupplierReplyRow(Base):
    __tablename__ = "supplier_replies"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    shipment_id: Mapped[str] = mapped_column(ForeignKey("shipments.id"), index=True)
    carrier_id: Mapped[str | None] = mapped_column(ForeignKey("carriers.id"), index=True)
    quotation_id: Mapped[str] = mapped_column(ForeignKey("quotations.id"), index=True)
    intake_item_id: Mapped[str] = mapped_column(ForeignKey("intake_items.id"), unique=True)
    association_method: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[str] = mapped_column(String(32))


class QuotationVersionRow(Base):
    __tablename__ = "quotation_versions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    quotation_id: Mapped[str] = mapped_column(ForeignKey("quotations.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    source_intake_item_id: Mapped[str] = mapped_column(ForeignKey("intake_items.id"), index=True)
    snapshot_json: Mapped[str] = mapped_column(Text)
    evidence_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(32))
    __table_args__ = (UniqueConstraint("quotation_id", "version"),)


Index("ix_intake_sha", IntakeItemRow.content_sha256)


class SourceDocumentRow(Base):
    __tablename__ = "source_documents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    intake_item_id: Mapped[str] = mapped_column(ForeignKey("intake_items.id"), index=True)
    filename: Mapped[str] = mapped_column(Text)
    source_kind: Mapped[str] = mapped_column(String(24))
    text: Mapped[str] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    parse_error: Mapped[str | None] = mapped_column(Text)


class ClarificationTurnRow(Base):
    __tablename__ = "clarification_turns"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    quotation_id: Mapped[str] = mapped_column(ForeignKey("quotations.id"), index=True)
    quotation_version: Mapped[int] = mapped_column(Integer)
    questions_json: Mapped[str] = mapped_column(Text)
    reply_text: Mapped[str | None] = mapped_column(Text)
    result_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(32))


class DestinationImportRow(Base):
    __tablename__ = "destination_imports"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    quotation_id: Mapped[str] = mapped_column(ForeignKey("quotations.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24))
    details_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(32))


class DestinationProposalRow(Base):
    __tablename__ = "destination_proposals"
    __table_args__ = (UniqueConstraint("intake_id", "database_id", "revision_key", name="uq_destination_source"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    intake_id: Mapped[str] = mapped_column(ForeignKey("intake_items.id"), index=True)
    database_id: Mapped[str] = mapped_column(ForeignKey("supplier_databases.id"), index=True)
    status: Mapped[str] = mapped_column(String(24), index=True)
    schema_json: Mapped[str] = mapped_column(Text)
    revision_key: Mapped[str] = mapped_column(String(64))
    source_hash: Mapped[str] = mapped_column(String(64))
    result_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(32))
    updated_at: Mapped[str] = mapped_column(String(32))
