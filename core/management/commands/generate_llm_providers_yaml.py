"""
Regenerate ``llm_providers.yaml`` from ``ModelConfig`` rows.

The generated file is a deploy artefact consumed by the *external* agents
service (see ``docs/AGENTS_SERVICE_GUIDE.md``). Django does not read it back.

Usage::

    # Write to the default location (<BASE_DIR>/dist/llm_providers.yaml):
    python manage.py generate_llm_providers_yaml

    # Explicit destination — typically in your CI/deploy pipeline:
    python manage.py generate_llm_providers_yaml --output /srv/agents-deploy/llm_providers.yaml

    # Print to stdout (no file writes):
    python manage.py generate_llm_providers_yaml --print

    # Validate only — no writes, non-zero exit if invalid:
    python manage.py generate_llm_providers_yaml --dry-run
"""

from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from core.services.llm_providers_yaml import build_providers_document, dump_yaml

# Default sits under ``dist/`` (gitignored) so CI/deploy tooling can pick it
# up without polluting the working tree. Set ``LLM_PROVIDERS_YAML_PATH`` in
# Django settings (or pass ``--output``) to send it elsewhere.
DEFAULT_RELATIVE_PATH = Path("dist") / "llm_providers.yaml"


class Command(BaseCommand):
    help = (
        "Regenerate llm_providers.yaml from ModelConfig rows. "
        "The output is consumed by the external agents service."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--output",
            type=Path,
            default=None,
            help=(
                "Where to write the YAML. Defaults to the LLM_PROVIDERS_YAML_PATH "
                "setting if set, otherwise <BASE_DIR>/dist/llm_providers.yaml."
            ),
        )
        parser.add_argument(
            "--print",
            dest="print_only",
            action="store_true",
            help="Print the YAML to stdout instead of writing to disk.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Compute the YAML but don't write anywhere (exit 0 if valid).",
        )

    def handle(self, *args, **options):
        document = build_providers_document()
        rendered = dump_yaml(document)

        if options["dry_run"]:
            n = len(document.get("providers", []))
            self.stdout.write(
                self.style.SUCCESS(f"Dry run OK ({n} providers, {len(rendered)} bytes).")
            )
            return

        if options.get("print_only"):
            self.stdout.write(rendered)
            return

        configured = getattr(settings, "LLM_PROVIDERS_YAML_PATH", None)
        if options["output"]:
            output_path = options["output"]
        elif configured:
            output_path = Path(configured)
        else:
            output_path = Path(settings.BASE_DIR) / DEFAULT_RELATIVE_PATH
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8")
        self.stdout.write(
            self.style.SUCCESS(
                f"Wrote {len(document.get('providers', []))} providers to {output_path}"
            )
        )
