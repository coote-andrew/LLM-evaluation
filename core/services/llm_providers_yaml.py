"""
Build an ``llm_providers.yaml`` deploy artefact from ``ModelConfig`` rows.

Per Upgrade_proposal §6.4, Django's ``ModelConfig`` is the source of truth for
LLM endpoints. ``llm_providers.yaml`` is a *derived* artefact consumed by the
external agents service (see ``docs/AGENTS_SERVICE_GUIDE.md``): every active,
non-agent ``ModelConfig`` becomes a model under one provider entry.

Only non-agent configs are written — agent configs are *consumers* of other
model configs, not providers themselves.

Secrets:
    API keys are never inlined. Each provider emits ``api_key_env`` /
    ``base_url_env`` names derived from the config UUID (stable across edits).
    The deployment pipeline is responsible for exporting those env vars in
    the agents service container.

Where the file ends up:
    Django writes the YAML to ``<BASE_DIR>/dist/llm_providers.yaml`` by default
    (or to ``LLM_PROVIDERS_YAML_PATH`` / ``--output`` if configured). CI/deploy
    tooling ships it into the agents container. Django never reads it back.
"""

from __future__ import annotations

from typing import Any, Iterable

from core.models import ModelConfig, Provider

# Map Django Provider choices → agents YAML provider `type`.
_PROVIDER_TYPE_MAP: dict[str, str] = {
    Provider.ANTHROPIC: "anthropic",
    Provider.OPENAI: "openai",
    Provider.AZURE_OPENAI: "openai_compatible",
    Provider.AZURE_AI_FOUNDRY: "openai_compatible",
    Provider.VLLM: "openai_compatible",
    Provider.LOCAL: "openai_compatible",
    Provider.CUSTOM: "openai_compatible",
}

# Providers whose endpoint is implied by their type (e.g. api.openai.com) and
# therefore don't need a base_url entry when ``api_endpoint`` is blank.
_IMPLIED_BASE_URL = {Provider.ANTHROPIC, Provider.OPENAI}


def _env_slug(mc: ModelConfig) -> str:
    """Stable uppercase slug for env-var generation."""
    # UUID hex is deterministic but unfriendly; we take the first 8 chars after
    # the config name to keep the env-var readable in ops dashboards.
    base = "".join(c if c.isalnum() else "_" for c in mc.name).strip("_").upper()
    return f"{base}_{str(mc.id).replace('-', '')[:8].upper()}"


def _provider_id_for(mc: ModelConfig) -> str:
    """Group key — configs sharing endpoint+provider become one YAML provider."""
    return f"mc-{str(mc.id).replace('-', '')[:12]}"


def _provider_entry(mc: ModelConfig) -> dict[str, Any]:
    """Build the base provider block (no models) for one ModelConfig."""
    provider_type = _PROVIDER_TYPE_MAP.get(mc.provider, "openai_compatible")
    slug = _env_slug(mc)

    entry: dict[str, Any] = {
        "id": _provider_id_for(mc),
        "type": provider_type,
        "api_key_env": f"LLM_{slug}_API_KEY",
    }

    needs_base_url = mc.provider not in _IMPLIED_BASE_URL or bool(mc.api_endpoint)
    if needs_base_url:
        entry["base_url_env"] = f"LLM_{slug}_BASE_URL"
        if mc.api_endpoint:
            entry["base_url_default"] = mc.api_endpoint

    return entry


def build_providers_document(configs: Iterable[ModelConfig] | None = None) -> dict[str, Any]:
    """
    Return the full YAML document as a plain dict (ready for ``yaml.safe_dump``).

    ``configs`` is an iterable of ModelConfig rows. If None, defaults to all
    active non-agent ModelConfigs in the database.
    """
    if configs is None:
        configs = ModelConfig.objects.filter(is_active=True, is_agent=False).order_by("name")

    providers: list[dict[str, Any]] = []
    for mc in configs:
        entry = _provider_entry(mc)
        entry["models"] = [
            {
                "name": mc.model_name,
                # ModelConfig.name (slugified) doubles as the human-readable alias.
                "alias": _alias_for(mc),
            }
        ]
        providers.append(entry)

    return {"providers": providers}


def _alias_for(mc: ModelConfig) -> str:
    """Slugify ModelConfig.name into a YAML-safe alias."""
    slug = "".join(c if c.isalnum() else "-" for c in mc.name).strip("-").lower()
    return slug or f"model-{str(mc.id)[:8]}"


# File-level header — written on every regeneration so the YAML is clearly
# flagged as derived and tells readers how to refresh it.
YAML_HEADER = (
    "# AUTO-GENERATED from core.models.ModelConfig in the llm-evaluation repo.\n"
    "# Do NOT edit by hand.\n"
    "# To regenerate:  python manage.py generate_llm_providers_yaml\n"
    "# To change entries: edit the corresponding ModelConfig in Django, then\n"
    "# regenerate and ship this file to the agents service (restart or POST\n"
    "# /admin/reload on the running service to pick it up).\n"
    "#\n"
    "# API keys are never inlined. Set the referenced *_API_KEY env vars in\n"
    "# the deployment before starting the agents service.\n"
)


def dump_yaml(document: dict[str, Any]) -> str:
    """Serialise ``document`` to a YAML string with the standard header."""
    import yaml  # local import; PyYAML is already in requirements.txt

    body = yaml.safe_dump(
        document,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )
    return YAML_HEADER + "\n" + body
