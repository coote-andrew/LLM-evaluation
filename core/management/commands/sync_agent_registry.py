"""Pull the agents service registry snapshot into Django's metadata cache.

Usage::

    python manage.py sync_agent_registry
    python manage.py sync_agent_registry --pull        # ask the agent to git pull first
    python manage.py sync_agent_registry --dry-run     # compute diff, don't write
    python manage.py sync_agent_registry --json        # dump the raw registry snapshot

No Python source is ever stored locally. This command only upserts metadata
rows (kind, name, label, content hash, pinned deps, declared params,
git_sha, ...). See ``docs/AGENTS_SERVICE_GUIDE.md`` for the contract.

Drift handling: if an existing ``AgentAssetVersion`` row's ``content_hash``
no longer matches what the agent service reports, that's a violation of the
"cut files are append-only" invariant. We refuse to silently overwrite and
instead print a warning + exit non-zero so operators notice. Use
``--force`` to override (you almost never should).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterable

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone as dj_timezone

from core.models import AgentAsset, AgentAssetKind, AgentAssetVersion
from core.services.agents_client import (
    AgentsClient,
    AgentsServiceError,
    AgentsServiceNotConfigured,
)

_VALID_KINDS = {c.value for c in AgentAssetKind}


class Command(BaseCommand):
    help = (
        "Sync Django's AgentAsset/AgentAssetVersion cache from the agents "
        "service admin API (GET /admin/registry)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--pull",
            action="store_true",
            help="Call POST /admin/pull first to git-fast-forward the agents repo.",
        )
        parser.add_argument(
            "--reload",
            action="store_true",
            help="Call POST /admin/reload after syncing to re-import modules.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Compute the changes but don't write them.",
        )
        parser.add_argument(
            "--json",
            dest="emit_json",
            action="store_true",
            help="Dump the raw registry snapshot as JSON and exit.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help=(
                "Overwrite existing version rows even if content_hash has drifted. "
                "Intended for one-off remediation; normally you should investigate "
                "the drift instead."
            ),
        )

    def handle(self, *args, **options):
        try:
            client = AgentsClient.from_settings()
        except AgentsServiceNotConfigured as exc:
            raise CommandError(str(exc)) from exc

        if options["pull"]:
            self._emit("Pulling agents repo ...")
            try:
                pull_result = client.pull()
            except AgentsServiceError as exc:
                raise CommandError(f"git pull failed on agents service: {exc}") from exc
            self._emit(
                f"  old_sha={pull_result.get('old_sha', '?')[:12]}  "
                f"new_sha={pull_result.get('new_sha', '?')[:12]}  "
                f"changed={len(pull_result.get('changed_files') or [])}"
            )

        try:
            snapshot = client.registry()
        except AgentsServiceError as exc:
            raise CommandError(f"Failed to fetch registry: {exc}") from exc

        if options["emit_json"]:
            self.stdout.write(json.dumps(snapshot, indent=2, default=str))
            return

        assets = snapshot.get("assets")
        if not isinstance(assets, list):
            raise CommandError(
                "Agents service returned an unexpected /admin/registry payload: "
                "'assets' missing or not a list."
            )

        stats = self._apply_snapshot(
            assets,
            scanned_at=snapshot.get("scanned_at"),
            dry_run=options["dry_run"],
            force=options["force"],
        )

        self._emit(
            f"Synced registry from agents service. "
            f"assets: {stats['assets_seen']} seen, {stats['assets_created']} created, "
            f"{stats['assets_deactivated']} deactivated. "
            f"versions: {stats['versions_seen']} seen, {stats['versions_created']} created, "
            f"{stats['versions_updated']} updated, {stats['versions_drift']} drifted."
            + (" [dry-run, no writes]" if options["dry_run"] else "")
        )

        if stats["versions_drift"] and not options["force"]:
            raise CommandError(
                f"{stats['versions_drift']} version(s) had drifted content_hash. "
                "Cut files are append-only; investigate the agents service or "
                "re-run with --force to accept the new hash (not recommended)."
            )

        if options["reload"] and not options["dry_run"]:
            try:
                client.reload()
                self._emit("Requested /admin/reload on agents service.")
            except AgentsServiceError as exc:
                self._emit(self.style.WARNING(f"Reload failed: {exc}"))

    # ------------------------------------------------------------------ helpers

    def _apply_snapshot(
        self,
        assets: Iterable[dict[str, Any]],
        *,
        scanned_at: str | None,
        dry_run: bool,
        force: bool,
    ) -> dict[str, int]:
        now = dj_timezone.now()
        scanned_dt = _parse_iso(scanned_at) or now

        stats = {
            "assets_seen": 0,
            "assets_created": 0,
            "assets_deactivated": 0,
            "versions_seen": 0,
            "versions_created": 0,
            "versions_updated": 0,
            "versions_drift": 0,
        }

        seen_asset_pks: set = set()

        for raw_asset in assets:
            kind = raw_asset.get("kind")
            name = raw_asset.get("name")
            if kind not in _VALID_KINDS or not isinstance(name, str) or not name:
                self._emit(
                    self.style.WARNING(
                        f"Skipping asset with invalid kind/name: kind={kind!r} name={name!r}"
                    )
                )
                continue
            stats["assets_seen"] += 1

            description = raw_asset.get("description") or ""

            if dry_run:
                existing = AgentAsset.objects.filter(kind=kind, name=name).first()
                if not existing:
                    stats["assets_created"] += 1
                asset_pk = existing.pk if existing else None
            else:
                with transaction.atomic():
                    asset, created = AgentAsset.objects.update_or_create(
                        kind=kind,
                        name=name,
                        defaults={
                            "description": description,
                            "is_active": True,
                            "last_synced_at": scanned_dt,
                        },
                    )
                if created:
                    stats["assets_created"] += 1
                asset_pk = asset.pk
                seen_asset_pks.add(asset_pk)

            for raw_version in raw_asset.get("versions") or []:
                v_stats = self._apply_version(
                    asset_pk=asset_pk,
                    kind=kind,
                    name=name,
                    raw_version=raw_version,
                    scanned_dt=scanned_dt,
                    dry_run=dry_run,
                    force=force,
                )
                for k, v in v_stats.items():
                    stats[k] += v

        # Mark assets that weren't in this snapshot as inactive. Only do this
        # on a full sync (i.e. we saw at least one asset); a zero-asset
        # response is more likely a bug than a legitimate empty registry, and
        # we don't want to deactivate everything in that case.
        if not dry_run and stats["assets_seen"] > 0:
            missing = AgentAsset.objects.filter(is_active=True).exclude(pk__in=seen_asset_pks)
            stats["assets_deactivated"] = missing.count()
            if stats["assets_deactivated"]:
                missing.update(is_active=False)

        return stats

    def _apply_version(
        self,
        *,
        asset_pk: Any,
        kind: str,
        name: str,
        raw_version: dict[str, Any],
        scanned_dt: datetime,
        dry_run: bool,
        force: bool,
    ) -> dict[str, int]:
        stats = {
            "versions_seen": 0,
            "versions_created": 0,
            "versions_updated": 0,
            "versions_drift": 0,
        }
        label = raw_version.get("label")
        content_hash = raw_version.get("content_hash")
        if not isinstance(label, str) or not label or not isinstance(content_hash, str):
            self._emit(
                self.style.WARNING(
                    f"  Skipping {kind}/{name} version with missing label/content_hash: "
                    f"{raw_version!r}"
                )
            )
            return stats

        stats["versions_seen"] = 1
        defaults = {
            "file_path": raw_version.get("file_path") or "",
            "content_hash": content_hash,
            "git_sha": raw_version.get("git_sha") or "",
            "declared_params": raw_version.get("declared_params") or {},
            "pinned_deps": raw_version.get("pinned_deps") or {},
            "is_working_copy": bool(raw_version.get("is_working_copy", label == "@latest")),
            "is_deprecated": bool(raw_version.get("is_deprecated", False)),
            "ready": bool(raw_version.get("ready", True)),
            "import_error": raw_version.get("error") or "",
            "created_at_agent": _parse_iso(raw_version.get("created_at")),
            "last_synced_at": scanned_dt,
        }

        if asset_pk is None:
            # dry_run and the asset itself is new — count this version as new too.
            stats["versions_created"] = 1
            return stats

        existing = AgentAssetVersion.objects.filter(asset_id=asset_pk, label=label).first()
        if existing is None:
            stats["versions_created"] = 1
            if not dry_run:
                AgentAssetVersion.objects.create(asset_id=asset_pk, label=label, **defaults)
            return stats

        # Working copies legitimately change hash; cut versions must not.
        if (
            not defaults["is_working_copy"]
            and existing.content_hash
            and existing.content_hash != content_hash
        ):
            stats["versions_drift"] = 1
            self._emit(
                self.style.WARNING(
                    f"  DRIFT: {kind}/{name} @ {label} — stored "
                    f"{existing.content_hash[:16]} != remote {content_hash[:16]}"
                )
            )
            if not force:
                return stats

        # Detect whether anything actually changed; avoid spurious UPDATEs.
        changed = False
        for attr, val in defaults.items():
            if getattr(existing, attr) != val:
                changed = True
                break

        if changed:
            stats["versions_updated"] = 1
            if not dry_run:
                for attr, val in defaults.items():
                    setattr(existing, attr, val)
                existing.save(update_fields=list(defaults.keys()))
        else:
            # Bump last_synced_at even if nothing else changed so UIs can
            # tell users "last seen at ...".
            if not dry_run:
                existing.last_synced_at = scanned_dt
                existing.save(update_fields=["last_synced_at"])

        return stats

    def _emit(self, line: str) -> None:
        self.stdout.write(line)


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        # ``fromisoformat`` in 3.11+ accepts ``...Z``; for older 3.x, swap it.
        cleaned = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
