from __future__ import annotations

from pydantic import ValidationError
from pydantic_ai import Agent
from pydantic_ai.exceptions import UnexpectedModelBehavior

from ..config import Settings
from ..domain.enums import DocumentType
from ..domain.models import ClassificationResult
from . import prompts
from .model import build_ollama_model, make_agent


def build_classifier_agent(settings: Settings) -> Agent:
    return make_agent(settings, ClassificationResult, prompts.CLASSIFIER_SYSTEM, "classifier")


def run_classification(agent: Agent, filename: str, text: str,
                       settings: Settings) -> ClassificationResult:
    """Classify one intake document. On bounded-retry failure, mark needs_review
    rather than inventing a result (spec §8)."""
    user_prompt = prompts.wrap_untrusted(filename, text)
    try:
        result = agent.run_sync(user_prompt)
        outcome = result.output
    except (UnexpectedModelBehavior, ValidationError) as exc:
        return ClassificationResult(
            is_freight_quote=False,
            reason=f"model output failed schema validation after bounded retries: {exc}",
            confidence=0.0,
            needs_human_review=True,
            document_type=DocumentType.ambiguous,
        )
    if outcome.confidence < settings.classification_confidence_threshold:
        outcome = outcome.model_copy(update={"needs_human_review": True})
    return outcome
