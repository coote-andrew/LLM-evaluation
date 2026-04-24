"""Web UI for the external agents service registry.

These views surface the ``AgentAsset`` / ``AgentAssetVersion`` cache populated
by the ``sync_agent_registry`` command, and layer live fetches on top of it
(source, diff, pull, reload, validate) via :class:`AgentsClient`.

Source and diff text are never stored locally — they are fetched on demand
from the agents service and rendered inline. This keeps the UI lightweight
while preserving the "agents repo is the source of truth" invariant.
"""

from __future__ import annotations

import logging
from io import StringIO
from typing import Any

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.management import CommandError, call_command
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views.decorators.http import require_POST
from django.views.generic import TemplateView, View

from core.models import AgentAsset, AgentAssetKind, AgentAssetVersion
from core.services.agents_client import (
    AgentsClient,
    AgentsServiceError,
    AgentsServiceNotConfigured,
)

_log = logging.getLogger(__name__)


def _build_client() -> AgentsClient | None:
    """Return a configured client, or ``None`` if settings are incomplete.

    Views use this helper so a missing ``AGENTS_SERVICE_URL`` degrades the
    page (banner + cached data) rather than 500ing.
    """
    try:
        return AgentsClient.from_settings()
    except AgentsServiceNotConfigured as exc:
        _log.info("Agents service not configured: %s", exc)
        return None


class AgentRegistryView(LoginRequiredMixin, TemplateView):
    """Landing page: service health, last sync, grouped asset list, actions."""

    template_name = "core/agents/registry.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        ctx = super().get_context_data(**kwargs)

        client = _build_client()
        health: dict[str, Any] | None = None
        health_error: str | None = None
        service_url = client.config.base_url if client else ""

        if client is None:
            health_error = (
                "Agents service not configured. Set AGENTS_SERVICE_URL and "
                "AGENTS_SERVICE_ADMIN_KEY in Django settings (or env vars) "
                "to enable live actions."
            )
        else:
            try:
                health = client.health()
            except AgentsServiceError as exc:
                health_error = str(exc)

        assets = (
            AgentAsset.objects.all()
            .prefetch_related("versions")
            .order_by("kind", "name")
        )

        groups: dict[str, list[AgentAsset]] = {
            kind.value: [] for kind in AgentAssetKind
        }
        for asset in assets:
            groups.setdefault(asset.kind, []).append(asset)

        grouped = [
            {
                "kind": kind.value,
                "label": kind.label,
                "assets": groups.get(kind.value, []),
            }
            for kind in AgentAssetKind
        ]

        latest_sync = (
            AgentAsset.objects.exclude(last_synced_at=None)
            .order_by("-last_synced_at")
            .values_list("last_synced_at", flat=True)
            .first()
        )

        ctx.update(
            {
                "service_url": service_url,
                "health": health,
                "health_error": health_error,
                "grouped_assets": grouped,
                "latest_sync": latest_sync,
                "total_assets": assets.count(),
                "total_versions": AgentAssetVersion.objects.count(),
            }
        )
        return ctx


class AgentAssetDetailView(LoginRequiredMixin, TemplateView):
    """Single asset with its versions + actions per version."""

    template_name = "core/agents/asset_detail.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        ctx = super().get_context_data(**kwargs)
        kind = kwargs["kind"]
        name = kwargs["name"]

        if kind not in {c.value for c in AgentAssetKind}:
            from django.http import Http404
            raise Http404(f"Unknown asset kind: {kind}")

        asset = get_object_or_404(AgentAsset, kind=kind, name=name)
        versions = list(asset.versions.all().order_by("-is_working_copy", "-label"))

        # Labels that can be diffed against (exclude the working copy as a
        # base so the picker defaults are cut-vs-cut; user can always flip).
        cut_labels = [v.label for v in versions if not v.is_working_copy]

        ctx.update(
            {
                "asset": asset,
                "versions": versions,
                "cut_labels": cut_labels,
            }
        )
        return ctx


class AgentVersionSourceView(LoginRequiredMixin, TemplateView):
    """Raw file viewer: live fetch, no local caching."""

    template_name = "core/agents/version_source.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        ctx = super().get_context_data(**kwargs)
        kind = kwargs["kind"]
        name = kwargs["name"]
        label = kwargs["label"]

        asset = get_object_or_404(AgentAsset, kind=kind, name=name)
        version = get_object_or_404(AgentAssetVersion, asset=asset, label=label)

        source: str | None = None
        fetch_error: str | None = None

        client = _build_client()
        if client is None:
            fetch_error = (
                "Agents service not configured — cannot fetch source. Set "
                "AGENTS_SERVICE_URL + AGENTS_SERVICE_ADMIN_KEY."
            )
        else:
            try:
                source = client.source(kind, name, label)
            except AgentsServiceError as exc:
                fetch_error = str(exc)

        ctx.update(
            {
                "asset": asset,
                "version": version,
                "source": source,
                "fetch_error": fetch_error,
            }
        )
        return ctx


class AgentVersionDiffView(LoginRequiredMixin, TemplateView):
    """Diff viewer for two labels of the same asset."""

    template_name = "core/agents/version_diff.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        ctx = super().get_context_data(**kwargs)
        kind = kwargs["kind"]
        name = kwargs["name"]

        asset = get_object_or_404(AgentAsset, kind=kind, name=name)
        versions = list(asset.versions.all().order_by("-is_working_copy", "-label"))

        from_label = self.request.GET.get("from") or ""
        to_label = self.request.GET.get("to") or ""

        diff_text: str | None = None
        fetch_error: str | None = None

        if from_label and to_label and from_label != to_label:
            client = _build_client()
            if client is None:
                fetch_error = (
                    "Agents service not configured — cannot fetch diff. Set "
                    "AGENTS_SERVICE_URL + AGENTS_SERVICE_ADMIN_KEY."
                )
            else:
                try:
                    diff_text = client.diff(
                        kind, name, from_label=from_label, to_label=to_label,
                    )  # type: ignore[assignment]
                except AgentsServiceError as exc:
                    fetch_error = str(exc)

        ctx.update(
            {
                "asset": asset,
                "versions": versions,
                "from_label": from_label,
                "to_label": to_label,
                "diff_text": diff_text,
                "fetch_error": fetch_error,
            }
        )
        return ctx


# -- Actions ------------------------------------------------------------------

_VALID_ACTIONS = {"sync", "pull", "reload", "validate"}


@method_decorator(require_POST, name="dispatch")
class AgentRegistryActionView(LoginRequiredMixin, View):
    """Handle POST actions triggered from the registry landing page.

    Supported actions (via the ``action`` form field):

    * ``sync`` — ``python manage.py sync_agent_registry`` in-process.
    * ``pull`` — ``POST /admin/pull`` on the agents service.
    * ``reload`` — ``POST /admin/reload`` on the agents service.
    * ``validate`` — ``POST /admin/validate`` on the agents service.

    On success or failure the user is redirected back to the registry page
    with a flash message describing what happened.
    """

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        action = (request.POST.get("action") or "").strip().lower()
        if action not in _VALID_ACTIONS:
            messages.error(request, f"Unknown agents action: {action or '(empty)'}")
            return self._back()

        handler = getattr(self, f"_handle_{action}")
        try:
            handler(request)
        except AgentsServiceNotConfigured as exc:
            messages.error(request, f"Agents service not configured: {exc}")
        except AgentsServiceError as exc:
            messages.error(request, f"Agents service error: {exc}")
        except CommandError as exc:
            messages.error(request, f"Sync command failed: {exc}")
        except Exception as exc:  # noqa: BLE001 — UI surface wants a friendly error
            _log.exception("Unexpected error in agents action %s", action)
            messages.error(request, f"Unexpected error: {exc}")
        return self._back()

    def _back(self) -> HttpResponseRedirect:
        return redirect(reverse("core:agent_registry"))

    # -- individual handlers --------------------------------------------------

    def _handle_sync(self, request: HttpRequest) -> None:
        buf = StringIO()
        call_command("sync_agent_registry", stdout=buf, stderr=buf)
        last_line = (buf.getvalue().strip().splitlines() or [""])[-1]
        messages.success(request, last_line or "Sync complete.")

    def _handle_pull(self, request: HttpRequest) -> None:
        client = AgentsClient.from_settings()
        result = client.pull()
        old = str(result.get("old_sha") or "")[:12]
        new = str(result.get("new_sha") or "")[:12]
        changed = len(result.get("changed_files") or [])
        messages.success(
            request,
            f"Pull: {old} → {new} ({changed} file(s) changed).",
        )

    def _handle_reload(self, request: HttpRequest) -> None:
        client = AgentsClient.from_settings()
        result = client.reload()
        patterns = result.get("patterns_loaded")
        if patterns is not None:
            messages.success(request, f"Reload OK — {patterns} pattern(s) loaded.")
        else:
            messages.success(request, "Reload OK.")

    def _handle_validate(self, request: HttpRequest) -> None:
        client = AgentsClient.from_settings()
        result = client.validate()
        ok = int(result.get("ok", 0))
        failed = int(result.get("failed", 0))
        if failed:
            messages.error(
                request,
                f"Validate: {ok} OK, {failed} failed. Check /admin/registry for "
                "the failed versions (ready=False).",
            )
        else:
            messages.success(request, f"Validate: {ok} file(s) OK, no failures.")
