"""Tests for Entra OIDC claim gating and stable local account linking."""

import os
import tempfile
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.core.exceptions import SuspiciousOperation
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from core.authentication import EntraOIDCAuthenticationBackend
from core.models import EntraIdentity

User = get_user_model()

OIDC_SETTINGS = {
    "ENTRA_OIDC_ENABLED": True,
    "ENTRA_TENANT_ID": "11111111-1111-1111-1111-111111111111",
    "ENTRA_CLIENT_ID": "22222222-2222-2222-2222-222222222222",
    "ENTRA_CLIENT_SECRET": "test-client-secret",
    "ENTRA_APPROVED_GROUP_ID": "33333333-3333-3333-3333-333333333333",
    "ENTRA_REQUIRE_GROUP_CLAIM": True,
    "ENTRA_OIDC_ISSUER": "https://login.microsoftonline.com/11111111-1111-1111-1111-111111111111/v2.0",
    "OIDC_RP_CLIENT_ID": "22222222-2222-2222-2222-222222222222",
    "OIDC_RP_CLIENT_SECRET": "test-client-secret",
    "OIDC_OP_AUTHORIZATION_ENDPOINT": "https://example.invalid/authorize",
    "OIDC_OP_TOKEN_ENDPOINT": "https://example.invalid/token",
    "OIDC_OP_USER_ENDPOINT": "https://example.invalid/userinfo",
    "OIDC_OP_JWKS_ENDPOINT": "https://example.invalid/keys",
    "OIDC_RP_SIGN_ALGO": "RS256",
    "OIDC_RP_SCOPES": "openid profile email",
}


@override_settings(**OIDC_SETTINGS)
class EntraOIDCAuthenticationBackendTests(TestCase):
    def setUp(self):
        self.backend = EntraOIDCAuthenticationBackend()
        self.claims = {
            "iss": OIDC_SETTINGS["ENTRA_OIDC_ISSUER"],
            "tid": OIDC_SETTINGS["ENTRA_TENANT_ID"],
            "oid": "44444444-4444-4444-4444-444444444444",
            "groups": [OIDC_SETTINGS["ENTRA_APPROVED_GROUP_ID"]],
            "preferred_username": "person@example.org",
        }

    def test_approved_claims_create_an_active_unprivileged_local_user(self):
        self.assertTrue(self.backend.verify_claims(self.claims))

        user = self.backend.create_user(self.claims)

        self.assertTrue(user.is_active)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertFalse(user.has_usable_password())
        self.assertEqual(user.email, "person@example.org")
        self.assertEqual(user.entra_identity.object_id, self.claims["oid"])

    def test_missing_or_unapproved_group_is_rejected(self):
        missing_groups = self.claims.copy()
        missing_groups.pop("groups")
        wrong_group = self.claims | {"groups": ["55555555-5555-5555-5555-555555555555"]}

        self.assertFalse(self.backend.verify_claims(missing_groups))
        self.assertFalse(self.backend.verify_claims(wrong_group))

    def test_enterprise_application_assignment_can_be_the_initial_gate(self):
        claims_without_groups = self.claims.copy()
        claims_without_groups.pop("groups")

        with self.settings(ENTRA_REQUIRE_GROUP_CLAIM=False):
            self.assertTrue(self.backend.verify_claims(claims_without_groups))

    @patch("mozilla_django_oidc.auth.OIDCAuthenticationBackend.verify_token")
    def test_token_requires_configured_issuer_tenant_and_audience(self, verify_token):
        verify_token.return_value = self.claims | {
            "aud": OIDC_SETTINGS["ENTRA_CLIENT_ID"],
        }
        self.assertEqual(self.backend.verify_token("unused-token"), verify_token.return_value)

        verify_token.return_value = self.claims | {
            "aud": OIDC_SETTINGS["ENTRA_CLIENT_ID"],
            "tid": "55555555-5555-5555-5555-555555555555",
        }
        with self.assertRaises(SuspiciousOperation):
            self.backend.verify_token("unused-token")

    def test_linked_user_is_resolved_by_stable_entra_identity(self):
        user = User.objects.create_user(
            username="existing-user",
            email="old-address@example.org",
            password="local-password",
            is_staff=True,
        )
        EntraIdentity.objects.create(
            user=user,
            tenant_id=self.claims["tid"],
            object_id=self.claims["oid"],
            issuer=self.claims["iss"],
        )

        found_users = self.backend.filter_users_by_claims(self.claims)
        updated_user = self.backend.update_user(found_users.get(), self.claims)

        self.assertEqual(updated_user.pk, user.pk)
        self.assertTrue(updated_user.is_staff)
        self.assertEqual(updated_user.email, "person@example.org")
        self.assertEqual(updated_user.entra_identity.last_known_upn, "person@example.org")


@override_settings(**OIDC_SETTINGS)
class EntraLoginViewTests(TestCase):
    def test_login_page_shows_both_local_and_sso_options(self):
        response = self.client.get(reverse("login"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sign in with RMH SSO")
        self.assertContains(response, "sign in with a local account")

    def test_registered_callback_has_no_trailing_slash(self):
        self.assertEqual(reverse("oidc_authentication_callback"), "/entra/login-success")

    def test_sso_login_starts_authorization_code_flow_at_registered_callback(self):
        response = self.client.get(reverse("oidc_authentication_init"))
        query = parse_qs(urlparse(response["Location"]).query)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(
            query["redirect_uri"], ["http://testserver/entra/login-success"]
        )


class LinkEntraIdentitiesCommandTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="existing-user")
        file_descriptor, self.csv_path = tempfile.mkstemp(suffix=".csv")
        os.close(file_descriptor)
        with open(self.csv_path, "w", encoding="utf-8", newline="") as csv_file:
            csv_file.write(
                "username,tenant_id,object_id,upn\n"
                "existing-user,11111111-1111-1111-1111-111111111111,"
                "44444444-4444-4444-4444-444444444444,person@example.org\n"
            )

    def tearDown(self):
        os.unlink(self.csv_path)

    def test_requires_apply_before_creating_identity_links(self):
        call_command("link_entra_identities", self.csv_path)
        self.assertFalse(EntraIdentity.objects.exists())

        call_command("link_entra_identities", self.csv_path, "--apply")
        self.assertEqual(
            self.user.entra_identity.object_id,
            "44444444-4444-4444-4444-444444444444",
        )


class TransferEntraIdentityCommandTests(TestCase):
    def setUp(self):
        self.source_user = User.objects.create_user(username="entra_source")
        self.target_user = User.objects.create_user(
            username="existing-user",
            is_staff=True,
        )
        EntraIdentity.objects.create(
            user=self.source_user,
            tenant_id="11111111-1111-1111-1111-111111111111",
            object_id="44444444-4444-4444-4444-444444444444",
            issuer="https://login.microsoftonline.com/11111111-1111-1111-1111-111111111111/v2.0",
        )

    def test_transfer_preserves_target_user_and_moves_identity(self):
        call_command(
            "transfer_entra_identity",
            self.source_user.username,
            self.target_user.username,
            "--apply",
        )

        self.assertTrue(User.objects.get(pk=self.target_user.pk).is_staff)
        self.assertEqual(
            self.target_user.entra_identity.object_id,
            "44444444-4444-4444-4444-444444444444",
        )
        self.assertTrue(User.objects.filter(pk=self.source_user.pk).exists())
        self.assertFalse(User.objects.get(pk=self.source_user.pk).is_active)


class EntraIdentityAdminTests(TestCase):
    def setUp(self):
        self.admin_user = User.objects.create_superuser(
            username="admin",
            password="admin-password",
        )
        self.source_user = User.objects.create_user(username="entra_source")
        self.target_user = User.objects.create_user(username="existing-user")
        EntraIdentity.objects.create(
            user=self.source_user,
            tenant_id="11111111-1111-1111-1111-111111111111",
            object_id="44444444-4444-4444-4444-444444444444",
            issuer="https://login.microsoftonline.com/11111111-1111-1111-1111-111111111111/v2.0",
        )
        self.client.force_login(self.admin_user)
        self.url = reverse(
            "admin:auth_user_transfer_entra_identity",
            args=[self.source_user.pk],
        )

    def test_admin_can_transfer_an_entra_identity_to_existing_user(self):
        response = self.client.get(self.url)
        self.assertContains(response, "Transfer Entra identity")

        response = self.client.post(self.url, {"target_user": self.target_user.pk})

        self.assertRedirects(
            response,
            reverse("admin:auth_user_change", args=[self.target_user.pk]),
        )
        self.assertEqual(
            self.target_user.entra_identity.object_id,
            "44444444-4444-4444-4444-444444444444",
        )
        self.assertFalse(User.objects.get(pk=self.source_user.pk).is_active)

    def test_admin_user_page_shows_entra_identity_transfer_link(self):
        response = self.client.get(
            reverse("admin:auth_user_change", args=[self.source_user.pk])
        )

        self.assertContains(response, "Transfer Entra identity")
