"""
Unified LLM client for ApplyPilot.

Auto-detects provider from environment:
  ANTHROPIC_API_KEY -> Anthropic Claude (default: claude-sonnet-4-6)
  GEMINI_API_KEY    -> Google Gemini (default: gemini-2.5-flash)
  OPENAI_API_KEY    -> OpenAI (default: gpt-4o-mini)
  LLM_URL           -> Local llama.cpp / Ollama compatible endpoint

LLM_MODEL env var overrides the model name for the default provider.

Per-stage model overrides (heterogeneous routing):
  SCORE_MODEL   -> used by scoring/scorer.py (per-job classifier; prefer cheap models)
  TAILOR_MODEL  -> used by scoring/tailor.py (resume generation; prefer frontier)
  JUDGE_MODEL   -> used by tailor.judge_tailored_resume (fabrication detector)
  COVER_MODEL   -> used by scoring/cover_letter.py (tone/voice; mid-tier is fine)

A per-stage override implies a separate client instance. The provider is
inferred from the model name prefix (claude-*, gemini-*, gpt-*/o1-*). Short
aliases "opus", "sonnet", "haiku" resolve to the current Anthropic model IDs.
"""

import logging
import os
import time

import httpx

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Provider detection
# ---------------------------------------------------------------------------

_ANTHROPIC_API_BASE = "https://api.anthropic.com/v1"
_OPENAI_API_BASE = "https://api.openai.com/v1"

# Short aliases -> current Anthropic model IDs. Update when new models ship.
_CLAUDE_ALIASES = {
    "opus": "claude-opus-4-7",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}


def _detect_provider() -> tuple[str, str, str]:
    """Return (base_url, model, api_key) based on environment variables.

    Reads env at call time (not module import time) so that load_env() called
    in _bootstrap() is always visible here.
    """
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    local_url = os.environ.get("LLM_URL", "")
    model_override = os.environ.get("LLM_MODEL", "")

    # Gemini is checked before Anthropic so that existing wizard-configured
    # setups keep Gemini as the default for scoring volume. Promote a stage
    # to Anthropic via TAILOR_MODEL / JUDGE_MODEL / COVER_MODEL instead.
    if gemini_key and not local_url:
        return (
            _GEMINI_COMPAT_BASE,
            model_override or "gemini-2.5-flash",
            gemini_key,
        )

    if anthropic_key and not local_url:
        return (
            _ANTHROPIC_API_BASE,
            _CLAUDE_ALIASES.get(model_override, model_override) or "claude-sonnet-4-6",
            anthropic_key,
        )

    if openai_key and not local_url:
        return (
            _OPENAI_API_BASE,
            model_override or "gpt-4o-mini",
            openai_key,
        )

    if local_url:
        return (
            local_url.rstrip("/"),
            model_override or "local-model",
            os.environ.get("LLM_API_KEY", ""),
        )

    raise RuntimeError(
        "No LLM provider configured. "
        "Set ANTHROPIC_API_KEY, GEMINI_API_KEY, OPENAI_API_KEY, or LLM_URL."
    )


def _resolve_from_model(spec: str) -> tuple[str, str, str]:
    """Given a model spec (e.g. 'claude-opus-4-7', 'opus', 'gpt-4o-mini'),
    return (base_url, full_model, api_key) for the appropriate provider.
    """
    spec = spec.strip()
    model = _CLAUDE_ALIASES.get(spec, spec)

    if model.startswith("claude-"):
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            raise RuntimeError(
                f"ANTHROPIC_API_KEY required for Claude model '{model}'."
            )
        return (_ANTHROPIC_API_BASE, model, key)

    if model.startswith("gemini-"):
        key = os.environ.get("GEMINI_API_KEY", "")
        if not key:
            raise RuntimeError(
                f"GEMINI_API_KEY required for Gemini model '{model}'."
            )
        return (_GEMINI_COMPAT_BASE, model, key)

    if model.startswith("gpt-") or model.startswith("o1"):
        key = os.environ.get("OPENAI_API_KEY", "")
        if not key:
            raise RuntimeError(
                f"OPENAI_API_KEY required for OpenAI model '{model}'."
            )
        return (_OPENAI_API_BASE, model, key)

    url = os.environ.get("LLM_URL", "")
    if url:
        return (url.rstrip("/"), model, os.environ.get("LLM_API_KEY", ""))

    raise RuntimeError(
        f"Cannot resolve provider for model '{model}'. "
        "Use claude-*, gemini-*, gpt-* prefix, or set LLM_URL."
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

_MAX_RETRIES = 5
_TIMEOUT = 120  # seconds

# Base wait on first 429/503 (doubles each retry, caps at 60s).
# Gemini free tier is 15 RPM = 4s minimum between requests; 10s gives headroom.
_RATE_LIMIT_BASE_WAIT = 10


_GEMINI_COMPAT_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
_GEMINI_NATIVE_BASE = "https://generativelanguage.googleapis.com/v1beta"


class LLMClient:
    """Thin LLM client supporting OpenAI-compatible and native Gemini endpoints.

    For Gemini keys, starts on the OpenAI-compat layer. On a 403 (which
    happens with preview/experimental models not exposed via compat), it
    automatically switches to the native generateContent API and stays there
    for the lifetime of the process.
    """

    def __init__(self, base_url: str, model: str, api_key: str) -> None:
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self._client = httpx.Client(timeout=_TIMEOUT)
        # True once we've confirmed the native Gemini API works for this model
        self._use_native_gemini: bool = False
        self._is_gemini: bool = base_url.startswith(_GEMINI_COMPAT_BASE)
        self._is_anthropic: bool = base_url.startswith(_ANTHROPIC_API_BASE)

    # -- Native Gemini API --------------------------------------------------

    def _chat_native_gemini(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Call the native Gemini generateContent API.

        Used automatically when the OpenAI-compat endpoint returns 403,
        which happens for preview/experimental models not exposed via compat.

        Converts OpenAI-style messages to Gemini's contents/systemInstruction
        format transparently.
        """
        contents: list[dict] = []
        system_parts: list[dict] = []

        for msg in messages:
            role = msg["role"]
            text = msg.get("content", "")
            if role == "system":
                system_parts.append({"text": text})
            elif role == "user":
                contents.append({"role": "user", "parts": [{"text": text}]})
            elif role == "assistant":
                # Gemini uses "model" instead of "assistant"
                contents.append({"role": "model", "parts": [{"text": text}]})

        payload: dict = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if system_parts:
            payload["systemInstruction"] = {"parts": system_parts}

        url = f"{_GEMINI_NATIVE_BASE}/models/{self.model}:generateContent"
        resp = self._client.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            params={"key": self.api_key},
        )
        resp.raise_for_status()
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]

    # -- Native Anthropic API -----------------------------------------------

    def _chat_native_anthropic(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Call the native Anthropic /v1/messages API.

        The Anthropic schema separates the system prompt from the message
        list, so we flatten any system messages into a single `system` field.
        """
        system_parts: list[str] = []
        claude_messages: list[dict] = []
        for msg in messages:
            if msg["role"] == "system":
                system_parts.append(msg.get("content", ""))
            else:
                claude_messages.append(
                    {"role": msg["role"], "content": msg.get("content", "")}
                )

        payload: dict = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": claude_messages,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        resp = self._client.post(
            f"{self.base_url}/messages",
            json=payload,
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()
        # content is a list of blocks; concatenate text blocks in order.
        parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        return "".join(parts)

    # -- OpenAI-compat API --------------------------------------------------

    def _chat_compat(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Call the OpenAI-compatible endpoint."""
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        resp = self._client.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers=headers,
        )

        # 403 or 404 on Gemini compat = model not exposed on compat layer
        # (preview/experimental models are often compat-forbidden with 403;
        # deprecated models are removed with 404). Raise a sentinel so chat()
        # can retry via the native generateContent API, which exposes a
        # wider set of models.
        if resp.status_code in (403, 404) and self._is_gemini:
            raise _GeminiCompatUnavailable(resp)

        return self._handle_compat_response(resp)

    @staticmethod
    def _handle_compat_response(resp: httpx.Response) -> str:
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    # -- public API ---------------------------------------------------------

    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> str:
        """Send a chat completion request and return the assistant message text."""
        # Qwen3 optimization: prepend /no_think to skip chain-of-thought
        # reasoning, saving tokens on structured extraction tasks.
        if "qwen" in self.model.lower() and messages:
            first = messages[0]
            if first.get("role") == "user" and not first["content"].startswith("/no_think"):
                messages = [{"role": first["role"], "content": f"/no_think\n{first['content']}"}] + messages[1:]

        for attempt in range(_MAX_RETRIES):
            try:
                if self._is_anthropic:
                    return self._chat_native_anthropic(messages, temperature, max_tokens)

                # Route to native Gemini if we've already confirmed it's needed
                if self._use_native_gemini:
                    return self._chat_native_gemini(messages, temperature, max_tokens)

                return self._chat_compat(messages, temperature, max_tokens)

            except _GeminiCompatUnavailable as exc:
                # Model not on OpenAI-compat layer — switch to native API.
                compat_status = exc.response.status_code
                log.warning(
                    "Gemini compat endpoint returned %s for model '%s'. "
                    "Switching to native generateContent API. "
                    "(403 = preview/experimental-only on compat; "
                    "404 = model deprecated or removed from compat.)",
                    compat_status, self.model,
                )
                self._use_native_gemini = True
                # Retry immediately with native — don't count as a rate-limit wait
                try:
                    return self._chat_native_gemini(messages, temperature, max_tokens)
                except httpx.HTTPStatusError as native_exc:
                    raise RuntimeError(
                        f"Both Gemini endpoints failed. Compat: {compat_status}. "
                        f"Native: {native_exc.response.status_code} — "
                        f"{native_exc.response.text[:200]}. "
                        f"Model '{self.model}' may be retired; try "
                        f"LLM_MODEL=gemini-2.5-flash in ~/.applypilot/.env."
                    ) from native_exc

            except httpx.HTTPStatusError as exc:
                resp = exc.response
                if resp.status_code in (429, 503) and attempt < _MAX_RETRIES - 1:
                    # Respect Retry-After header if provided (Gemini sends this).
                    retry_after = (
                        resp.headers.get("Retry-After")
                        or resp.headers.get("X-RateLimit-Reset-Requests")
                    )
                    if retry_after:
                        try:
                            wait = float(retry_after)
                        except (ValueError, TypeError):
                            wait = _RATE_LIMIT_BASE_WAIT * (2 ** attempt)
                    else:
                        wait = min(_RATE_LIMIT_BASE_WAIT * (2 ** attempt), 60)

                    log.warning(
                        "LLM rate limited (HTTP %s). Waiting %ds before retry %d/%d. "
                        "Tip: Gemini free tier = 15 RPM. Consider a paid account "
                        "or switching to a local model.",
                        resp.status_code, wait, attempt + 1, _MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue
                raise

            except httpx.TimeoutException:
                if attempt < _MAX_RETRIES - 1:
                    wait = min(_RATE_LIMIT_BASE_WAIT * (2 ** attempt), 60)
                    log.warning(
                        "LLM request timed out, retrying in %ds (attempt %d/%d)",
                        wait, attempt + 1, _MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue
                raise

        raise RuntimeError("LLM request failed after all retries")

    def ask(self, prompt: str, **kwargs) -> str:
        """Convenience: single user prompt -> assistant response."""
        return self.chat([{"role": "user", "content": prompt}], **kwargs)

    def close(self) -> None:
        self._client.close()


class _GeminiCompatUnavailable(Exception):
    """Sentinel: Gemini OpenAI-compat returned 403 or 404. Switch to native API.

    403 = model exists but is preview/experimental-only on native.
    404 = model is deprecated or removed from compat entirely.
    """
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        super().__init__(
            f"Gemini compat {response.status_code}: {response.text[:200]}"
        )


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: LLMClient | None = None
_stage_clients: dict[tuple[str, str], LLMClient] = {}


def get_client(stage: str | None = None) -> LLMClient:
    """Return an LLMClient.

    If `stage` is set and the env var ``<STAGE>_MODEL`` is populated, returns a
    dedicated client for that stage. Otherwise falls back to the default
    auto-detected client (singleton).

    Supported stage names (case-insensitive): ``score``, ``tailor``, ``judge``,
    ``cover``. Each maps to ``SCORE_MODEL`` / ``TAILOR_MODEL`` / ``JUDGE_MODEL`` /
    ``COVER_MODEL`` respectively.
    """
    if stage:
        env_name = f"{stage.upper()}_MODEL"
        model_spec = os.environ.get(env_name, "").strip()
        if model_spec:
            base_url, full_model, key = _resolve_from_model(model_spec)
            cache_key = (base_url, full_model)
            client = _stage_clients.get(cache_key)
            if client is None:
                log.info(
                    "LLM stage=%s provider=%s model=%s",
                    stage, base_url, full_model,
                )
                client = LLMClient(base_url, full_model, key)
                _stage_clients[cache_key] = client
            return client

    global _instance
    if _instance is None:
        base_url, model, api_key = _detect_provider()
        log.info("LLM provider: %s  model: %s", base_url, model)
        _instance = LLMClient(base_url, model, api_key)
    return _instance
