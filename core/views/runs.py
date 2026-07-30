"""Test run views: create, monitor, results."""

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.generic import DeleteView, DetailView, FormView, ListView, View
from django.urls import reverse_lazy

RESULTS_PAGE_SIZE_DEFAULT = 50
RESULTS_PAGE_SIZE_MAX = 100


def _parse_page_size(request, default=RESULTS_PAGE_SIZE_DEFAULT):
    try:
        size = int(request.GET.get("page_size", default))
    except (TypeError, ValueError):
        size = default
    return min(max(size, 10), RESULTS_PAGE_SIZE_MAX)

from core.access import editable_projects, visible_model_configs, visible_projects, visible_test_runs
from core.forms import TestRunCreateForm
from core.models import ModelConfig, PromptTemplate, RunStatus, TestCaseVersion, TestRun
from core.services.costing import format_aud
from core.services.task_dispatch import dispatch_task
from core.tasks import execute_test_run


def _build_prompt_template_groups(form):
    """Return grouped prompt template choices for the create-run template.

    Each group dict:
      {
        "name": str,          # template series name
        "latest": pt,         # PromptTemplate with highest version_number
        "older":  [pt, ...],  # older versions, newest first (may be empty)
      }

    The queryset comes from the form's prompt_template field so that any
    test-case filtering applied to the form is respected.
    """
    if form is None:
        queryset = PromptTemplate.objects.select_related("test_case").order_by(
            "name", "-version_number"
        )
    else:
        queryset = form.fields["prompt_template"].queryset.order_by(
            "name", "-version_number"
        )

    groups = {}
    order = []
    for pt in queryset:
        key = (pt.test_case_id, pt.name)
        if key not in groups:
            groups[key] = {"name": pt.name, "latest": pt, "older": []}
            order.append(key)
        else:
            groups[key]["older"].append(pt)
    return [groups[k] for k in order]


def _group_test_runs(runs):
    """Group runs so that per (test_case, prompt_name) the latest test-case version
    is shown first; older-versioned runs are collapsed under a divider.

    Returns a list of dicts:
      {
        "key":    (test_case_name, prompt_name),
        "latest": [TestRun, ...],   # runs on the highest test_case_version for this group
        "older":  [TestRun, ...],   # runs on older test_case_versions, newest first
      }

    Within each bucket runs are ordered by -created_at (already assumed from queryset).
    Groups themselves are ordered by the created_at of their most-recent run.
    """
    # group_data[key] = {"max_version": int, "latest": [], "older": []}
    group_data = {}
    group_order = []  # preserve insertion order for most-recent-first

    for run in runs:
        key = (
            run.test_case_version.test_case_id,
            run.prompt_template.name,
        )
        version_num = run.test_case_version.version_number

        if key not in group_data:
            group_data[key] = {
                "key": (
                    run.test_case_version.test_case.name,
                    run.prompt_template.name,
                ),
                "max_version": version_num,
                "latest": [],
                "older": [],
            }
            group_order.append(key)

        entry = group_data[key]
        if version_num > entry["max_version"]:
            # This run is on a newer version — demote previous "latest" to "older"
            entry["older"] = entry["latest"] + entry["older"]
            entry["latest"] = [run]
            entry["max_version"] = version_num
        elif version_num == entry["max_version"]:
            entry["latest"].append(run)
        else:
            entry["older"].append(run)

    return [group_data[k] for k in group_order]


class TestRunListView(LoginRequiredMixin, ListView):
    """List test runs, grouped by (test case, prompt template name)."""

    model = TestRun
    template_name = "core/testrun_list.html"
    context_object_name = "test_runs"
    paginate_by = 20

    def _filter_project(self):
        """Return the selected project when ?project=<uuid> is valid and visible."""
        if hasattr(self, "_cached_filter_project"):
            return self._cached_filter_project
        project_id = self.request.GET.get("project")
        if not project_id:
            self._cached_filter_project = None
        else:
            self._cached_filter_project = (
                visible_projects(self.request.user)
                .filter(pk=project_id)
                .first()
            )
        return self._cached_filter_project

    def get_queryset(self):
        qs = (
            visible_test_runs(self.request.user).select_related(
                "prompt_template",
                "model_config",
                "test_case_version__test_case",
                "created_by",
            )
            .order_by("-created_at")
        )
        project = self._filter_project()
        if project is not None:
            qs = qs.filter(test_case_version__test_case=project)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["run_groups"] = _group_test_runs(ctx["test_runs"])
        filter_project = self._filter_project()
        ctx["filter_project"] = filter_project
        ctx["filter_projects"] = visible_projects(self.request.user).order_by("name")
        return ctx


class TestRunCreateView(LoginRequiredMixin, FormView):
    """Create and start a test run."""

    template_name = "core/testrun_create.html"
    form_class = TestRunCreateForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def get_initial(self):
        initial = super().get_initial()
        from_run_id = self.request.GET.get("from_run")
        if from_run_id:
            try:
                source_run = visible_test_runs(self.request.user).select_related(
                    "test_case_version", "prompt_template", "model_config"
                ).get(pk=from_run_id)
                initial["test_case_version"] = source_run.test_case_version
                initial["prompt_template"] = source_run.prompt_template
                initial["model_config"] = source_run.model_config
            except (TestRun.DoesNotExist, Exception):
                pass
        return initial

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        from_run_id = self.request.GET.get("from_run") or self.request.POST.get("from_run")
        if from_run_id:
            try:
                source_run = visible_test_runs(self.request.user).select_related(
                    "test_case_version__test_case"
                ).get(pk=from_run_id)
                test_case = source_run.test_case_version.test_case
                form.fields["test_case_version"].queryset = (
                    TestCaseVersion.objects.filter(
                        test_case__in=visible_test_runs(self.request.user)
                        .values("test_case_version__test_case"),
                        test_case=test_case,
                    )
                    .select_related("test_case")
                )
                form.fields["prompt_template"].queryset = (
                    PromptTemplate.objects.filter(
                        test_case__in=visible_test_runs(self.request.user)
                        .values("test_case_version__test_case"),
                        test_case=test_case,
                    )
                    .select_related("test_case")
                )
            except (TestRun.DoesNotExist, Exception):
                pass
        return form

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        from_run_id = self.request.GET.get("from_run") or self.request.POST.get("from_run")
        if from_run_id:
            try:
                ctx["source_run"] = visible_test_runs(self.request.user).select_related(
                    "test_case_version__test_case", "prompt_template", "model_config"
                ).get(pk=from_run_id)
            except (TestRun.DoesNotExist, Exception):
                pass
        ctx["from_run"] = from_run_id
        # Build grouped prompt template choices for the custom select widget
        form = ctx.get("form")
        ctx["prompt_template_groups"] = _build_prompt_template_groups(form)
        # Data for PHI filtering and rough cost estimates in the create UI.
        version_qs = (
            form.fields["test_case_version"].queryset
            if form is not None
            else TestCaseVersion.objects.none()
        )
        ctx["version_phi_flags"] = {
            str(v.pk): v.test_case.contains_phi for v in version_qs
        }
        model_qs = (
            form.fields["model_config"].queryset
            if form is not None
            else ModelConfig.objects.none()
        )
        # When PHI filtering already narrowed the queryset, still expose full
        # visible models for client-side estimates when version is not PHI.
        if form is not None and self.request.user.is_authenticated:
            all_models = visible_model_configs(self.request.user).filter(is_active=True)
        else:
            all_models = model_qs
        ctx["model_cost_meta"] = [
            {
                "id": str(m.pk),
                "phi_approved": m.is_phi_approved,
                "in_rate": (
                    str(m.cost_per_1m_input_tokens)
                    if m.cost_per_1m_input_tokens is not None
                    else ""
                ),
                "out_rate": (
                    str(m.cost_per_1m_output_tokens)
                    if m.cost_per_1m_output_tokens is not None
                    else ""
                ),
                "max_tokens": m.default_max_tokens,
            }
            for m in all_models
        ]
        prompt_qs = (
            form.fields["prompt_template"].queryset
            if form is not None
            else PromptTemplate.objects.none()
        )
        ctx["prompt_lengths"] = {
            str(p.pk): len(p.template_text or "") for p in prompt_qs
        }
        ctx["version_row_counts"] = {
            str(v.pk): v.row_count for v in version_qs
        }
        return ctx

    def post(self, request, *args, **kwargs):
        return super().post(request, *args, **kwargs)

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

        dispatch_task(execute_test_run, str(run.id))
        messages.success(self.request, f"Run {str(run.id)[:8]} started.")
        return redirect("core:testrun_detail", pk=run.pk)


class TestRunPromptTemplateOptionsView(LoginRequiredMixin, View):
    """HTMX partial: prompt template options filtered by test case version."""

    def get(self, request):
        form = TestRunCreateForm(data={
            "test_case_version": request.GET.get("test_case_version", ""),
            "prompt_template": request.GET.get("prompt_template", ""),
        }, user=request.user)
        return render(request, "core/_prompt_template_options.html", {
            "prompt_template_groups": _build_prompt_template_groups(form),
            "selected_prompt_template_id": request.GET.get("prompt_template", ""),
        })


class TestRunDetailView(LoginRequiredMixin, DetailView):
    """Run detail: progress, results table."""

    model = TestRun
    template_name = "core/testrun_detail.html"
    context_object_name = "test_run"

    def get_queryset(self):
        return visible_test_runs(self.request.user).select_related(
            "prompt_template",
            "model_config",
            "test_case_version__test_case",
        ).prefetch_related(
            "evaluation_runs__evaluation_config",
            "evaluation_runs__results",
        )

    def get_context_data(self, **kwargs):
        from core.views.evaluations import compute_accuracy
        ctx = super().get_context_data(**kwargs)
        ctx["eval_runs_with_accuracy"] = [
            (er, compute_accuracy(er))
            for er in self.object.evaluation_runs.all()
        ]

        page_size = _parse_page_size(self.request)
        results_qs = (
            self.object.results
            .select_related("test_case_row")
            .order_by("test_case_row__row_number")
        )
        paginator = Paginator(results_qs, page_size)
        page_obj = paginator.get_page(self.request.GET.get("page", 1))

        ctx["page_obj"] = page_obj
        ctx["page_results"] = page_obj.object_list
        ctx["is_paginated"] = paginator.num_pages > 1
        ctx["page_size"] = page_size
        ctx["is_tail_page"] = (
            self.object.status in ("pending", "running")
            and page_obj.number == paginator.num_pages
        )

        run = self.object
        run_cost = run.model_config.estimate_cost_aud(
            run.total_input_tokens or 0,
            run.total_output_tokens or 0,
        )
        ctx["run_cost_aud"] = run_cost
        ctx["run_cost_display"] = format_aud(run_cost)

        contains_phi = run.test_case_version.test_case.contains_phi
        compare_models = []
        for model in visible_model_configs(self.request.user).filter(is_active=True):
            if (
                model.cost_per_1m_input_tokens is None
                or model.cost_per_1m_output_tokens is None
            ):
                continue
            if contains_phi and not model.is_phi_approved:
                continue
            cost = model.estimate_cost_aud(
                run.total_input_tokens or 0,
                run.total_output_tokens or 0,
            )
            compare_models.append({
                "model": model,
                "cost": cost,
                "cost_display": format_aud(cost),
                "is_current": model.pk == run.model_config_id,
            })
        compare_models.sort(key=lambda row: (not row["is_current"], row["model"].name.lower()))
        ctx["cost_compare_models"] = compare_models
        return ctx


class TestRunDeleteView(LoginRequiredMixin, DeleteView):
    """Delete a test run and all its results."""

    model = TestRun
    success_url = reverse_lazy("core:testrun_list")

    def get_queryset(self):
        return TestRun.objects.filter(
            test_case_version__test_case__in=editable_projects(self.request.user)
        )
    # Confirmation is handled via JS in the list template; redirect GET to list.
    def get(self, request, *args, **kwargs):
        return redirect("core:testrun_list")


class TestRunDeleteView(LoginRequiredMixin, DeleteView):
    """Delete a test run."""

    model = TestRun
    success_url = reverse_lazy("core:testrun_list")

    def get_queryset(self):
        return TestRun.objects.filter(
            test_case_version__test_case__in=editable_projects(self.request.user)
        )

    def get(self, request, *args, **kwargs):
        return self.post(request, *args, **kwargs)

    def form_valid(self, form):
        run = self.get_object()
        messages.success(self.request, f"Run {str(run.id)[:8]} deleted.")
        return super().form_valid(form)


class CancelTestRunView(LoginRequiredMixin, View):
    """Cancel a running test run by setting its status to CANCELLED.

    The worker checks this status between LLM requests and will stop after
    any in-flight request completes.
    """

    def post(self, request, pk):
        run = get_object_or_404(
            TestRun.objects.filter(
                test_case_version__test_case__in=editable_projects(request.user)
            ),
            pk=pk,
        )
        if run.status in (RunStatus.PENDING, RunStatus.RUNNING):
            run.status = RunStatus.CANCELLED
            run.save(update_fields=["status"])
            messages.success(request, f"Run {str(run.id)[:8]} cancelled — it will stop after the current request.")
        else:
            messages.warning(request, f"Run {str(run.id)[:8]} is already {run.get_status_display().lower()} and cannot be cancelled.")
        return redirect("core:testrun_detail", pk=pk)


class TestRunStatusView(LoginRequiredMixin, View):
    """Return live status/progress for a test run as JSON.

    Used by the detail page's fetch-based polling so it can update
    progress counters without a full page reload.
    """

    def get(self, request, pk):
        run = get_object_or_404(
            visible_test_runs(request.user).only(
                "status",
                "rows_completed",
                "rows_total",
                "rows_failed",
                "total_duration_seconds",
                "total_input_tokens",
                "total_output_tokens",
                "error_message",
            ),
            pk=pk,
        )
        return JsonResponse({
            "status": run.status,
            "status_display": run.get_status_display(),
            "rows_completed": run.rows_completed,
            "rows_total": run.rows_total,
            "rows_failed": run.rows_failed or 0,
            "total_duration_seconds": run.total_duration_seconds,
            "total_input_tokens": run.total_input_tokens or 0,
            "total_output_tokens": run.total_output_tokens or 0,
            "error_message": run.error_message or "",
            "result_count": run.results.count(),
        })


class TestRunResultsPartialView(LoginRequiredMixin, View):
    """HTMX partial: paginated results table + pagination bar for one page.

    Accepts ?page= and ?page_size= query params. Returns a full results
    section fragment (table rows + pagination bar) for HTMX to swap into
    #results-section on the detail page.
    """

    def get(self, request, pk):
        run = get_object_or_404(
            visible_test_runs(request.user).select_related(
                "prompt_template", "model_config", "test_case_version__test_case"
            ),
            pk=pk,
        )
        page_size = _parse_page_size(request)
        results_qs = (
            run.results
            .select_related("test_case_row")
            .order_by("test_case_row__row_number")
        )
        paginator = Paginator(results_qs, page_size)
        page_obj = paginator.get_page(request.GET.get("page", 1))

        return render(request, "core/testrun_results_partial.html", {
            "test_run": run,
            "page_obj": page_obj,
            "page_results": page_obj.object_list,
            "is_paginated": paginator.num_pages > 1,
            "page_size": page_size,
            "is_tail_page": (
                run.status in ("pending", "running")
                and page_obj.number == paginator.num_pages
            ),
        })
