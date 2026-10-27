from __future__ import annotations

import json
from collections import Counter

import httpx2
from pydantic import ValidationError
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior

from ..config import Settings
from ..domain.exceptions import ModelUnavailableError
from ..domain.models import GapAnalysisResult, Shipment
from . import prompts
from .model import make_agent


def build_gap_agent(settings: Settings) -> Agent:
    return make_agent(settings, GapAnalysisResult, prompts.GAP_SYSTEM, "gap_agent")


def _valid_wording(result: GapAnalysisResult, analysis: GapAnalysisResult) -> bool:
    # Compare entire gap semantics, including multiplicity; field sets alone are unsafe.
    def gap_keys(value: GapAnalysisResult) -> Counter:
        return Counter(json.dumps(g.model_dump(mode="json"), sort_keys=True)
                       for g in value.gaps)

    if gap_keys(result) != gap_keys(analysis):
        return False
    if (result.comparison_readiness != analysis.comparison_readiness
            or result.summary != analysis.summary):
        return False
    expected = {q.field_name: q for q in analysis.clarification_questions}
    if len(expected) != len(analysis.clarification_questions):
        return False
    if len(result.clarification_questions) != len(expected):
        return False
    seen: set[str] = set()
    texts: set[str] = set()
    for question in result.clarification_questions:
        original = expected.get(question.field_name)
        if original is None or question.field_name in seen:
            return False
        seen.add(question.field_name)
        if (question.model_dump(exclude={"question_text"})
                != original.model_dump(exclude={"question_text"})):
            return False
        text = question.question_text.strip()
        if (not text or len(text) > 600 or text.casefold() in texts
                or any(ord(c) < 32 or ord(c) == 127 for c in text)
                or "<" in text or ">" in text):
            return False
        # Evidence is for the employee only, never the carrier-facing email.
        for item in analysis.clarification_questions:
            snippet = item.source_context.strip()
            if snippet != "not found" and len(snippet) >= 8 and snippet.casefold() in text.casefold():
                return False
        texts.add(text.casefold())
    return True


def word_questions(agent: Agent, shipment: Shipment, analysis: GapAnalysisResult,
                   settings: Settings) -> GapAnalysisResult:
    """Bounded wording-only call. Invalid output keeps authoritative rule templates.

    Transport/model availability failures are not invalid-output fallbacks: the caller
    receives an actionable typed error and nothing should be persisted.
    """
    payload = {
        "shipment": {
            "reference": shipment.reference, "origin": shipment.origin,
            "destination": shipment.destination,
            "required_delivery_scope": str(shipment.required_delivery_scope),
        },
        "gaps": [g.model_dump(mode="json") for g in analysis.gaps],
        "existing_questions": [q.model_dump(mode="json") for q in analysis.clarification_questions],
        "comparison_readiness": str(analysis.comparison_readiness),
        "summary": analysis.summary,
    }
    user_prompt = (
        f"Clarification prompt version: {prompts.CLARIFICATION_PROMPT_VERSION}\n"
        "Improve only the question wording for these gaps (do not add or drop gaps):\n"
        + prompts.wrap_untrusted("gap_context.json", json.dumps(payload, indent=2), source="rule-engine")
    )
    try:
        result = GapAnalysisResult.model_validate(agent.run_sync(user_prompt).output)
    except (httpx2.HTTPError, ModelHTTPError, ConnectionError, TimeoutError) as exc:
        raise ModelUnavailableError(
            f"Local Ollama is unavailable at {settings.ollama_base_url} for model "
            f"{settings.ollama_model!r}. Start `ollama serve`, check `ollama list`, "
            "and pull/configure the selected model before retrying clarification generation."
        ) from exc
    except (UnexpectedModelBehavior, ValidationError):
        return analysis
    if not _valid_wording(result, analysis):
        return analysis
    texts = {q.field_name: q.question_text.strip() for q in result.clarification_questions}
    # Never return model-owned gap objects, identity, evidence, or priorities.
    return analysis.model_copy(update={
        "clarification_questions": [q.model_copy(update={"question_text": texts[q.field_name]})
                                    for q in analysis.clarification_questions],
    })
