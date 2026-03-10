"""Test run views: create, monitor, results."""

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect
from django.views.generic import DetailView, FormView, ListView

from core.forms import TestRunCreateForm
from core.models import TestRun
from core.tasks import execute_test_run


class TestRunListView(LoginRequiredMixin, ListView):
    """List test runs."""

    model = TestRun
    template_name = "core/testrun_list.html"
    context_object_name = "test_runs"
    paginate_by = 20

    def get_queryset(self):
        return (
            TestRun.objects.select_related(
                "prompt_template",
                "model_config",
                "test_case_version__test_case",
            )
            .order_by("-created_at")
        )


class TestRunCreateView(LoginRequiredMixin, FormView):
    """Create and start a test run."""

    template_name = "core/testrun_create.html"
    form_class = TestRunCreateForm

    def form_valid(self, form):
        version = form.cleaned_data["test_case_version"]
        prompt_template = form.cleaned_data["prompt_template"]
        model_config = form.cleaned_data["model_config"]
        row_limit = form.cleaned_data.get("row_limit")

        if prompt_template.test_case_id != version.test_case_id:
            messages.error(self.request, "Prompt template must belong to the same test case.")
            return redirect("core:testrun_create")

        run = TestRun.objects.create(
            test_case_version=version,
            prompt_template=prompt_template,
            model_config=model_config,
            row_limit=row_limit,
            prompt_snapshot=prompt_template.template_text,
            rows_total=version.row_count if not row_limit else min(row_limit, version.row_count),
            created_by=self.request.user,
        )

        execute_test_run.delay(str(run.id))
        messages.success(self.request, f"Run {str(run.id)[:8]} started. Redirecting to monitor.")
        return redirect("core:testrun_detail", pk=run.pk)


class TestRunDetailView(LoginRequiredMixin, DetailView):
    """Run detail: progress, results table."""

    model = TestRun
    template_name = "core/testrun_detail.html"
    context_object_name = "test_run"

    def get_queryset(self):
        return TestRun.objects.select_related(
            "prompt_template",
            "model_config",
            "test_case_version__test_case",
        ).prefetch_related("results__test_case_row")
