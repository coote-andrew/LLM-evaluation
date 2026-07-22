"""Help / guides views — Markdown articles under ``core/guides/``."""

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404
from django.shortcuts import redirect
from django.views.generic import TemplateView

from core.services.guides import get_guide, guides_by_group, load_guides


class HelpIndexView(LoginRequiredMixin, TemplateView):
    """Redirect to the first guide, or show an empty state."""

    template_name = "core/help_article.html"

    def get(self, request, *args, **kwargs):
        articles = load_guides()
        if articles:
            return redirect("core:help_article", slug=articles[0].slug)
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["guide_groups"] = guides_by_group()
        ctx["article"] = None
        return ctx


class HelpArticleView(LoginRequiredMixin, TemplateView):
    """Render one guide article with an auto-built TOC."""

    template_name = "core/help_article.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        slug = self.kwargs["slug"]
        article = get_guide(slug)
        if article is None:
            raise Http404(f"No help guide named “{slug}”.")
        ctx["article"] = article
        ctx["guide_groups"] = guides_by_group()
        return ctx
