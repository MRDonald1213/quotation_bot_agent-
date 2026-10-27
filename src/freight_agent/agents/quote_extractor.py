from __future__ import annotations

from pydantic import ValidationError
from pydantic_ai import Agent
from pydantic_ai.exceptions import UnexpectedModelBehavior

from ..config import Settings
from ..domain.exceptions import LLMOutputInvalidError
from ..domain.models import QuoteExtraction
from . import prompts
from .model import make_agent


def build_extractor_agent(settings: Settings) -> Agent:
    return make_agent(settings, QuoteExtraction, prompts.EXTRACTOR_SYSTEM, "quote_extractor")


def run_extraction(agent: Agent, filename: str, text: str,
                   settings: Settings) -> QuoteExtraction:
    user_prompt = (
        prompts.wrap_untrusted(filename, text)
        + "\n\nExtract the QuoteExtraction now. Remember: unknown=null, evidence required."
    )
    try:
        return agent.run_sync(user_prompt).output
    except (UnexpectedModelBehavior, ValidationError) as exc:
        raise LLMOutputInvalidError(
            f"extraction output failed schema validation after bounded retries: {exc}"
        ) from exc
