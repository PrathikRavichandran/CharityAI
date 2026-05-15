"""
LLM provider abstraction for CharityAI.

Selects between Ollama (local) and Anthropic (cloud) based on the
LLM_PROVIDER env var. The existing OllamaRouter remains the local default;
the Anthropic backend exists so the cloud demo (HF Spaces) can run without
a local Ollama install.

Usage (drop-in replacement for OllamaRouter):

    from shared.llm import get_llm, ModelTask

    llm = get_llm()
    text = await llm.complete(ModelTask.EMAIL_CLASSIFY, prompt="...")
"""

from __future__ import annotations

import logging
import os
from typing import Optional, Protocol

from shared.ollama_client import (
    ModelFailureError,
    ModelTask,
    OllamaRouter,
)

logger = logging.getLogger(__name__)


class LLMRouter(Protocol):
    async def complete(
        self,
        task: ModelTask,
        prompt: str,
        system: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> str: ...


# ── Anthropic backend ─────────────────────────────────────────────────────────


class AnthropicRouter:
    """
    Minimal Anthropic backend that satisfies the same complete() contract
    as OllamaRouter. Two model tiers:

        FAST  → claude-haiku-4-5     (classify, dedup, RSVP intent)
        SMART → claude-sonnet-4-6    (extract, synthesize, draft, prioritize)

    The fast/smart split mirrors the phi3-vs-llama3 split Ollama uses.
    """

    _FAST_TASKS = {
        ModelTask.EMAIL_CLASSIFY,
        ModelTask.RSVP_CLASSIFY,
    }

    def __init__(self) -> None:
        try:
            import anthropic  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "LLM_PROVIDER=anthropic but the `anthropic` package is not "
                "installed. Add `anthropic>=0.40.0` to requirements.txt."
            ) from exc

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "LLM_PROVIDER=anthropic requires ANTHROPIC_API_KEY in env."
            )

        from anthropic import AsyncAnthropic
        self._client = AsyncAnthropic(api_key=api_key)
        self._model_fast = os.environ.get(
            "ANTHROPIC_MODEL_FAST", "claude-haiku-4-5-20251001"
        )
        self._model_smart = os.environ.get(
            "ANTHROPIC_MODEL_SMART", "claude-sonnet-4-6"
        )

    def _model_for(self, task: ModelTask) -> str:
        return self._model_fast if task in self._FAST_TASKS else self._model_smart

    async def complete(
        self,
        task: ModelTask,
        prompt: str,
        system: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> str:
        model = self._model_for(task)
        try:
            resp = await self._client.messages.create(
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system or "",
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(
                block.text for block in resp.content if hasattr(block, "text")
            ).strip()
            if not text:
                raise ModelFailureError(task=task, primary=model, fallback=model)
            return text
        except Exception as exc:
            logger.error("Anthropic %s failed: %s", model, exc)
            raise ModelFailureError(task=task, primary=model, fallback=model) from exc


# ── Provider selector ─────────────────────────────────────────────────────────

_router: Optional[LLMRouter] = None


def get_llm() -> LLMRouter:
    """Return the configured LLM router (singleton).

    Provider is chosen by the LLM_PROVIDER env var:
      - 'ollama'     → OllamaRouter (default)
      - 'anthropic'  → AnthropicRouter
    """
    global _router
    if _router is None:
        provider = os.environ.get("LLM_PROVIDER", "ollama").lower()
        if provider == "anthropic":
            logger.info("LLM provider: anthropic")
            _router = AnthropicRouter()
        else:
            logger.info("LLM provider: ollama")
            _router = OllamaRouter()
    return _router
