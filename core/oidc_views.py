"""OIDC views with safe operational diagnostics for Entra sign-in."""

import logging

from mozilla_django_oidc.views import OIDCAuthenticationCallbackView

logger = logging.getLogger(__name__)


class EntraOIDCCallbackView(OIDCAuthenticationCallbackView):
    """Log safe callback failure context without exposing codes or tokens."""

    def login_failure(self):
        error = self.request.GET.get("error")
        if error:
            logger.warning("Entra OIDC callback failed with provider error: %s.", error)
        elif "code" not in self.request.GET:
            logger.warning("Entra OIDC callback failed: authorization code was absent.")
        elif "state" not in self.request.GET:
            logger.warning("Entra OIDC callback failed: state was absent.")
        else:
            logger.warning(
                "Entra OIDC callback failed after token validation or access-group checks."
            )
        return super().login_failure()
