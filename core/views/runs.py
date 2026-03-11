"""Test run views: create, monitor, results."""

import socket
import threading
from urllib.parse import urlparse

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect
from django.views.generic import DeleteView, DetailView, FormView, ListView
from django.urls import reverse_lazy

from core.forms import TestRunCreateForm
from core.models import TestRun
from core.tasks import execute_test_run


def _broker_reachable() -> bool:
    """Return True if the Celery broker TCP port is accepting connections."""
    try:
        parsed = urlparse(settings.CELERY_BROKER_URL)
        host = parsed.hostname or "localhost"
        port = parsed.port or 6379
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


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

        if _broker_reachable():
            execute_test_run.delay(str(run.id))
        else:
            # No Celery broker — run in a background thread instead.
            t = threading.Thread(target=execute_test_run, args=(str(run.id),), daemon=True)
            t.start()
        messages.success(self.request, f"Run {str(run.id)[:8]} started.")
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
        ).prefetch_related(
            "results__test_case_row",
            "evaluation_runs__evaluation_config",
            "evaluation_runs__results",
        )

    def get_context_data(self, **kwargs):
        from core.views.evaluations import compute_accuracy
        ctx = super().get_context_data(**kwargs)
        # Attach accuracy to each completed evaluation run for display
        ctx["eval_runs_with_accuracy"] = [
            (er, compute_accuracy(er))
            for er in self.object.evaluation_runs.all()
        ]
        return ctx


class TestRunDeleteView(LoginRequiredMixin, DeleteView):
    """Delete a test run and all its results."""

    model = TestRun
    success_url = reverse_lazy("core:testrun_list")
    # Confirmation is handled via JS in the list template; redirect GET to list.
    def get(self, request, *args, **kwargs):
        return redirect("core:testrun_list")


class TestRunDeleteView(LoginRequiredMixin, DeleteView):
    """Delete a test run."""

    model = TestRun
    success_url = reverse_lazy("core:testrun_list")

    def get(self, request, *args, **kwargs):
        return self.post(request, *args, **kwargs)

    def form_valid(self, form):
        run = self.get_object()
        messages.success(self.request, f"Run {str(run.id)[:8]} deleted.")
        return super().form_valid(form)
