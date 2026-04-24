"""
Signal handlers for the core app.

Currently: auto-regenerate ``llm_providers.yaml`` whenever a ``ModelConfig``
row is saved or deleted, if opted in via settings. The generated file is a
deploy artefact consumed by the *external* agents service (see
``docs/AGENTS_SERVICE_GUIDE.md``); Django doesn't read it back.

Enable in Django settings:

    AUTO_GENERATE_LLM_PROVIDERS_YAML = True
    # Optional — defaults to <BASE_DIR>/dist/llm_providers.yaml
    # LLM_PROVIDERS_YAML_PATH = "/srv/agents-deploy/llm_providers.yaml"

The signal runs best-effort — any failure is logged but never re-raised, so
a missing output directory or disk error never blocks a save.
"""

from __future__ import annotations

import logging
from pathlib import Path

from django.conf import settings
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from core.models import ModelConfig

_log = logging.getLogger(__name__)

# Default sits in a gitignored ``dist/`` folder so the file can be picked up
# by CI/deploy tooling and copied to wherever the external agents service
# expects it. We deliberately do *not* default to an ``agents/`` path: that
# subfolder is being extracted into its own repository and should not exist
# in this checkout long-term.
DEFAULT_RELATIVE_OUTPUT_PATH = Path("dist") / "llm_providers.yaml"


def _regenerate_yaml() -> None:
    # Imported lazily so app startup doesn't pay for PyYAML until used.
    from core.services.llm_providers_yaml import build_providers_document, dump_yaml

    configured = getattr(settings, "LLM_PROVIDERS_YAML_PATH", None)
    output_path = (
        Path(configured)
        if configured
        else Path(settings.BASE_DIR) / DEFAULT_RELATIVE_OUTPUT_PATH
    )
    document = build_providers_document()
    rendered = dump_yaml(document)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered, encoding="utf-8")


@receiver(post_save, sender=ModelConfig)
@receiver(post_delete, sender=ModelConfig)
def sync_llm_providers_yaml(sender, instance: ModelConfig, **kwargs):
    if not getattr(settings, "AUTO_GENERATE_LLM_PROVIDERS_YAML", False):
        return
    try:
        _regenerate_yaml()
    except Exception:
        _log.exception(
            "Failed to regenerate llm_providers.yaml after ModelConfig change"
        )
