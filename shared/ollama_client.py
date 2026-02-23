"""
Ollama LLM model router for CharityAI.

Routes inference tasks to the correct Ollama model and implements
a primary → fallback chain per the architecture spec.

Model assignments:
  Task                         Primary     Fallback
  ─────────────────────────────────────────────────
  Email classification         phi3        mistral
  Structured extraction        mistral     llama3
  Multi-source synthesis       mistral     llama3
  Impact scale extraction      llama3      mistral
  Priority justification       llama3      mistral
  PA Slack summary drafting    llama3      mistral
  Confirmation email drafting  llama3      mistral
  RSVP intent classification   phi3        mistral

Usage:
    from shared.ollama_client import OllamaRouter, ModelTask

    router = OllamaRouter()
    result = await router.complete(ModelTask.EMAIL_CLASSIFY, prompt="...")
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Optional

import httpx
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)

# Confidence threshold — if LLM returns below this, trigger fallback
_MIN_CONFIDENCE = 0.6


class ModelTask(str, Enum):
    """Named tasks that map to a specific model assignment."""
    EMAIL_CLASSIFY        = "email_classify"
    STRUCTURED_EXTRACT    = "structured_extract"
    VERIFICATION_SYNTH    = "verification_synth"
    IMPACT_EXTRACT        = "impact_extract"
    PRIORITY_JUSTIFY      = "priority_justify"
    PRIORITY_SCORE        = "priority_score"       # Alias used by scorer.py
    PA_SUMMARY_DRAFT      = "pa_summary_draft"
    CONFIRMATION_EMAIL    = "confirmation_email"
    EMAIL_COMPOSE         = "email_compose"         # Alias used by composer.py
    RSVP_CLASSIFY         = "rsvp_classify"


class OllamaSettings(BaseSettings):
    OLLAMA_BASE_URL:         str = "http://localhost:11434"
    OLLAMA_MODEL_PHI3:       str = "phi3"
    OLLAMA_MODEL_MISTRAL:    str = "tinyllama"
    OLLAMA_MODEL_LLAMA3:     str = "phi3"

    class Config:
        env_file = ".env"
        extra = "ignore"


class OllamaRouter:
    """Routes LLM tasks to the correct model with primary/fallback chain."""

    def __init__(self) -> None:
        self._settings = OllamaSettings()
        self._base_url = self._settings.OLLAMA_BASE_URL

        # Task → (primary_model, fallback_model)
        self._routing: dict[ModelTask, tuple[str, str]] = {
            ModelTask.EMAIL_CLASSIFY:     (self._settings.OLLAMA_MODEL_PHI3,    self._settings.OLLAMA_MODEL_MISTRAL),
            ModelTask.STRUCTURED_EXTRACT: (self._settings.OLLAMA_MODEL_MISTRAL, self._settings.OLLAMA_MODEL_LLAMA3),
            ModelTask.VERIFICATION_SYNTH: (self._settings.OLLAMA_MODEL_MISTRAL, self._settings.OLLAMA_MODEL_LLAMA3),
            ModelTask.IMPACT_EXTRACT:     (self._settings.OLLAMA_MODEL_LLAMA3,  self._settings.OLLAMA_MODEL_MISTRAL),
            ModelTask.PRIORITY_JUSTIFY:   (self._settings.OLLAMA_MODEL_LLAMA3,  self._settings.OLLAMA_MODEL_MISTRAL),
            ModelTask.PRIORITY_SCORE:     (self._settings.OLLAMA_MODEL_LLAMA3,  self._settings.OLLAMA_MODEL_MISTRAL),
            ModelTask.PA_SUMMARY_DRAFT:   (self._settings.OLLAMA_MODEL_LLAMA3,  self._settings.OLLAMA_MODEL_MISTRAL),
            ModelTask.CONFIRMATION_EMAIL: (self._settings.OLLAMA_MODEL_LLAMA3,  self._settings.OLLAMA_MODEL_MISTRAL),
            ModelTask.EMAIL_COMPOSE:      (self._settings.OLLAMA_MODEL_LLAMA3,  self._settings.OLLAMA_MODEL_MISTRAL),
            ModelTask.RSVP_CLASSIFY:      (self._settings.OLLAMA_MODEL_PHI3,    self._settings.OLLAMA_MODEL_MISTRAL),
        }

    async def complete(
        self,
        task: ModelTask,
        prompt: str,
        system: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> str:
        """
        Run inference for the given task, trying primary model first.
        Falls back to secondary model if primary fails or returns empty.

        Returns:
            The model's text response.

        Raises:
            RuntimeError: If both primary and fallback model fail (MODEL_FAILURE).
        """
        primary, fallback = self._routing[task]

        # Attempt primary
        result = await self._call(primary, prompt, system, temperature, max_tokens)
        if result:
            return result

        logger.warning("Primary model %s failed for task %s — trying fallback %s", primary, task, fallback)

        # Attempt fallback
        result = await self._call(fallback, prompt, system, temperature, max_tokens)
        if result:
            return result

        # Both failed — signal MODEL_FAILURE
        logger.error("Both models failed for task %s [primary=%s fallback=%s]", task, primary, fallback)
        raise ModelFailureError(task=task, primary=primary, fallback=fallback)

    async def _call(
        self,
        model: str,
        prompt: str,
        system: Optional[str],
        temperature: float,
        max_tokens: int,
    ) -> Optional[str]:
        """
        Single model inference call via Ollama /api/generate.
        Returns response text, or None on any error.
        """
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }
        if system:
            payload["system"] = system

        try:
            async with httpx.AsyncClient(timeout=180) as client:
                resp = await client.post(
                    f"{self._base_url}/api/generate",
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
                text = data.get("response", "").strip()
                logger.debug("Ollama %s responded (%d chars)", model, len(text))
                return text if text else None

        except Exception as exc:
            logger.warning("Ollama %s error: %s", model, exc)
            return None

    async def is_available(self, model: str) -> bool:
        """Check if a specific model is available in Ollama."""
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{self._base_url}/api/tags")
                resp.raise_for_status()
                models = [m["name"].split(":")[0] for m in resp.json().get("models", [])]
                return model in models
        except Exception:
            return False

    async def list_available_models(self) -> list[str]:
        """List all models available in the local Ollama server."""
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{self._base_url}/api/tags")
                resp.raise_for_status()
                return [m["name"] for m in resp.json().get("models", [])]
        except Exception as exc:
            logger.error("Could not list Ollama models: %s", exc)
            return []


class ModelFailureError(Exception):
    """
    Raised when both primary and fallback models fail.
    The orchestrator catches this and sets state = MODEL_FAILURE.
    """
    def __init__(self, task: ModelTask, primary: str, fallback: str):
        self.task = task
        self.primary = primary
        self.fallback = fallback
        super().__init__(
            f"MODEL_FAILURE: Both {primary} and {fallback} failed for task '{task}'"
        )


# ── Singleton instance for agents that don't want to construct their own ──────
_router: Optional[OllamaRouter] = None


def get_router() -> OllamaRouter:
    global _router
    if _router is None:
        _router = OllamaRouter()
    return _router
