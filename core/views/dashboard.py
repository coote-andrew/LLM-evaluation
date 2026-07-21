"""Dashboard / home page view."""

from collections import defaultdict

from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView

from core.access import visible_projects, visible_test_runs
from core.models import RunStatus


class DashboardView(LoginRequiredMixin, TemplateView):
    """Overview of recent runs, active runs, quick stats."""

    template_name = "core/dashboard.html"

    def get_context_data(self, **kwargs):
        from core.views.evaluations import compute_accuracy
        context = super().get_context_data(**kwargs)
        context["test_cases_count"] = visible_projects(self.request.user).count()
        context["active_runs"] = visible_test_runs(self.request.user).filter(
            status__in=[RunStatus.PENDING, RunStatus.RUNNING]
        ).select_related("prompt_template", "model_config", "test_case_version__test_case")[:5]

        # Build per-model summary: all completed runs grouped by model, with their eval results
        all_runs = visible_test_runs(self.request.user).select_related(
            "prompt_template", "model_config", "test_case_version__test_case"
        ).prefetch_related(
            "evaluation_runs__evaluation_config",
            "evaluation_runs__results",
        ).order_by("model_config__name", "-created_at")

        models_data = {}  # model_config_id -> {model, runs: [...]}
        for run in all_runs:
            mc = run.model_config
            if mc.pk not in models_data:
                models_data[mc.pk] = {"model": mc, "runs": []}
            run_evals = []
            for er in run.evaluation_runs.all():
                acc = compute_accuracy(er)
                run_evals.append({
                    "eval_run": er,
                    "accuracy": acc,
                })
            models_data[mc.pk]["runs"].append({
                "run": run,
                "evals": run_evals,
            })

        context["models_data"] = list(models_data.values())
        return context
