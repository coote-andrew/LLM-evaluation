"""HTTP client for the agents service admin API.

This wraps every call to the agents service's ``/admin/*`` endpoints — the
registry, source, diff, pull, reload, and validate routes described in
``docs/AGENTS_SERVICE_GUIDE.md``.

All agents-admin traffic in Django should go through ``AgentsClient`` so we
have one place to:

- Resolve the base URL from settings.
- Attach the ``X-Admin-Key`` shared secret.
- Apply the configured timeout.
- Normalise errors into ``AgentsServiceError``.
- Offer typed helpers (``registry()``, ``source()``, ``diff()``, ...).

The runtime path (``POST /v1/chat/completions``) is *not* this client's job —
that stays in ``core.services.llm_client`` because it is driven by per-row
``ModelConfig`` rows, not a single cluster-wide service URL.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import httpx
from django.conf import settings

_log = logging.getLogger(__name__)


class AgentsServiceError(RuntimeError):
    """Raised for any non-2xx response or transport failure from the agents admin API."""

    def __init__(self, message: str, *, status_code: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class AgentsServiceNotConfigured(AgentsServiceError):
    """Raised when the caller tries to use the admin API without settings."""


@dataclass(frozen=True)
class AgentsClientConfig:
    base_url: str
    admin_key: str
    timeout: float

    @classmethod
    def from_settings(cls) -> "AgentsClientConfig":
        base = (getattr(settings, "AGENTS_SERVICE_URL", "") or "").rstrip("/")
        key = getattr(settings, "AGENTS_SERVICE_ADMIN_KEY", "") or ""
        timeout = float(getattr(settings, "AGENTS_SERVICE_TIMEOUT", 30.0))
        if not base:
            raise AgentsServiceNotConfigured(
                "AGENTS_SERVICE_URL is not set. Configure the agents-service URL and "
                "admin key in Django settings (or environment variables) before "
                "calling the admin API. See docs/AGENTS_SERVICE_GUIDE.md §9."
            )
        if not key:
            raise AgentsServiceNotConfigured(
                "AGENTS_SERVICE_ADMIN_KEY is not set. The agents admin API requires "
                "a shared secret; see docs/AGENTS_SERVICE_GUIDE.md §4.1."
            )
        return cls(base_url=base, admin_key=key, timeout=timeout)


class AgentsClient:
    """Typed wrapper around the agents service admin API.

    Thread-safe for concurrent calls as long as you share an ``httpx.Client``
    across them (default behaviour).

    Typical use::

        client = AgentsClient.from_settings()
        snapshot = client.registry()
        src = client.source("tool", "snomed_lookup", "1.2")

    Or inject a custom transport for tests::

        transport = httpx.MockTransport(handler)
        client = AgentsClient(
            config=AgentsClientConfig(base_url="http://agents", admin_key="k", timeout=5),
            transport=transport,
        )
    """

    def __init__(
        self,
        *,
        config: AgentsClientConfig | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.config = config or AgentsClientConfig.from_settings()
        self._transport = transport

    @classmethod
    def from_settings(cls) -> "AgentsClient":
        return cls(config=AgentsClientConfig.from_settings())

    # -- public API ---------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """``GET /admin/health`` — liveness + git_sha."""
        return self._get_json("/admin/health")

    def registry(self) -> dict[str, Any]:
        """``GET /admin/registry`` — full metadata snapshot."""
        return self._get_json("/admin/registry")

    def asset(self, kind: str, name: str) -> dict[str, Any]:
        """``GET /admin/assets/{kind}/{name}`` — one asset with its versions."""
        return self._get_json(f"/admin/assets/{kind}/{name}")

    def source(self, kind: str, name: str, label: str) -> str:
        """``GET /admin/assets/{kind}/{name}/versions/{label}/source`` — raw file bytes as text."""
        resp = self._request(
            "GET",
            f"/admin/assets/{kind}/{name}/versions/{label}/source",
        )
        return resp.text

    def diff(
        self,
        kind: str,
        name: str,
        *,
        from_label: str,
        to_label: str,
        context: int = 3,
        as_json: bool = False,
    ) -> str | dict[str, Any]:
        """``GET /admin/assets/{kind}/{name}/diff`` — unified diff or JSON wrapper."""
        params: dict[str, str] = {"from": from_label, "to": to_label}
        if context != 3:
            params["context"] = str(context)
        if as_json:
            params["format"] = "json"
        resp = self._request(
            "GET",
            f"/admin/assets/{kind}/{name}/diff",
            params=params,
        )
        if as_json:
            return resp.json()
        return resp.text

    def pull(self, ref: str | None = None) -> dict[str, Any]:
        """``POST /admin/pull`` — ``git fetch && git merge --ff-only`` on the agents repo."""
        body: dict[str, Any] = {}
        if ref:
            body["ref"] = ref
        return self._post_json("/admin/pull", json=body)

    def reload(self) -> dict[str, Any]:
        """``POST /admin/reload`` — re-import Python modules in the running service."""
        return self._post_json("/admin/reload")

    def validate(self) -> dict[str, Any]:
        """``POST /admin/validate`` — sandbox-import every cut file."""
        return self._post_json("/admin/validate")

    # -- low-level helpers --------------------------------------------------

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.config.base_url,
            timeout=self.config.timeout,
            transport=self._transport,
        )

    def _headers(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        headers = {"X-Admin-Key": self.config.admin_key, "Accept": "application/json"}
        if extra:
            headers.update(extra)
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json: Any = None,
    ) -> httpx.Response:
        try:
            with self._client() as client:
                resp = client.request(
                    method,
                    path,
                    params=dict(params) if params else None,
                    json=json,
                    headers=self._headers(),
                )
        except httpx.HTTPError as exc:
            raise AgentsServiceError(
                f"Transport error talking to agents service ({method} {path}): {exc}"
            ) from exc

        if 200 <= resp.status_code < 300:
            return resp

        body = resp.text[:2000]
        raise AgentsServiceError(
            f"Agents service returned {resp.status_code} for {method} {path}",
            status_code=resp.status_code,
            body=body,
        )

    def _get_json(self, path: str, *, params: Mapping[str, str] | None = None) -> dict[str, Any]:
        resp = self._request("GET", path, params=params)
        try:
            data = resp.json()
        except ValueError as exc:
            raise AgentsServiceError(
                f"Agents service returned non-JSON for GET {path}: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise AgentsServiceError(
                f"Agents service returned non-object JSON for GET {path}: {type(data).__name__}"
            )
        return data

    def _post_json(self, path: str, *, json: Any = None) -> dict[str, Any]:
        resp = self._request("POST", path, json=json)
        try:
            data = resp.json()
        except ValueError as exc:
            raise AgentsServiceError(
                f"Agents service returned non-JSON for POST {path}: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise AgentsServiceError(
                f"Agents service returned non-object JSON for POST {path}: {type(data).__name__}"
            )
        return data


__all__: Iterable[str] = (
    "AgentsClient",
    "AgentsClientConfig",
    "AgentsServiceError",
    "AgentsServiceNotConfigured",
)
