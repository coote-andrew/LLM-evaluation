"""Public landing page."""

from django.views.generic import TemplateView


class HomeView(TemplateView):
    """Public Cicada landing page (no login required)."""

    template_name = "core/home.html"
