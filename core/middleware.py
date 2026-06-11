"""Authentication-related middleware."""

from urllib.parse import urlencode

from django.conf import settings
from django.shortcuts import redirect
from django.urls import reverse

from core.models import UserProfile


class MustChangePasswordMiddleware:
    """Keep temporary-password users on the password-change flow."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user and user.is_authenticated:
            profile, _ = UserProfile.objects.get_or_create(user=user)
            if profile.must_change_password and not self._is_allowed_path(request):
                query = urlencode({"next": request.get_full_path()})
                return redirect(f"{reverse('password_change')}?{query}")

        return self.get_response(request)

    def _is_allowed_path(self, request):
        allowed_paths = {
            reverse("password_change"),
            reverse("logout"),
        }
        return (
            request.path_info in allowed_paths
            or request.path_info.startswith(settings.STATIC_URL)
            or request.path_info.startswith(settings.MEDIA_URL)
        )
