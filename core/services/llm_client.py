"""
Unified LLM API client for multiple providers.

Handles Azure OpenAI, OpenAI, Anthropic, Local (Ollama), and Custom.
"""

import json
import time
from typing import Any

import httpx

from core.models import ModelConfig, Provider


def _call_openai_compatible(
    client: httpx.Client,
    url: str,
    api_key: str,
    model_name: str,
    prompt: str,
    temperature: float,
    max_tokens: int,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """OpenAI-compatible API (OpenAI, Azure, Local, Custom)."""
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    headers["Content-Type"] = "application/json"

    # Use URL as base - for Azure, deployment may be in path
    if not url:
        url = "https://api.openai.com/v1"
    base = url.rstrip("/")
    if "/openai/" in base or "/v1" in base:
        chat_url = f"{base}/chat/completions" if "completions" not in base else base
    else:
        chat_url = f"{base}/v1/chat/completions"

    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    start = time.monotonic()
    resp = client.post(chat_url, json=payload, headers=headers, timeout=timeout)
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
    return {
        "text": text,
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "latency_ms": elapsed_ms,
        "error": None,
    }


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
    resp = client.post(chat_url, json=payload, headers=headers, timeout=timeout)
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
    timeout: float = 120.0,
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
    url = model_config.api_endpoint or ""
    api_key = model_config.api_key or ""
    model_name = model_config.model_name

    with httpx.Client() as client:
        if model_config.provider == Provider.ANTHROPIC:
            result = _call_anthropic(
                client, url, api_key, model_name, prompt, temp, max_tok, timeout
            )
        else:
            result = _call_openai_compatible(
                client, url, api_key, model_name, prompt, temp, max_tok, timeout
            )

    if result.get("error"):
        return result

    parsed = None
    if response_format_json and result.get("text"):
        try:
            parsed = json.loads(result["text"])
        except json.JSONDecodeError:
            result["parse_error"] = "Failed to parse response as JSON"

    result["parsed"] = parsed
    return result
