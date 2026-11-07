"""Conversational wording for answers the Python layer has already worked out.

Ranking, prices and dates are computed deterministically in the service; this agent only changes
tone. Every number and name in the findings must survive the rewording, so the output is checked
against the input and the plain sentence is used when the model deviates.
"""
from __future__ import annotations

import json
import re

import httpx2
from pydantic import ValidationError
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelHTTPError, ModelTimeoutError, UnexpectedModelBehavior

from ..config import Settings
from ..domain.exceptions import ModelUnavailableError
from ..domain.models import ChatReply
from . import prompts
from .model import make_agent

PROMPT_VERSION = prompts.CHAT_PROMPT_VERSION
_NUMBER = re.compile(r"\d[\d.,]*%?")
_BANNED = re.compile(r"<|>|\*|#[a-z]|\n")


def build_chat_agent(settings: Settings) -> Agent:
    return make_agent(settings, ChatReply, prompts.CHAT_SYSTEM, "quotation_chat_wording")


def tokens(text: str) -> set[str]:
    return set(_NUMBER.findall(text))


def faithful(findings: str, reply: str) -> bool:
    """The wording may change; the arithmetic, dates and units may not."""
    if _BANNED.search(reply):
        return False
    said = tokens(reply)
    given = tokens(findings)
    return said <= given and given <= said and len(reply) <= max(200, len(findings) * 3)


def phrase(settings: Settings, *, question: str, findings: str) -> str:
    """Return natural wording for `findings`, or raise ModelUnavailableError for the caller to fall back."""
    payload = {"question": question, "verified_findings": findings}
    user_prompt = (
        f"Wording prompt version: {PROMPT_VERSION}\n"
        "Say the verified findings in your own words:\n"
        + prompts.wrap_untrusted("chat_findings.json", json.dumps(payload, indent=2), source="rule-engine")
    )
    agent = build_chat_agent(settings)
    try:
        result = ChatReply.model_validate(agent.run_sync(user_prompt).output)
    except (httpx2.HTTPError, ModelHTTPError, ModelTimeoutError, ConnectionError, TimeoutError) as exc:
        raise ModelUnavailableError(
            f"Local Ollama did not answer in time at {settings.ollama_base_url} for model "
            f"{settings.ollama_model!r}."
        ) from exc
    except (UnexpectedModelBehavior, ValidationError) as exc:
        raise ModelUnavailableError("The local model returned wording that failed validation.") from exc
    except Exception as exc:  # noqa: BLE001 — any model failure must still leave the plain answer
        raise ModelUnavailableError(
            f"The local model stopped early ({type(exc).__name__}).") from exc
    reply = " ".join(result.reply.split())
    if not faithful(findings, reply):
        raise ModelUnavailableError(
            "The local model changed a figure or a date while rephrasing; the plain answer was kept.")
    return reply


__all__ = ["ChatReply", "build_chat_agent", "faithful", "phrase", "tokens"]
