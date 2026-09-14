"""Entra OIDC authentication and local-account linking."""

import logging
import secrets

from django.conf import settings
from django.core.exceptions import SuspiciousOperation
from mozilla_django_oidc.auth import OIDCAuthenticationBackend

from core.models import EntraIdentity, UserProfile

logger = logging.getLogger(__name__)


class EntraOIDCAuthenticationBackend(OIDCAuthenticationBackend):
    """Authenticate approved Entra users against a stable local identity."""

    def verify_token(self, token, **kwargs):
        """Validate Entra tenant and audience in addition to the package checks."""
        claims = super().verify_token(token, **kwargs)
        expected_issuer = settings.ENTRA_OIDC_ISSUER
        issuer = claims.get("iss")
        if not isinstance(issuer, str) or not secrets.compare_digest(
            issuer, expected_issuer
        ):
            logger.warning("Entra OIDC token rejected: issuer did not match configured tenant.")
            raise SuspiciousOperation("Entra token issuer did not match configured tenant.")

        audience = claims.get("aud")
        audiences = audience if isinstance(audience, list) else [audience]
        if settings.ENTRA_CLIENT_ID not in audiences:
            logger.warning("Entra OIDC token rejected: client audience did not match.")
            raise SuspiciousOperation("Entra token audience did not match client.")
        if len(audiences) > 1 and claims.get("azp") != settings.ENTRA_CLIENT_ID:
            logger.warning(
                "Entra OIDC token rejected: authorized party did not match client."
            )
            raise SuspiciousOperation(
                "Entra token authorized party did not match client."
            )

        if claims.get("tid") != settings.ENTRA_TENANT_ID:
            logger.warning("Entra OIDC token rejected: tenant ID claim did not match.")
            raise SuspiciousOperation("Entra token tenant ID did not match configured tenant.")
        return claims

    def get_userinfo(self, access_token, id_token, payload):
        """Use the signed ID token: the required Entra claims are present there."""
        return payload

    def verify_claims(self, claims):
        """Require a stable Entra identity and membership of the access group."""
        required_claims = ("oid", "tid", "iss")
        missing_claims = [claim for claim in required_claims if not claims.get(claim)]
        if missing_claims:
            logger.warning(
                "Entra OIDC login rejected: required claims missing: %s.",
                ", ".join(missing_claims),
            )
            return False

        if not settings.ENTRA_REQUIRE_GROUP_CLAIM:
            logger.info(
                "Entra OIDC login accepted without token group check; "
                "Enterprise Application assignment is the access boundary."
            )
            return True

        groups = claims.get("groups")
        if not isinstance(groups, list):
            if claims.get("hasgroups") or "_claim_names" in claims:
                logger.warning(
                    "Entra OIDC login rejected: group overage requires a Graph lookup."
                )
            else:
                logger.warning(
                    "Entra OIDC login rejected: no groups claim was supplied."
                )
            return False

        approved_group = settings.ENTRA_APPROVED_GROUP_ID.casefold()
        if not any(
            isinstance(group_id, str) and group_id.casefold() == approved_group
            for group_id in groups
        ):
            logger.warning(
                "Entra OIDC login rejected: user is not in the configured access group."
            )
            return False
        return True

    def filter_users_by_claims(self, claims):
        """Find only users already linked to the immutable Entra object ID."""
        return self.UserModel.objects.filter(
            entra_identity__tenant_id=claims["tid"],
            entra_identity__object_id=claims["oid"],
        )

    def create_user(self, claims):
        """Create an active, unprivileged local user for an approved Entra identity."""
        username = self._new_username(claims["oid"])
        upn = self._upn_from_claims(claims)
        user = self.UserModel.objects.create_user(username=username, email=upn)
        user.set_unusable_password()
        user.save(update_fields=["password"])
        UserProfile.objects.get_or_create(
            user=user,
            defaults={"must_change_password": False},
        )
        EntraIdentity.objects.create(
            user=user,
            tenant_id=claims["tid"],
            object_id=claims["oid"],
            issuer=claims["iss"],
            last_known_upn=upn,
        )
        logger.info("Created local user from an approved Entra OIDC identity.")
        return user

    def update_user(self, user, claims):
        """Refresh non-authoritative display data without changing local RBAC."""
        upn = self._upn_from_claims(claims)
        identity = user.entra_identity
        fields = []
        if identity.last_known_upn != upn:
            identity.last_known_upn = upn
            fields.append("last_known_upn")
        identity.save(update_fields=[*fields, "last_login_at"])

        if upn and user.email != upn:
            user.email = upn
            user.save(update_fields=["email"])
        return user

    def _new_username(self, object_id):
        base_username = f"entra_{object_id.replace('-', '')}"
        username = base_username[:150]
        suffix = 1
        while self.UserModel.objects.filter(username=username).exists():
            suffix_text = f"_{suffix}"
            username = f"{base_username[:150 - len(suffix_text)]}{suffix_text}"
            suffix += 1
        return username

    @staticmethod
    def _upn_from_claims(claims):
        return claims.get("email") or claims.get("preferred_username") or ""
