from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic_ai import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from freight_agent.config import Settings
from freight_agent.db import engine as db_engine

FIXTURES = Path(__file__).parent / "fixtures" / "sample_quotes"


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    s = Settings(
        _env_file=None,
        demo_login_enabled=False, gmail_background_enabled=False,
        ollama_base_url="http://localhost:11434/v1",
        ollama_model="fake-model",
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        workspace_dir=tmp_path / "workspace",
        llm_max_retries=0,
    )
    from freight_agent.db.engine import get_session
    from freight_agent.db.migrations import init_schema
    session = get_session(s)
    init_schema(session)
    session.close()
    return s


class FakeLLM:
    """Routes by prompt marker; records every prompt received for assertions."""

    def __init__(self) -> None:
        self.responses: dict[str, object] = {}
        self.prompts: list[str] = []

    def handler(self, messages, ctx):
        text = json.dumps([m.part_kind if hasattr(m, "part_kind") else str(m) for m in messages])
        combined = str(messages)
        self.prompts.append(combined)
        for marker, payload in self.responses.items():
            if marker in combined:
                if isinstance(payload, Exception):
                    raise payload
                return ModelResponse(parts=[TextPart(json.dumps(payload))])
        raise AssertionError(f"FakeLLM has no response for prompt; markers={list(self.responses)}\n{text[:400]}")

    def model(self) -> FunctionModel:
        return FunctionModel(self.handler)


@pytest.fixture(autouse=True)
def fake_llm(monkeypatch) -> FakeLLM:
    fake = FakeLLM()
    monkeypatch.setattr("freight_agent.agents.model.build_ollama_model",
                        lambda settings: fake.model())
    return fake


@pytest.fixture()
def quote_a_file() -> Path:
    return FIXTURES / "quote_a_baltic_lines.txt"


@pytest.fixture()
def newsletter_file() -> Path:
    return FIXTURES / "unrelated_newsletter.txt"
