"""
backend/app/services/llm.py

Shared LLM narration layer over Foundry gpt-4.1. Used by:
  - briefing playbook narrative (on-the-fly, point 4)
  - persona-tailored narratives (on-the-fly, point 5)
  - one-time CRM case enrichment (tools/generate, point 2)

Auth mirrors search.py exactly: in-container managed identity -> bearer token
provider -> OpenAI v1 client. Degrades gracefully (raises LLMUnavailable) when the
Foundry endpoint is not configured (local dev), so callers can fall back to the
deterministic text instead of crashing.

GROUNDING DISCIPLINE: callers pass a structured "evidence" object; the system
prompt instructs the model to narrate ONLY from that evidence and never invent
figures, approvals, or facts. This keeps the on-the-fly narratives auditable.
"""
from __future__ import annotations
import json
import logging
import os
import re
from functools import lru_cache

from app.config import get_settings

log = logging.getLogger("contoso.msme.llm")


class LLMUnavailable(Exception):
    pass


# --- Reasoning vs non-reasoning request shape ---------------------------------------
# Reasoning deployments (gpt-5.x / o-series) reject `temperature` and `max_tokens`; they
# take `max_completion_tokens`, out of which their hidden reasoning tokens are also
# billed. The backend narration path runs on gpt-4.1-mini, which is NOT a reasoning
# model, so in the default deployment this branch never fires and the request shape is
# unchanged. It exists so that pointing FAST_CHAT_DEPLOYMENT (or FOUNDRY_CHAT_DEPLOYMENT)
# at the voice deployment does not silently return empty completions.
_REASONING_NAME_HINT = re.compile(r"(^|[^a-z0-9])(gpt-5|o1|o3|o4)", re.IGNORECASE)
_REASONING_EFFORT = os.getenv("VOICE_AI_REASONING_EFFORT", "low")
_REASONING_MIN_TOKENS = int(os.getenv("VOICE_AI_REASONING_MIN_TOKENS", "512"))
_REASONING_TOKEN_MULTIPLIER = float(os.getenv("VOICE_AI_REASONING_TOKEN_MULTIPLIER", "6"))


def _reasoning_deployments() -> set[str]:
    raw = os.getenv("AI_REASONING_DEPLOYMENTS", "")
    return {p.strip() for p in raw.split(",") if p.strip()}


def is_reasoning_model(model: str) -> bool:
    m = str(model or "")
    return m in _reasoning_deployments() or bool(_REASONING_NAME_HINT.search(m))


def build_params(model: str, *, max_tokens: int | None = None,
                 temperature: float | None = None, json_mode: bool = False) -> dict:
    """Model-specific half of a chat.completions request body."""
    params: dict = {"model": model}
    if is_reasoning_model(model):
        params["reasoning_effort"] = _REASONING_EFFORT
        if max_tokens is not None:
            params["max_completion_tokens"] = max(
                _REASONING_MIN_TOKENS, int(max_tokens * _REASONING_TOKEN_MULTIPLIER)
            )
        # `temperature` is deliberately omitted: reasoning models reject it.
    else:
        if temperature is not None:
            params["temperature"] = temperature
        if max_tokens is not None:
            params["max_tokens"] = max_tokens
    if json_mode:
        params["response_format"] = {"type": "json_object"}
    return params


@lru_cache(maxsize=1)
def _client():
    s = get_settings()
    if not s.foundry_aoai_endpoint:
        raise LLMUnavailable("Foundry AOAI endpoint not configured")
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    from openai import OpenAI

    # Azure AI Foundry /openai/v1 endpoint. This mirrors the validated Playground
    # sample: OpenAI(base_url=..., api_key=<Entra token provider>) with the
    # https://ai.azure.com/.default audience. Do not strip /openai/v1.
    ep = s.foundry_aoai_endpoint.rstrip("/")
    if not ep.endswith("/openai/v1"):
        ep = ep.rstrip("/") + "/openai/v1"
    token_provider = get_bearer_token_provider(
        DefaultAzureCredential(), "https://ai.azure.com/.default"
    )
    client = OpenAI(base_url=ep, api_key=token_provider)
    return client, s.foundry_chat_deployment


SYSTEM_GUARDRAIL = (
    "You are an assistant to a bank Relationship Manager (RM) at Contoso Bank, a "
    "fictional Indian bank serving MSME customers. You produce internal RM-facing "
    "guidance only. Hard rules: (1) Narrate ONLY from the EVIDENCE provided in the "
    "user message — never invent figures, balances, approvals, or facts not present. "
    "(2) Never state or imply a credit decision is approved/sanctioned; enhancements "
    "and renewals are recommendations requiring credit appraisal and human sign-off. "
    "(3) For risk signals, use clarification-seeking language — never allege fraud, "
    "diversion, or wrongdoing. (4) Be specific and practical: an RM should be able to "
    "act on what you write. (5) All data is synthetic; do not add disclaimers about that."
)


def chat(messages: list[dict], temperature: float = 0.5, max_tokens: int = 900,
         deployment: str | None = None) -> str:
    """Single chat completion. Raises LLMUnavailable if Foundry is unreachable.
    Pass `deployment` to override the default chat model (e.g. a faster mini)."""
    client, default_deployment = _client()
    resp = client.chat.completions.create(
        **build_params(deployment or default_deployment,
                       max_tokens=max_tokens, temperature=temperature),
        messages=messages,
    )
    return (resp.choices[0].message.content or "").strip()


def narrate(task_instruction: str, evidence: dict, temperature: float = 0.5,
            max_tokens: int = 900, system_prompt: str | None = None) -> str:
    """Grounded narration: the model narrates the task using ONLY `evidence`."""
    user = (
        f"{task_instruction}\n\n"
        f"EVIDENCE (the only facts you may use):\n```json\n{json.dumps(evidence, indent=2, default=str)}\n```"
    )
    return chat(
        [{"role": "system", "content": system_prompt or SYSTEM_GUARDRAIL},
         {"role": "user", "content": user}],
        temperature=temperature, max_tokens=max_tokens,
    )


def available() -> bool:
    try:
        _client()
        return True
    except Exception:
        return False


def narrate_json(task_instruction: str, evidence: dict, schema_hint: str,
                 temperature: float = 0.5, max_tokens: int = 1400,
                 deployment: str | None = None, system_prompt: str | None = None) -> dict:
    """Grounded narration that returns STRUCTURED JSON. schema_hint describes the
    exact JSON shape required. Parses and returns a dict; raises on bad JSON.
    Pass `deployment` to override the default chat model (e.g. a faster mini)."""
    import json as _json
    user = (
        f"{task_instruction}\n\n"
        f"Return ONLY valid JSON matching this shape (no markdown, no prose outside JSON):\n"
        f"{schema_hint}\n\n"
        f"EVIDENCE (the only facts you may use):\n```json\n{_json.dumps(evidence, indent=2, default=str)}\n```"
    )
    client, default_deployment = _client()
    resp = client.chat.completions.create(
        **build_params(deployment or default_deployment,
                       max_tokens=max_tokens, temperature=temperature, json_mode=True),
        messages=[{"role": "system", "content": system_prompt or SYSTEM_GUARDRAIL},
                  {"role": "user", "content": user}],
    )
    raw = (resp.choices[0].message.content or "{}").strip()
    return _json.loads(raw)
