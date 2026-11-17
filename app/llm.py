"""The only module that talks to the LLM provider. Swapping providers means rewriting this file, nothing else.

What it adds on top of the SDK:
- a fallback chain: if a model is overloaded (503/429) or times out, retry briefly, then move to the next model
- structured output validated against a Pydantic model, with one repair retry
- tool calling (the model decides which Python functions to call), with every call recorded for tracing
- embeddings for documents and queries
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

from google import genai
from google.genai import errors, types
from pydantic import BaseModel, ValidationError

from app.config import get_settings
from app.logs import log

T = TypeVar("T", bound=BaseModel)

RETRYABLE_CODES = {429, 500, 502, 503, 504}
ATTEMPTS_PER_MODEL = 1      # the next model in the chain is the retry; waiting on an overloaded model just adds latency
COOLDOWN_S = 300            # a model that just failed is skipped for 5 minutes (simple circuit breaker)
_cooldown_until: dict[str, float] = {}


class LLMError(RuntimeError):
    """Raised when every model in the chain failed. Callers turn this into a friendly Slack message."""


@dataclass
class LLMResult:
    text: str
    model: str
    latency_ms: int
    tokens_in: int = 0
    tokens_out: int = 0
    parsed: Any = None
    tool_calls: list[dict] = field(default_factory=list)


_client: genai.Client | None = None


def client() -> genai.Client:
    global _client
    if _client is None:
        s = get_settings()
        _client = genai.Client(api_key=s.gemini_api_key,
                               http_options=types.HttpOptions(timeout=s.llm_timeout_s * 1000))
    return _client


def _chain(tier: str) -> list[str]:
    """Models to try, in order, skipping any still cooling down after a recent failure."""
    s = get_settings()
    first = s.llm_model_strong if tier == "strong" else s.llm_model_fast
    chain = [first] + [m for m in s.fallback_models if m != first]
    now = time.monotonic()
    healthy = [m for m in chain if _cooldown_until.get(m, 0) <= now]
    return healthy or chain  # if everything is cooling down, try them all anyway


def _is_retryable(e: Exception) -> bool:
    if isinstance(e, errors.APIError):
        return getattr(e, "code", None) in RETRYABLE_CODES
    # timeouts and dropped connections surface as httpx / OS errors
    name = type(e).__name__.lower()
    return "timeout" in name or "connect" in name or "remoteprotocol" in name


def _call(tier: str, contents: Any, config: types.GenerateContentConfig) -> tuple[Any, str, int]:
    """Run one generate_content call through the fallback chain. Returns (response, model, latency_ms)."""
    last: Exception | None = None
    for model in _chain(tier):
        for attempt in range(1, ATTEMPTS_PER_MODEL + 1):
            t0 = time.monotonic()
            try:
                resp = client().models.generate_content(model=model, contents=contents, config=config)
                return resp, model, int((time.monotonic() - t0) * 1000)
            except Exception as e:  # noqa: BLE001 - classified below
                last = e
                if not _is_retryable(e):
                    raise LLMError(f"{model} rejected the request: {e}") from e
                log.warning("llm.retry", model=model, attempt=attempt, error=str(e)[:160])
                if attempt < ATTEMPTS_PER_MODEL:
                    time.sleep(2 * attempt)
        _cooldown_until[model] = time.monotonic() + COOLDOWN_S
        log.warning("llm.fallback", from_model=model, cooldown_s=COOLDOWN_S)
    raise LLMError(f"All models busy or unreachable: {last}") from last


def _usage(resp: Any) -> tuple[int, int]:
    u = getattr(resp, "usage_metadata", None)
    return (getattr(u, "prompt_token_count", 0) or 0, getattr(u, "candidates_token_count", 0) or 0)


def generate_text(prompt: str, *, system: str, tier: str = "strong", temperature: float = 0.4) -> LLMResult:
    cfg = types.GenerateContentConfig(system_instruction=system, temperature=temperature)
    resp, model, ms = _call(tier, prompt, cfg)
    tin, tout = _usage(resp)
    return LLMResult(text=(resp.text or "").strip(), model=model, latency_ms=ms, tokens_in=tin, tokens_out=tout)


def generate_structured(prompt: str, schema: type[T], *, system: str, tier: str = "strong",
                        temperature: float = 0.2) -> LLMResult:
    """Ask for JSON matching `schema`. If it does not validate, send the error back once and ask for a fix."""
    cfg = types.GenerateContentConfig(system_instruction=system, temperature=temperature,
                                      response_mime_type="application/json", response_schema=schema)
    contents: list[Any] = [prompt]
    for repair in range(2):
        resp, model, ms = _call(tier, contents, cfg)
        tin, tout = _usage(resp)
        try:
            parsed = resp.parsed if isinstance(resp.parsed, schema) else schema.model_validate_json(resp.text or "")
            return LLMResult(text=resp.text or "", model=model, latency_ms=ms, tokens_in=tin, tokens_out=tout, parsed=parsed)
        except (ValidationError, json.JSONDecodeError, ValueError) as e:
            log.warning("llm.invalid_json", model=model, repair=repair, error=str(e)[:200])
            contents = [prompt, resp.text or "", f"That JSON did not match the schema: {e}. Return corrected JSON only."]
    raise LLMError(f"Model output never matched {schema.__name__}")


def run_with_tools(prompt: str, tools: list[Callable[..., Any]], *, system: str, tier: str = "strong",
                   max_calls: int = 8, temperature: float = 0.2) -> LLMResult:
    """Let the model call our Python functions (tool calling) until it produces a final answer.

    Functions need type hints and a docstring: the SDK turns them into tool declarations.
    Each call is recorded so the agent run can be traced and shown in the demo.
    """
    calls: list[dict] = []

    def traced(fn: Callable[..., Any]) -> Callable[..., Any]:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            out = fn(*args, **kwargs)
            calls.append({"tool": fn.__name__, "args": kwargs, "result_preview": str(out)[:300]})
            return out
        wrapper.__name__, wrapper.__doc__ = fn.__name__, fn.__doc__
        wrapper.__annotations__ = fn.__annotations__
        wrapper.__signature__ = __import__("inspect").signature(fn)  # keeps the parameter schema for the SDK
        return wrapper

    cfg = types.GenerateContentConfig(
        system_instruction=system, temperature=temperature, tools=[traced(t) for t in tools],
        automatic_function_calling=types.AutomaticFunctionCallingConfig(maximum_remote_calls=max_calls))
    resp, model, ms = _call(tier, prompt, cfg)
    tin, tout = _usage(resp)
    return LLMResult(text=(resp.text or "").strip(), model=model, latency_ms=ms, tokens_in=tin, tokens_out=tout, tool_calls=calls)


def embed(texts: list[str], *, task: str = "RETRIEVAL_DOCUMENT", dims: int = 768) -> list[list[float]]:
    """Embed in batches. task = RETRIEVAL_DOCUMENT for stored chunks, RETRIEVAL_QUERY for searches."""
    s = get_settings()
    out: list[list[float]] = []
    for i in range(0, len(texts), 100):
        batch = texts[i:i + 100]
        for attempt in range(1, 4):
            try:
                r = client().models.embed_content(
                    model=s.embed_model, contents=batch,
                    config=types.EmbedContentConfig(task_type=task, output_dimensionality=dims))
                out.extend(e.values for e in r.embeddings)
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 3 or not _is_retryable(e):
                    raise LLMError(f"Embedding failed: {e}") from e
                time.sleep(2 * attempt)
    return out
