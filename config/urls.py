"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import logout as auth_logout
from django.shortcuts import redirect
from django.urls import include, path
from django.views.decorators.http import require_POST
from mozilla_django_oidc.views import OIDCAuthenticationRequestView

from core.views.auth import FirstLoginPasswordChangeLoginView, ForcedPasswordChangeView
from core.oidc_views import EntraOIDCCallbackView


@require_POST
def logout_view(request):
    auth_logout(request)
    return redirect("login")


urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/logout/", logout_view, name="logout"),
    path("accounts/login/", FirstLoginPasswordChangeLoginView.as_view(), name="login"),
    path("accounts/password_change/", ForcedPasswordChangeView.as_view(), name="password_change"),
    path("accounts/", include("django.contrib.auth.urls")),
    # The callback is registered in Entra without a trailing slash. These are
    # dormant until ENTRA_OIDC_ENABLED exposes the login button.
    path(
        "entra/login-success",
        EntraOIDCCallbackView.as_view(),
        name="oidc_authentication_callback",
    ),
    path(
        "entra/login/",
        OIDCAuthenticationRequestView.as_view(),
        name="oidc_authentication_init",
    ),
    path("", include("core.urls")),
]
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
