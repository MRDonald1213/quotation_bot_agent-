from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    def __str__(self) -> str:  # pragma: no cover
        return self.value


class ShipmentStatus(StrEnum):
    open = "open"
    closed = "closed"
    needs_review = "needs_review"


class DeliveryScope(StrEnum):
    port_to_port = "port_to_port"
    door_to_door = "door_to_door"
    door_to_port = "door_to_port"
    port_to_door = "port_to_door"
    unknown = "unknown"


class SourceType(StrEnum):
    upload = "upload"
    imported_email = "imported_email"
    imported_reply = "imported_reply"


class ClassificationStatus(StrEnum):
    pending = "pending"
    relevant = "relevant"
    irrelevant = "irrelevant"
    needs_review = "needs_review"
    failed = "failed"


class ProcessingStatus(StrEnum):
    pending = "pending"
    processing = "processing"
    processed = "processed"
    needs_review = "needs_review"
    failed = "failed"


class ValidationStatus(StrEnum):
    accepted = "accepted"
    needs_review = "needs_review"
    rejected = "rejected"


class ComparisonStatus(StrEnum):
    comparable = "comparable"
    incomplete = "incomplete"
    expired = "expired"
    needs_review = "needs_review"


class DocumentType(StrEnum):
    carrier_quote = "carrier_quote"
    rfq = "rfq"
    supplier_reply = "supplier_reply"
    invoice = "invoice"
    unrelated = "unrelated"
    ambiguous = "ambiguous"


class GapType(StrEnum):
    missing = "missing"
    ambiguous = "ambiguous"
    conflicting = "conflicting"
    expired = "expired"
    scope_mismatch = "scope_mismatch"


class GapSeverity(StrEnum):
    critical = "critical"
    important = "important"
    informational = "informational"


class GapStatus(StrEnum):
    open = "open"
    resolved = "resolved"
    waived = "waived"


class DraftStatus(StrEnum):
    draft = "draft"
    approved = "approved"
    exported = "exported"
    sent_external = "sent_external"
    cancelled = "cancelled"


class QuestionSetStatus(StrEnum):
    draft = "draft"
    approved = "approved"
    superseded = "superseded"


class AssociationMethod(StrEnum):
    manual = "manual"
    explicit_reference = "explicit_reference"
    suggested_match = "suggested_match"


class ComparisonReadiness(StrEnum):
    ready = "ready"
    partially_ready = "partially_ready"
    not_ready = "not_ready"
