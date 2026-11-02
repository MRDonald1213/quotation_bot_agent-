from __future__ import annotations

import json
import re

import httpx2
from pydantic import ValidationError
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelHTTPError, ModelTimeoutError, UnexpectedModelBehavior

from ..config import Settings
from ..domain.exceptions import FreightAgentError, ModelUnavailableError
from ..domain.models import SupplierDraftWordings
from . import prompts
from .model import make_agent

PROMPT_VERSION = "supplier-draft-2026-11-02.1"
_NUMBERED_LINE = re.compile(r"^\s*\d+[.)]\s+\S", re.MULTILINE)


def build_supplier_draft_agent(settings: Settings) -> Agent:
    return make_agent(settings, SupplierDraftWordings, prompts.SUPPLIER_DRAFT_SYSTEM, "supplier_draft_writer")


def acceptable(wordings: SupplierDraftWordings, question_count: int) -> bool:
    """Structural checks only: the model never decides who the mail goes to or what is missing."""
    subject, body = wordings.subject.strip(), wordings.body.strip()
    if "\n" in subject or "<" in subject or ">" in subject:
        return False
    if "<" in body or ">" in body:
        return False
    if any(ord(c) < 32 or ord(c) == 127 for c in subject + body.replace("\n", "")):
        return False
    if "Kind regards" not in body:
        return False
    if len(_NUMBERED_LINE.findall(body)) < question_count:
        return False
    return len(body) >= 20


def word_draft(settings: Settings, *, supplier_name: str, missing: list[dict],
               extracted: dict[str, str]) -> SupplierDraftWordings:
    """Write the supplier-facing request. Invalid wording is an error the caller falls back from.

    Transport failures raise ModelUnavailableError so the caller records that the local model
    did not answer, rather than silently shipping template text.
    """
    payload = {
        "supplier": supplier_name,
        "missing_fields": [{"label": item.get("label") or item.get("field"),
                           "why": item.get("reason", "")} for item in missing],
        "already_extracted": sorted(extracted.items()),
    }
    user_prompt = (
        f"Draft prompt version: {PROMPT_VERSION}\n"
        "Write the supplier email for exactly these missing points:\n"
        + prompts.wrap_untrusted("supplier_draft_context.json",
                                 json.dumps(payload, indent=2), source="rule-engine")
    )
    agent = build_supplier_draft_agent(settings)
    try:
        result = SupplierDraftWordings.model_validate(agent.run_sync(user_prompt).output)
    except (httpx2.HTTPError, ModelHTTPError, ModelTimeoutError, ConnectionError, TimeoutError) as exc:
        raise ModelUnavailableError(
            f"Local Ollama did not answer in time at {settings.ollama_base_url} for model "
            f"{settings.ollama_model!r}; the email was written from the standard template instead."
        ) from exc
    except (UnexpectedModelBehavior, ValidationError) as exc:
        raise ModelUnavailableError(
            "The local model returned wording that failed validation; "
            "the email was written from the standard template instead."
        ) from exc
    except FreightAgentError:
        raise
    except Exception as exc:  # noqa: BLE001 — a model that cannot answer must still leave a readable draft
        raise ModelUnavailableError(
            f"The local model stopped early ({type(exc).__name__}); "
            "the email was written from the standard template instead."
        ) from exc
    if not acceptable(result, len(missing)):
        raise ModelUnavailableError(
            "The local model returned wording that did not ask for every missing point; "
            "the email was written from the standard template instead."
        )
    return result


__all__ = ["SupplierDraftWordings", "acceptable", "build_supplier_draft_agent", "word_draft"]
