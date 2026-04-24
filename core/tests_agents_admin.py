"""Tests for the agents-service admin API integration.

Covers:
    - core.services.agents_client.AgentsClient
    - core.management.commands.sync_agent_registry
    - core.models.AgentAsset / AgentAssetVersion

All tests use ``httpx.MockTransport`` — we never start a real server and we
never contact the production agents service URL.
"""

from __future__ import annotations

import json

import httpx
from django.core.management import CommandError, call_command
from django.test import TestCase as DjangoTestCase
from django.test.utils import override_settings

from core.models import AgentAsset, AgentAssetKind, AgentAssetVersion
from core.services.agents_client import (
    AgentsClient,
    AgentsClientConfig,
    AgentsServiceError,
    AgentsServiceNotConfigured,
)


# ----------------------------------------------------------------------- helpers


def _make_client(handler) -> AgentsClient:
    transport = httpx.MockTransport(handler)
    return AgentsClient(
        config=AgentsClientConfig(
            base_url="http://agents.test",
            admin_key="secret",
            timeout=2.0,
        ),
        transport=transport,
    )


def _snapshot(git_sha: str = "abc123") -> dict:
    """Minimal valid /admin/registry payload covering every supported kind."""
    return {
        "git_sha": git_sha,
        "scanned_at": "2026-04-23T10:00:00Z",
        "assets": [
            {
                "kind": "tool",
                "name": "snomed_lookup",
                "description": "SNOMED candidate lookup",
                "versions": [
                    {
                        "label": "1.0",
                        "file_path": "clinical_graphs/tools/_cut/snomed_lookup__v1_0.py",
                        "content_hash": "sha256:aaaa",
                        "git_sha": git_sha,
                        "declared_params": {"limit": 10},
                        "pinned_deps": {},
                        "created_at": "2026-04-20T09:00:00Z",
                        "ready": True,
                    },
                    {
                        "label": "1.1",
                        "file_path": "clinical_graphs/tools/_cut/snomed_lookup__v1_1.py",
                        "content_hash": "sha256:bbbb",
                        "git_sha": git_sha,
                        "declared_params": {"limit": 5},
                        "pinned_deps": {},
                        "created_at": "2026-04-22T09:00:00Z",
                        "ready": True,
                    },
                    {
                        "label": "@latest",
                        "file_path": "clinical_graphs/tools/snomed_lookup.py",
                        "content_hash": "sha256:cccc",
                        "git_sha": git_sha,
                        "declared_params": {"limit": 5},
                        "pinned_deps": {},
                        "is_working_copy": True,
                        "ready": True,
                    },
                ],
            },
            {
                "kind": "node",
                "name": "summariser",
                "description": "Summarises a clinical note",
                "versions": [
                    {
                        "label": "2.0",
                        "file_path": "clinical_graphs/nodes/_cut/summariser__v2_0.py",
                        "content_hash": "sha256:dddd",
                        "git_sha": git_sha,
                        "declared_params": {},
                        "pinned_deps": {"tool.snomed_lookup": "1.1"},
                        "created_at": "2026-04-22T09:01:00Z",
                        "ready": True,
                    },
                ],
            },
        ],
    }


# ----------------------------------------------------------------- AgentsClient


class AgentsClientConfigTests(DjangoTestCase):
    def test_requires_url(self):
        with override_settings(AGENTS_SERVICE_URL="", AGENTS_SERVICE_ADMIN_KEY="k"):
            with self.assertRaises(AgentsServiceNotConfigured):
                AgentsClientConfig.from_settings()

    def test_requires_admin_key(self):
        with override_settings(
            AGENTS_SERVICE_URL="http://agents.test", AGENTS_SERVICE_ADMIN_KEY=""
        ):
            with self.assertRaises(AgentsServiceNotConfigured):
                AgentsClientConfig.from_settings()

    def test_ok(self):
        with override_settings(
            AGENTS_SERVICE_URL="http://agents.test/",
            AGENTS_SERVICE_ADMIN_KEY="k",
            AGENTS_SERVICE_TIMEOUT=12.5,
        ):
            cfg = AgentsClientConfig.from_settings()
            self.assertEqual(cfg.base_url, "http://agents.test")  # trailing slash stripped
            self.assertEqual(cfg.admin_key, "k")
            self.assertEqual(cfg.timeout, 12.5)


class AgentsClientHttpTests(DjangoTestCase):
    def test_registry_success_sends_admin_key(self):
        seen_headers: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_headers.update(request.headers)
            return httpx.Response(200, json=_snapshot())

        client = _make_client(handler)
        data = client.registry()
        self.assertEqual(seen_headers.get("x-admin-key"), "secret")
        self.assertEqual(len(data["assets"]), 2)

    def test_non_2xx_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"detail": "nope"})

        client = _make_client(handler)
        with self.assertRaises(AgentsServiceError) as ctx:
            client.registry()
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertIn("nope", ctx.exception.body)

    def test_source_returns_text(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/admin/assets/tool/snomed_lookup/versions/1.1/source")
            return httpx.Response(200, text="PARAMS = {'limit': 5}\n")

        client = _make_client(handler)
        body = client.source("tool", "snomed_lookup", "1.1")
        self.assertIn("PARAMS", body)

    def test_diff_query_params(self):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, text="--- a\n+++ b\n")

        client = _make_client(handler)
        client.diff("tool", "snomed_lookup", from_label="1.0", to_label="1.1")
        self.assertIn("from=1.0", captured["url"])
        self.assertIn("to=1.1", captured["url"])

    def test_pull_posts_ref(self):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["body"] = json.loads(request.content.decode() or "{}")
            return httpx.Response(200, json={"ok": True, "old_sha": "a", "new_sha": "a"})

        client = _make_client(handler)
        client.pull(ref="main")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["body"], {"ref": "main"})

    def test_transport_error_wraps(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        client = _make_client(handler)
        with self.assertRaises(AgentsServiceError):
            client.registry()


# ----------------------------------------------------------- sync_agent_registry


def _install_registry_handler(testcase: DjangoTestCase, snapshot: dict | None = None):
    """Monkey-patch AgentsClient.from_settings to hit an httpx.MockTransport."""
    payload = snapshot if snapshot is not None else _snapshot()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/admin/registry":
            return httpx.Response(200, json=payload)
        if path == "/admin/pull":
            return httpx.Response(
                200,
                json={"ok": True, "old_sha": "a" * 40, "new_sha": "b" * 40, "changed_files": []},
            )
        if path == "/admin/reload":
            return httpx.Response(200, json={"ok": True, "reloaded": 1})
        return httpx.Response(404, json={"detail": f"unknown path {path}"})

    from core.management.commands import sync_agent_registry as mod

    real_from_settings = mod.AgentsClient.from_settings

    def fake_from_settings():  # type: ignore[no-untyped-def]
        return AgentsClient(
            config=AgentsClientConfig(
                base_url="http://agents.test",
                admin_key="secret",
                timeout=2.0,
            ),
            transport=httpx.MockTransport(handler),
        )

    mod.AgentsClient.from_settings = classmethod(lambda cls: fake_from_settings())  # type: ignore[assignment]
    testcase.addCleanup(lambda: setattr(mod.AgentsClient, "from_settings", real_from_settings))


@override_settings(
    AGENTS_SERVICE_URL="http://agents.test",
    AGENTS_SERVICE_ADMIN_KEY="secret",
    AGENTS_SERVICE_TIMEOUT=2.0,
)
class SyncAgentRegistryTests(DjangoTestCase):
    def test_first_sync_populates_tables(self):
        _install_registry_handler(self)
        call_command("sync_agent_registry")

        tool = AgentAsset.objects.get(kind=AgentAssetKind.TOOL, name="snomed_lookup")
        self.assertEqual(tool.description, "SNOMED candidate lookup")
        self.assertTrue(tool.is_active)
        self.assertEqual(tool.versions.count(), 3)

        v1_1 = tool.versions.get(label="1.1")
        self.assertEqual(v1_1.content_hash, "sha256:bbbb")
        self.assertEqual(v1_1.declared_params, {"limit": 5})
        self.assertFalse(v1_1.is_working_copy)
        self.assertTrue(v1_1.ready)

        latest = tool.versions.get(label="@latest")
        self.assertTrue(latest.is_working_copy)

        node = AgentAsset.objects.get(kind=AgentAssetKind.NODE, name="summariser")
        v2_0 = node.versions.get(label="2.0")
        self.assertEqual(v2_0.pinned_deps, {"tool.snomed_lookup": "1.1"})

    def test_idempotent_second_sync(self):
        _install_registry_handler(self)
        call_command("sync_agent_registry")
        call_command("sync_agent_registry")

        self.assertEqual(AgentAsset.objects.count(), 2)
        self.assertEqual(AgentAssetVersion.objects.count(), 4)

    def test_deactivates_missing_assets(self):
        _install_registry_handler(self)
        call_command("sync_agent_registry")

        # Second sync omits the node. It should flip to inactive, NOT be deleted.
        snap = _snapshot()
        snap["assets"] = [snap["assets"][0]]  # keep tool only
        _install_registry_handler(self, snapshot=snap)

        call_command("sync_agent_registry")

        node = AgentAsset.objects.get(kind=AgentAssetKind.NODE, name="summariser")
        self.assertFalse(node.is_active)
        tool = AgentAsset.objects.get(kind=AgentAssetKind.TOOL, name="snomed_lookup")
        self.assertTrue(tool.is_active)

    def test_updates_working_copy_hash(self):
        _install_registry_handler(self)
        call_command("sync_agent_registry")

        snap = _snapshot()
        # Change the working-copy hash; cut versions unchanged.
        for v in snap["assets"][0]["versions"]:
            if v["label"] == "@latest":
                v["content_hash"] = "sha256:eeee"
        _install_registry_handler(self, snapshot=snap)

        call_command("sync_agent_registry")

        latest = AgentAssetVersion.objects.get(asset__name="snomed_lookup", label="@latest")
        self.assertEqual(latest.content_hash, "sha256:eeee")

    def test_drift_on_cut_version_errors(self):
        _install_registry_handler(self)
        call_command("sync_agent_registry")

        # Flip the hash of a cut version (simulating illegal in-place edit).
        snap = _snapshot()
        for v in snap["assets"][0]["versions"]:
            if v["label"] == "1.1":
                v["content_hash"] = "sha256:ffff"
        _install_registry_handler(self, snapshot=snap)

        with self.assertRaises(CommandError) as ctx:
            call_command("sync_agent_registry")
        self.assertIn("drifted", str(ctx.exception))

        # The drifted version should NOT have been overwritten.
        v = AgentAssetVersion.objects.get(asset__name="snomed_lookup", label="1.1")
        self.assertEqual(v.content_hash, "sha256:bbbb")

    def test_drift_with_force_overwrites(self):
        _install_registry_handler(self)
        call_command("sync_agent_registry")

        snap = _snapshot()
        for v in snap["assets"][0]["versions"]:
            if v["label"] == "1.1":
                v["content_hash"] = "sha256:ffff"
        _install_registry_handler(self, snapshot=snap)

        call_command("sync_agent_registry", "--force")

        v = AgentAssetVersion.objects.get(asset__name="snomed_lookup", label="1.1")
        self.assertEqual(v.content_hash, "sha256:ffff")

    def test_dry_run_writes_nothing(self):
        _install_registry_handler(self)
        call_command("sync_agent_registry", "--dry-run")
        self.assertEqual(AgentAsset.objects.count(), 0)
        self.assertEqual(AgentAssetVersion.objects.count(), 0)

    def test_pull_flag_is_honoured(self):
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if request.url.path == "/admin/pull":
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "old_sha": "a" * 40,
                        "new_sha": "b" * 40,
                        "changed_files": ["x.py"],
                    },
                )
            if request.url.path == "/admin/registry":
                return httpx.Response(200, json=_snapshot())
            return httpx.Response(404)

        from core.management.commands import sync_agent_registry as mod

        real = mod.AgentsClient.from_settings
        mod.AgentsClient.from_settings = classmethod(  # type: ignore[assignment]
            lambda cls: AgentsClient(
                config=AgentsClientConfig(
                    base_url="http://agents.test", admin_key="secret", timeout=2.0
                ),
                transport=httpx.MockTransport(handler),
            )
        )
        self.addCleanup(lambda: setattr(mod.AgentsClient, "from_settings", real))

        call_command("sync_agent_registry", "--pull")

        self.assertIn("/admin/pull", calls)
        self.assertIn("/admin/registry", calls)
        self.assertLess(calls.index("/admin/pull"), calls.index("/admin/registry"))

    def test_not_configured_raises(self):
        with override_settings(AGENTS_SERVICE_URL="", AGENTS_SERVICE_ADMIN_KEY=""):
            with self.assertRaises(CommandError):
                call_command("sync_agent_registry")


class AgentAssetModelTests(DjangoTestCase):
    def test_unique_per_kind_name(self):
        AgentAsset.objects.create(kind=AgentAssetKind.TOOL, name="x")
        from django.db import IntegrityError

        with self.assertRaises(IntegrityError):
            AgentAsset.objects.create(kind=AgentAssetKind.TOOL, name="x")

    def test_same_name_different_kind_ok(self):
        AgentAsset.objects.create(kind=AgentAssetKind.TOOL, name="x")
        AgentAsset.objects.create(kind=AgentAssetKind.NODE, name="x")  # no error

    def test_unique_label_per_asset(self):
        asset = AgentAsset.objects.create(kind=AgentAssetKind.TOOL, name="x")
        AgentAssetVersion.objects.create(
            asset=asset, label="1.0", content_hash="sha256:a"
        )
        from django.db import IntegrityError

        with self.assertRaises(IntegrityError):
            AgentAssetVersion.objects.create(
                asset=asset, label="1.0", content_hash="sha256:a"
            )
