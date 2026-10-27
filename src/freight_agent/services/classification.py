from __future__ import annotations

import json

from ..config import Settings
from ..agents import prompts
from ..agents.classifier import build_classifier_agent, run_classification
from ..domain.enums import ClassificationStatus
from ..domain.models import AuditEvent, ClassificationResult, IntakeItem
from .common import with_session

_TRUNCATION_NOTE = "\n\n[...document truncated for model context...]"


def truncate_for_llm(text: str, settings: Settings) -> str:
    limit = settings.max_text_chars
    if len(text) <= limit:
        return text
    return text[:limit] + _TRUNCATION_NOTE


def classify_intake(settings: Settings, intake_id: str, dry_run: bool = False
                    ) -> tuple[IntakeItem, ClassificationResult]:
    with with_session(settings) as repos:
        item = repos.get_intake(intake_id)
        agent = build_classifier_agent(settings)
        result = run_classification(agent, item.original_filename,
                                    truncate_for_llm(item.raw_text, settings), settings)

        if result.needs_human_review:
            status = ClassificationStatus.needs_review
        elif result.is_freight_quote:
            status = ClassificationStatus.relevant
        else:
            status = ClassificationStatus.irrelevant

        updated = item.model_copy(update={
            "classification_status": status,
            "classification_reason": result.reason,
            "classification_confidence": result.confidence,
        })
        if not dry_run:
            repos.update_intake(updated, classification_json=result.model_dump_json())
            repos.add_audit(AuditEvent(
                entity_type="intake_item", entity_id=item.id, action="classified",
                details_json=json.dumps({
                    "status": str(status), "confidence": result.confidence,
                    "threshold": settings.classification_confidence_threshold,
                    "model": settings.ollama_model, "prompt_version": prompts.PROMPT_VERSION,
                }),
            ))
        return updated, result
