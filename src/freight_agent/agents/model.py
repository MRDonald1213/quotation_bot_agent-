from __future__ import annotations

import httpx2

from pydantic_ai import Agent
from pydantic_ai.models.ollama import OllamaModel
from pydantic_ai.providers.ollama import OllamaProvider

from ..config import Settings, is_local_endpoint
from ..domain.exceptions import ConfigurationError


def build_ollama_model(settings: Settings) -> OllamaModel:
    """Local Ollama only. Model name comes from config (never hard-coded);
    no cloud fallback if Ollama is unavailable (spec §2, §8)."""
    if not settings.ollama_model:
        raise ConfigurationError(
            "OLLAMA_MODEL is not configured. Set it in .env to a model from `ollama list`."
        )
    if not is_local_endpoint(settings.ollama_base_url):
        raise ConfigurationError(
            f"OLLAMA_BASE_URL={settings.ollama_base_url} is not a local endpoint. "
            "The MVP requires a local Ollama URL; cloud endpoints are out of scope."
        )
    provider = OllamaProvider(
        base_url=settings.ollama_base_url,
        http_client=httpx2.AsyncClient(timeout=settings.llm_timeout_seconds),
    )
    return OllamaModel(model_name=settings.ollama_model, provider=provider)


def check_ollama(settings: Settings) -> tuple[bool, str, list[str]]:
    """Health probe: reachability + configured model presence. Sends no document text anywhere."""
    import json
    import urllib.error
    import urllib.request

    url = f"{settings.ollama_root_url}/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"Ollama not reachable at {url}: {exc}", []
    models = [m.get("name", "") for m in data.get("models", [])]
    if settings.ollama_model and settings.ollama_model not in models:
        return False, f"configured model {settings.ollama_model!r} not found in `ollama list`", models
    return True, "ok", models


def make_agent(settings: Settings, output_type: type, system_prompt: str, name: str) -> Agent:
    return Agent(
        build_ollama_model(settings),
        output_type=output_type,
        system_prompt=system_prompt,
        retries=settings.llm_max_retries,  # bounded retries
        name=name,
    )
