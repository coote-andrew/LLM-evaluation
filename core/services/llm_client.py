"""
Unified LLM API client for multiple providers.

Handles Azure OpenAI, Azure AI Foundry, OpenAI, Anthropic, vLLM, Local (Ollama), and Custom.

URL conventions per provider
-----------------------------
OPENAI          base defaults to https://api.openai.com/v1
                → POST {base}/chat/completions
                auth: Authorization: Bearer <key>

AZURE_OPENAI    classic deployment-level URL, e.g.
                https://<resource>.openai.azure.com/openai/deployments/<deploy>
                → POST {base}/chat/completions
                auth: api-key: <key> or Authorization: Bearer <token>

AZURE_AI_FOUNDRY new Foundry / cognitive-services endpoint, e.g.
                https://<resource>.openai.azure.com  (no /deployments/ in path)
                → POST {base}/openai/v1/chat/completions
                auth: api-key: <key> or Authorization: Bearer <token>

VLLM            vLLM OpenAI-compatible server, e.g. http://host:8000
                → POST {base}/v1/chat/completions
                auth: Authorization: Bearer <key>  (token optional for local)

LOCAL/CUSTOM    any OpenAI-compatible server (Ollama, LM Studio, etc.)
                Infers path the same way as vLLM above.
                auth: Authorization: Bearer <key>
"""

import json
import re
import time
from typing import Any

import httpx

from core.models import AuthType, ModelConfig, Provider


_AZURE_TOKEN_CACHE: dict[tuple[str, str, str], tuple[str, float]] = {}


def _build_openai_compatible_url(provider: str, base: str) -> str:
    """Return the chat/completions URL for an OpenAI-compatible endpoint."""
    base = base.rstrip("/")

    if provider == Provider.AZURE_OPENAI:
        # Classic deployment URL already includes /openai/deployments/<name>
        # Just append /chat/completions if not already present.
        if "completions" in base:
            return base
        return f"{base}/chat/completions"

    if provider == Provider.AZURE_AI_FOUNDRY:
        # New Foundry endpoint: base is the resource root.
        # Correct path is /openai/v1/chat/completions.
        if "completions" in base:
            return base
        if base.endswith("/openai/v1"):
            return f"{base}/chat/completions"
        if base.endswith("/openai"):
            return f"{base}/v1/chat/completions"
        return f"{base}/openai/v1/chat/completions"

    # OPENAI, VLLM, LOCAL, CUSTOM — standard /v1/chat/completions layout.
    if not base:
        return "https://api.openai.com/v1/chat/completions"
    if "completions" in base:
        return base
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def _build_auth_headers(provider: str, api_key: str) -> dict[str, str]:
    """Return authentication headers appropriate for the provider."""
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if not api_key:
        return headers
    if provider in (Provider.AZURE_OPENAI, Provider.AZURE_AI_FOUNDRY):
        # Azure uses api-key header, not Bearer token.
        headers["api-key"] = api_key
    else:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _get_azure_access_token(
    client: httpx.Client,
    model_config: ModelConfig,
    timeout: float,
) -> str:
    """Exchange app-registration credentials for an Azure access token."""
    scope = model_config.azure_token_scope or "https://cognitiveservices.azure.com/.default"
    cache_key = (model_config.azure_tenant_id, model_config.azure_client_id, scope)
    cached = _AZURE_TOKEN_CACHE.get(cache_key)
    now = time.monotonic()
    if cached and cached[1] > now:
        return cached[0]

    token_url = (
        f"https://login.microsoftonline.com/{model_config.azure_tenant_id}"
        "/oauth2/v2.0/token"
    )
    resp = client.post(
        token_url,
        data={
            "grant_type": "client_credentials",
            "client_id": model_config.azure_client_id,
            "client_secret": model_config.azure_client_secret,
            "scope": scope,
        },
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Azure token request failed {resp.status_code}: {resp.text[:500]}")

    data = resp.json()
    access_token = data.get("access_token")
    if not access_token:
        raise RuntimeError("Azure token response did not include an access token.")

    expires_in = int(data.get("expires_in", 3600))
    _AZURE_TOKEN_CACHE[cache_key] = (access_token, now + max(expires_in - 60, 0))
    return access_token


def _call_openai_compatible(
    client: httpx.Client,
    provider: str,
    url: str,
    api_key: str,
    model_name: str,
    prompt: str,
    temperature: float,
    max_tokens: int,
    timeout: float = 120.0,
    is_agent: bool = False,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """OpenAI-compatible API (OpenAI, Azure OpenAI, Azure AI Foundry, vLLM, Local, Custom, Agents)."""
    if not url and provider == Provider.OPENAI:
        url = "https://api.openai.com/v1"

    chat_url = _build_openai_compatible_url(provider, url or "")
    headers = _build_auth_headers(provider, api_key)
    if extra_headers:
        headers.update(extra_headers)

    payload: dict[str, Any] = {
        "model": model_name,
        "messages": [{"role": "user", "content": prompt}],
    }
    if not is_agent:
        # clinical_graphs agent patterns ignore most OpenAI knobs and may 422 on
        # unknown fields. Only send the sampling params for real LLM endpoints.
        payload["temperature"] = temperature
        payload["max_tokens"] = max_tokens
        payload["chat_template_kwargs"] = {"enable_thinking": False}

    start = time.monotonic()
    try:
        resp = client.post(chat_url, json=payload, headers=headers, timeout=timeout)
    except (httpx.HTTPError, OSError) as exc:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return {
            "text": "",
            "input_tokens": 0,
            "output_tokens": 0,
            "latency_ms": elapsed_ms,
            "error": f"Connection error calling {chat_url}: {exc}",
        }
    elapsed_ms = int((time.monotonic() - start) * 1000)

    if resp.status_code != 200:
        return {
            "text": "",
            "input_tokens": 0,
            "output_tokens": 0,
            "latency_ms": elapsed_ms,
            "error": f"API error {resp.status_code}: {resp.text[:500]}",
        }

    data = resp.json()
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message", {})
    text = message.get("content", "")
    usage = data.get("usage", {})
    result: dict[str, Any] = {
        "text": text,
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0),
        "latency_ms": elapsed_ms,
        "error": None,
    }
    if is_agent:
        # Agents service sends the full graph state as JSON in `content`, and
        # duplicates it in the non-standard `message.parsed` field. Surface both
        # so downstream evaluators can inspect structured output directly.
        if isinstance(message.get("parsed"), dict):
            result["agent_state"] = message["parsed"]
        query_id = resp.headers.get("X-Query-Id")
        if query_id:
            result["query_id"] = query_id
    return result


def _auth_for_openai_compatible(
    client: httpx.Client,
    model_config: ModelConfig,
    effective_timeout: float,
) -> tuple[str, dict[str, str] | None]:
    """Return API key plus optional headers for the selected auth mode."""
    if model_config.auth_type != AuthType.AZURE_CLIENT_SECRET:
        return model_config.api_key or "", None

    token = _get_azure_access_token(client, model_config, effective_timeout)
    return "", {"Authorization": f"Bearer {token}"}


def _call_anthropic(
    client: httpx.Client,
    url: str,
    api_key: str,
    model_name: str,
    prompt: str,
    temperature: float,
    max_tokens: int,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Anthropic Messages API."""
    chat_url = url or "https://api.anthropic.com/v1/messages"
    chat_url = chat_url.rstrip("/")
    if not chat_url.endswith("/messages"):
        chat_url = f"{chat_url}/v1/messages"

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }

    payload = {
        "model": model_name,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if temperature > 0:
        payload["temperature"] = temperature

    start = time.monotonic()
    try:
        resp = client.post(chat_url, json=payload, headers=headers, timeout=timeout)
    except (httpx.HTTPError, OSError) as exc:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return {
            "text": "",
            "input_tokens": 0,
            "output_tokens": 0,
            "latency_ms": elapsed_ms,
            "error": f"Connection error calling {chat_url}: {exc}",
        }
    elapsed_ms = int((time.monotonic() - start) * 1000)

    if resp.status_code != 200:
        return {
            "text": "",
            "input_tokens": 0,
            "output_tokens": 0,
            "latency_ms": elapsed_ms,
            "error": f"API error {resp.status_code}: {resp.text[:500]}",
        }

    data = resp.json()
    content = data.get("content", [])
    text = ""
    for block in content:
        if block.get("type") == "text":
            text += block.get("text", "")
    usage = data.get("usage", {})
    return {
        "text": text,
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "latency_ms": elapsed_ms,
        "error": None,
    }


def call_llm(
    model_config: ModelConfig,
    prompt: str,
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout: float | None = None,
    response_format_json: bool = False,
) -> dict[str, Any]:
    """
    Call the LLM with the given prompt. Returns:
    - text: raw response
    - input_tokens, output_tokens, latency_ms
    - error: str or None
    - parsed: dict if response_format_json and parse succeeded, else None
    """
    temp = temperature if temperature is not None else model_config.default_temperature
    max_tok = max_tokens if max_tokens is not None else model_config.default_max_tokens
    effective_timeout = timeout if timeout is not None else model_config.default_timeout
    url = model_config.api_endpoint or ""
    api_key = model_config.api_key or ""
    model_name = model_config.model_name

    is_agent = bool(getattr(model_config, "is_agent", False))

    with httpx.Client() as client:
        if model_config.provider == Provider.ANTHROPIC and not is_agent:
            result = _call_anthropic(
                client, url, api_key, model_name, prompt, temp, max_tok, effective_timeout
            )
        else:
            try:
                api_key, extra_headers = _auth_for_openai_compatible(
                    client, model_config, effective_timeout
                )
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                return {
                    "text": "",
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "latency_ms": 0,
                    "error": str(exc),
                }
            result = _call_openai_compatible(
                client,
                model_config.provider,
                url,
                api_key,
                model_name,
                prompt,
                temp,
                max_tok,
                effective_timeout,
                is_agent=is_agent,
                extra_headers=extra_headers,
            )

    if result.get("error"):
        return result

    if result.get("text") and not is_agent:
        # Agent responses are JSON graph state; don't treat <think>...</think>
        # as model chain-of-thought and strip it.
        result["text"] = _strip_think_tags(result["text"])

    parsed = None
    if is_agent and isinstance(result.get("agent_state"), dict):
        # Agent runs always expose structured output — use the graph state.
        parsed = result["agent_state"]
    elif response_format_json and result.get("text"):
        text = _strip_code_fence(result["text"])
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            result["parse_error"] = "Failed to parse response as JSON"

    result["parsed"] = parsed
    return result


def _strip_think_tags(text: str) -> str:
    """Remove <think>...</think> blocks from model output (thinking models)."""
    if not text or "<think>" not in text:
        return text
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _strip_code_fence(text: str) -> str:
    """Remove markdown code fences that models sometimes wrap JSON responses in."""
    stripped = text.strip()
    if stripped.startswith("```"):
        # Drop the opening fence line (```json, ```JSON, ``` etc.)
        first_newline = stripped.find("\n")
        if first_newline != -1:
            stripped = stripped[first_newline + 1:]
        # Drop the closing fence
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3].rstrip()
    return stripped
