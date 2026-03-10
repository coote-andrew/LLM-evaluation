"""Dashboard / home page view."""

from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView

from core.models import TestCase, TestRun, RunStatus


class DashboardView(LoginRequiredMixin, TemplateView):
    """Overview of recent runs, active runs, quick stats."""

    template_name = "core/dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["test_cases_count"] = TestCase.objects.count()
        context["recent_runs"] = TestRun.objects.select_related(
            "prompt_template", "model_config", "test_case_version__test_case"
        ).order_by("-created_at")[:10]
        context["active_runs"] = TestRun.objects.filter(
            status__in=[RunStatus.PENDING, RunStatus.RUNNING]
        ).select_related("prompt_template", "model_config")[:5]
        return context
