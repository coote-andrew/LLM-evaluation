"""Evaluation views: configure, run, human review."""

from __future__ import annotations

import json
import re
import threading

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html, mark_safe
from django.views import View
from django.views.generic import DetailView, ListView

from core.models import (
    AssessorType,
    EvalRunStatus,
    EvalType,
    EvaluationConfig,
    EvaluationResult,
    EvaluationRun,
    ModelConfig,
    TestCase,
    TestRun,
    TestRunResult,
)
from core.services.scorer import score_result


# ---------------------------------------------------------------------------
# Evaluation config
# ---------------------------------------------------------------------------

class EvaluationConfigCreateView(LoginRequiredMixin, View):
    """Create an evaluation config for a test case."""

    template_name = "core/evaluationconfig_form.html"

    def _get_test_case(self, test_case_id):
        return get_object_or_404(TestCase, pk=test_case_id)

    def _base_context(self, test_case, form_data=None, editing=None):
        return {
            "test_case": test_case,
            "eval_types": EvalType.choices,
            "model_configs": ModelConfig.objects.filter(is_active=True).order_by("name"),
            "form_data": form_data or {},
            **({"editing": editing} if editing else {}),
        }

    def get(self, request, test_case_id):
        test_case = self._get_test_case(test_case_id)
        return render(request, self.template_name, self._base_context(test_case))

    def post(self, request, test_case_id):
        test_case = self._get_test_case(test_case_id)
        name = request.POST.get("name", "").strip()
        eval_type = request.POST.get("eval_type", "")
        raw_criteria = request.POST.get("scoring_criteria", "{}").strip()
        judge_prompt = request.POST.get("judge_prompt_template", "").strip()
        judge_model_id = request.POST.get("judge_model_config", "").strip()

        if not name:
            messages.error(request, "Name is required.")
            return render(request, self.template_name, self._base_context(test_case, request.POST))

        try:
            scoring_criteria = json.loads(raw_criteria) if raw_criteria else {}
        except json.JSONDecodeError as e:
            messages.error(request, f"Invalid JSON in scoring criteria: {e}")
            return render(request, self.template_name, self._base_context(test_case, request.POST))

        judge_model = None
        if judge_model_id:
            try:
                judge_model = ModelConfig.objects.get(pk=judge_model_id)
            except ModelConfig.DoesNotExist:
                pass

        config = EvaluationConfig.objects.create(
            test_case=test_case,
            name=name,
            eval_type=eval_type,
            judge_prompt_template=judge_prompt,
            judge_model_config=judge_model,
            scoring_criteria=scoring_criteria,
            created_by=request.user,
        )
        messages.success(request, f"Evaluation config '{config.name}' created.")
        return redirect("core:testcase_detail", pk=test_case.pk)


class EvaluationConfigUpdateView(LoginRequiredMixin, View):
    """Edit an existing evaluation config."""

    template_name = "core/evaluationconfig_form.html"

    def _get_config(self, pk):
        return get_object_or_404(EvaluationConfig.objects.select_related("test_case"), pk=pk)

    def _base_context(self, config, form_data=None):
        return {
            "test_case": config.test_case,
            "eval_types": EvalType.choices,
            "model_configs": ModelConfig.objects.filter(is_active=True).order_by("name"),
            "editing": config,
            "form_data": form_data or {
                "name": config.name,
                "eval_type": config.eval_type,
                "judge_prompt_template": config.judge_prompt_template,
                "scoring_criteria_json": json.dumps(config.scoring_criteria),
                "judge_model_config": str(config.judge_model_config_id) if config.judge_model_config_id else "",
            },
        }

    def get(self, request, pk):
        config = self._get_config(pk)
        return render(request, self.template_name, self._base_context(config))

    def post(self, request, pk):
        config = self._get_config(pk)
        test_case = config.test_case
        name = request.POST.get("name", "").strip()
        eval_type = request.POST.get("eval_type", "")
        raw_criteria = request.POST.get("scoring_criteria", "{}").strip()
        judge_prompt = request.POST.get("judge_prompt_template", "").strip()
        judge_model_id = request.POST.get("judge_model_config", "").strip()

        if not name:
            messages.error(request, "Name is required.")
            return render(request, self.template_name, self._base_context(config, request.POST))

        try:
            scoring_criteria = json.loads(raw_criteria) if raw_criteria else {}
        except json.JSONDecodeError as e:
            messages.error(request, f"Invalid JSON in scoring criteria: {e}")
            return render(request, self.template_name, self._base_context(config, request.POST))

        judge_model = None
        if judge_model_id:
            try:
                judge_model = ModelConfig.objects.get(pk=judge_model_id)
            except ModelConfig.DoesNotExist:
                pass

        config.name = name
        config.eval_type = eval_type
        config.judge_prompt_template = judge_prompt
        config.judge_model_config = judge_model
        config.scoring_criteria = scoring_criteria
        config.save(update_fields=["name", "eval_type", "judge_prompt_template", "judge_model_config", "scoring_criteria"])
        messages.success(request, f"Evaluation config '{config.name}' updated.")
        return redirect("core:testcase_detail", pk=test_case.pk)


# ---------------------------------------------------------------------------
# Evaluation run: start + detail
# ---------------------------------------------------------------------------

class EvaluationRunCreateView(LoginRequiredMixin, View):
    """Start an evaluation run against a completed test run."""

    template_name = "core/evaluationrun_create.html"

    def _context(self, test_run, form_data=None, errors=None):
        configs = EvaluationConfig.objects.filter(
            test_case=test_run.test_case_version.test_case
        ).select_related("judge_model_config")
        return {
            "test_run": test_run,
            "configs": configs,
            "eval_types": EvalType.choices,
            "model_configs": ModelConfig.objects.filter(is_active=True).order_by("name"),
            "form_data": form_data or {},
            "errors": errors or [],
        }

    def get(self, request, test_run_id):
        test_run = get_object_or_404(TestRun, pk=test_run_id)
        return render(request, self.template_name, self._context(test_run))

    def post(self, request, test_run_id):
        test_run = get_object_or_404(TestRun, pk=test_run_id)
        action = request.POST.get("action", "start")

        # --- create a new config inline, then continue ---
        if action == "create_config":
            name = request.POST.get("new_config_name", "").strip()
            eval_type = request.POST.get("new_eval_type", "")
            raw_criteria = request.POST.get("new_scoring_criteria", "{}").strip()
            errors = []
            if not name:
                errors.append("Config name is required.")
            try:
                criteria = json.loads(raw_criteria) if raw_criteria else {}
            except json.JSONDecodeError as e:
                errors.append(f"Invalid JSON: {e}")
                criteria = {}
            if errors:
                return render(request, self.template_name,
                              self._context(test_run, request.POST, errors))
            judge_model_id = request.POST.get("new_judge_model", "").strip()
            judge_model = None
            if judge_model_id:
                from core.models import ModelConfig
                try:
                    judge_model = ModelConfig.objects.get(pk=judge_model_id)
                except ModelConfig.DoesNotExist:
                    pass
            EvaluationConfig.objects.create(
                test_case=test_run.test_case_version.test_case,
                name=name,
                eval_type=eval_type,
                judge_prompt_template=request.POST.get("new_judge_prompt", "").strip(),
                judge_model_config=judge_model,
                scoring_criteria=criteria,
                created_by=request.user,
            )
            messages.success(request, f"Config '{name}' created.")
            return redirect(request.path)

        # --- start the evaluation run ---
        config_id = request.POST.get("evaluation_config")
        is_gold = request.POST.get("is_gold_standard") == "on"

        if not config_id:
            ctx = self._context(test_run)
            ctx["errors"] = ["Please select an evaluation config."]
            return render(request, self.template_name, ctx)

        config = get_object_or_404(
            EvaluationConfig,
            pk=config_id,
            test_case=test_run.test_case_version.test_case,
        )

        eval_run = EvaluationRun.objects.create(
            evaluation_config=config,
            test_run=test_run,
            is_gold_standard=is_gold,
            created_by=request.user,
        )

        if config.eval_type == EvalType.KEYWORD_MATCH:
            t = threading.Thread(target=_run_keyword_eval, args=(eval_run.pk,), daemon=True)
            t.start()
            messages.success(request, "Keyword evaluation started.")
            return redirect("core:evaluationrun_detail", pk=eval_run.pk)

        if config.eval_type == EvalType.HUMAN:
            return redirect("core:human_review", eval_run_id=eval_run.pk)

        if config.eval_type == EvalType.AI_JUDGE:
            if not config.judge_model_config:
                messages.error(request, "This AI judge config has no judge model assigned. Edit the config to add one.")
                return redirect("core:evaluationrun_detail", pk=eval_run.pk)
            t = threading.Thread(target=_run_ai_judge_eval, args=(eval_run.pk,), daemon=True)
            t.start()
            messages.success(request, "AI judge evaluation started.")
            return redirect("core:evaluationrun_detail", pk=eval_run.pk)

        messages.warning(request, "Unknown evaluation type.")
        return redirect("core:evaluationrun_detail", pk=eval_run.pk)


class EvaluationRunListView(LoginRequiredMixin, ListView):
    """List all evaluation runs."""

    model = EvaluationRun
    template_name = "core/evaluationrun_list.html"
    context_object_name = "eval_runs"
    paginate_by = 30

    def get_queryset(self):
        return EvaluationRun.objects.select_related(
            "evaluation_config__test_case",
            "test_run__model_config",
        ).prefetch_related("results").order_by("-created_at")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["eval_runs_with_accuracy"] = [
            (er, compute_accuracy(er)) for er in ctx["eval_runs"]
        ]
        return ctx


def describe_config(config) -> list[str]:
    """
    Return a list of plain-English sentences describing what an evaluation
    config checks, suitable for display at the top of an eval run page.
    """
    criteria = config.scoring_criteria or {}
    lines = []

    if config.eval_type == EvalType.KEYWORD_MATCH:
        for check in criteria.get("checks", []):
            name = check.get("name", "unnamed")
            ctype = check.get("type", "")
            phrase = check.get("phrase") or check.get("expected_value", "")
            path = check.get("json_path", "")
            case = "case-sensitive" if check.get("case_sensitive") else "case-insensitive"
            if ctype == "contains_phrase":
                lines.append(f'<strong>{name}</strong>: response contains "{phrase}" ({case})')
            elif ctype == "json_key_contains":
                lines.append(f'<strong>{name}</strong>: JSON field <code>{path}</code> contains "{phrase}" ({case})')
            elif ctype == "json_key_equals":
                lines.append(f'<strong>{name}</strong>: JSON field <code>{path}</code> equals "{phrase}" ({case})')
            else:
                lines.append(f'<strong>{name}</strong>: {ctype}')

    elif config.eval_type == EvalType.HUMAN:
        for field in criteria.get("review_fields", []):
            label = field.get("label") or field.get("name", "unnamed")
            ftype = field.get("type", "text")
            if ftype == "boolean":
                lines.append(f'<strong>{label}</strong> — yes / no')
            elif ftype == "integer":
                lo, hi = field.get("min", 0), field.get("max", 10)
                lines.append(f'<strong>{label}</strong> — integer {lo}–{hi}')
            else:
                lines.append(f'<strong>{label}</strong> — free text')

    elif config.eval_type == EvalType.AI_JUDGE:
        for field in criteria.get("output_fields", []):
            label = field.get("label") or field.get("name", "unnamed")
            ftype = field.get("type", "text")
            if ftype == "boolean":
                lines.append(f'<strong>{label}</strong> — boolean (used for accuracy)')
            elif ftype == "integer":
                lo, hi = field.get("min", 0), field.get("max", 10)
                lines.append(f'<strong>{label}</strong> — integer {lo}–{hi}')
            else:
                lines.append(f'<strong>{label}</strong> — free text')

    return [mark_safe(line) for line in lines]


def compute_accuracy(eval_run) -> dict | None:
    """
    Return accuracy stats for a completed evaluation run.

    A row is counted as "correct" if every boolean field in its assessment is True.
    Returns None if there are no results or no boolean fields.
    """
    results = list(eval_run.results.all())
    if not results:
        return None

    bool_fields = [
        f["name"]
        for f in eval_run.evaluation_config.scoring_criteria.get("review_fields", [])
        + eval_run.evaluation_config.scoring_criteria.get("checks", [])
        + eval_run.evaluation_config.scoring_criteria.get("output_fields", [])
        if f.get("type") == "boolean"
    ]
    # For keyword_match the checks themselves are boolean-valued in the assessment
    if eval_run.evaluation_config.eval_type == EvalType.KEYWORD_MATCH:
        # All keys in assessment are boolean check results
        sample = results[0].assessment if results else {}
        bool_fields = [k for k, v in sample.items() if isinstance(v, bool)]

    if not bool_fields:
        return None

    total = len(results)
    correct = 0
    field_stats: dict[str, int] = {f: 0 for f in bool_fields}

    for r in results:
        assessment = r.assessment or {}
        row_correct = all(assessment.get(f) is True for f in bool_fields)
        if row_correct:
            correct += 1
        for f in bool_fields:
            if assessment.get(f) is True:
                field_stats[f] += 1

    pct = round(correct / total * 100, 1) if total else 0
    return {
        "correct": correct,
        "total": total,
        "pct": pct,
        "per_field": [
            {"name": f, "correct": field_stats[f],
             "pct": round(field_stats[f] / total * 100, 1) if total else 0}
            for f in bool_fields
        ],
    }


class EvaluationRunDetailView(LoginRequiredMixin, DetailView):
    """Show results of a completed evaluation run."""

    model = EvaluationRun
    template_name = "core/evaluationrun_detail.html"
    context_object_name = "eval_run"

    def get_queryset(self):
        return EvaluationRun.objects.select_related(
            "evaluation_config",
            "test_run__test_case_version__test_case",
            "test_run__prompt_template",
            "test_run__model_config",
        ).prefetch_related(
            "results__test_run_result__test_case_row",
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["accuracy"] = compute_accuracy(self.object)
        ctx["config_description"] = describe_config(self.object.evaluation_config)
        return ctx


class EvaluationConfigDeleteView(LoginRequiredMixin, View):
    """Delete an evaluation config and cascade its runs/results."""

    def post(self, request, pk):
        config = get_object_or_404(
            EvaluationConfig.objects.select_related("test_case"), pk=pk
        )
        test_case_pk = config.test_case_id
        name = config.name
        config.delete()
        messages.success(request, f"Evaluation config '{name}' deleted.")
        return redirect("core:testcase_detail", pk=test_case_pk)

    def get(self, request, pk):
        config = get_object_or_404(EvaluationConfig.objects.select_related("test_case"), pk=pk)
        return redirect("core:testcase_detail", pk=config.test_case_id)


class EvaluationRunDeleteView(LoginRequiredMixin, View):
    """Delete an evaluation run and all its results."""

    def post(self, request, pk):
        eval_run = get_object_or_404(
            EvaluationRun.objects.select_related("evaluation_config"),
            pk=pk,
        )
        test_run_pk = eval_run.test_run_id
        name = eval_run.evaluation_config.name
        eval_run.delete()
        messages.success(request, f"Evaluation '{name}' deleted.")
        return redirect("core:testrun_detail", pk=test_run_pk)

    def get(self, request, pk):
        return redirect("core:testrun_detail", pk=get_object_or_404(EvaluationRun, pk=pk).test_run_id)


# ---------------------------------------------------------------------------
# Human review
# ---------------------------------------------------------------------------

class HumanReviewView(LoginRequiredMixin, View):
    """Step through rows one at a time for human review."""

    template_name = "core/human_review.html"

    def _get_eval_run(self, eval_run_id):
        return get_object_or_404(
            EvaluationRun.objects.select_related(
                "evaluation_config",
                "test_run__test_case_version__test_case",
            ),
            pk=eval_run_id,
        )

    def get(self, request, eval_run_id):
        eval_run = self._get_eval_run(eval_run_id)
        row_number = int(request.GET.get("row", 1))

        results = list(
            TestRunResult.objects.filter(test_run=eval_run.test_run)
            .select_related("test_case_row")
            .order_by("test_case_row__row_number")
        )

        if not results:
            messages.warning(request, "This test run has no results to review.")
            return redirect("core:evaluationrun_detail", pk=eval_run.pk)

        total = len(results)
        idx = max(0, min(row_number - 1, total - 1))
        current_result = results[idx]

        existing = EvaluationResult.objects.filter(
            evaluation_run=eval_run,
            test_run_result=current_result,
        ).first()

        raw_review_fields = eval_run.evaluation_config.scoring_criteria.get("review_fields", [
            {"name": "correct", "type": "boolean", "label": "Is the output correct?"},
            {"name": "notes", "type": "text", "label": "Reviewer notes"},
        ])

        existing_assessment = existing.assessment if existing else {}
        # Attach the saved value to each field dict so templates don't need dict lookups
        review_fields = []
        for f in raw_review_fields:
            field = dict(f)
            field["saved_value"] = existing_assessment.get(field["name"], "")
            review_fields.append(field)

        reviewed_ids = set(
            EvaluationResult.objects.filter(evaluation_run=eval_run)
            .values_list("test_run_result_id", flat=True)
        )

        return render(request, self.template_name, {
            "eval_run": eval_run,
            "current_result": current_result,
            "existing_notes": existing.notes if existing else "",
            "review_fields": review_fields,
            "row_number": idx + 1,
            "total": total,
            "prev_row": idx if idx > 0 else None,
            "next_row": idx + 2 if idx < total - 1 else None,
            "reviewed_count": len(reviewed_ids),
            "is_reviewed": current_result.pk in reviewed_ids,
        })

    def post(self, request, eval_run_id):
        eval_run = self._get_eval_run(eval_run_id)
        result_id = request.POST.get("result_id")
        current_result = get_object_or_404(TestRunResult, pk=result_id, test_run=eval_run.test_run)
        row_number = int(request.POST.get("row_number", 1))

        review_fields = eval_run.evaluation_config.scoring_criteria.get("review_fields", [
            {"name": "correct", "type": "boolean", "label": "Is the output correct?"},
            {"name": "notes", "type": "text", "label": "Reviewer notes"},
        ])

        assessment = {}
        for field in review_fields:
            fname = field["name"]
            ftype = field.get("type", "text")
            if ftype == "boolean":
                assessment[fname] = request.POST.get(fname) == "true"
            elif ftype == "integer":
                try:
                    assessment[fname] = int(request.POST.get(fname, 0))
                except ValueError:
                    assessment[fname] = None
            else:
                assessment[fname] = request.POST.get(fname, "")

        notes = request.POST.get("notes", "")

        EvaluationResult.objects.update_or_create(
            evaluation_run=eval_run,
            test_run_result=current_result,
            defaults={
                "assessor_type": AssessorType.HUMAN,
                "assessor_id": str(request.user),
                "assessment": assessment,
                "notes": notes,
            },
        )

        total = TestRunResult.objects.filter(test_run=eval_run.test_run).count()
        reviewed = EvaluationResult.objects.filter(evaluation_run=eval_run).count()

        if reviewed >= total:
            eval_run.status = EvalRunStatus.COMPLETED
            eval_run.completed_at = timezone.now()
            eval_run.save(update_fields=["status", "completed_at"])

        next_row = int(request.POST.get("next_row") or 0)
        if next_row:
            return redirect(f"{reverse('core:human_review', args=[eval_run.pk])}?row={next_row}")
        return redirect("core:evaluationrun_detail", pk=eval_run.pk)


# ---------------------------------------------------------------------------
# Background task: keyword evaluation
# ---------------------------------------------------------------------------

def _run_keyword_eval(eval_run_pk):
    """Run keyword matching for all results in an eval run (called in thread)."""
    from core.models import EvaluationRun  # local import avoids circular issues in thread
    eval_run = EvaluationRun.objects.select_related("evaluation_config", "test_run").get(pk=eval_run_pk)
    eval_run.status = EvalRunStatus.IN_PROGRESS
    eval_run.save(update_fields=["status"])

    criteria = eval_run.evaluation_config.scoring_criteria
    results = TestRunResult.objects.filter(test_run=eval_run.test_run).select_related("test_case_row")

    for result in results:
        assessment = score_result(result, criteria)
        EvaluationResult.objects.update_or_create(
            evaluation_run=eval_run,
            test_run_result=result,
            defaults={
                "assessor_type": AssessorType.AI,
                "assessor_id": "keyword_match",
                "assessment": assessment,
            },
        )

    eval_run.status = EvalRunStatus.COMPLETED
    eval_run.completed_at = timezone.now()
    eval_run.save(update_fields=["status", "completed_at"])


# ---------------------------------------------------------------------------
# Background task: AI judge evaluation
# ---------------------------------------------------------------------------

def _run_ai_judge_eval(eval_run_pk):
    """Call a judge LLM for every result row and store its assessment."""
    from core.models import EvaluationRun
    from core.services.llm_client import call_llm

    eval_run = EvaluationRun.objects.select_related(
        "evaluation_config__judge_model_config",
        "test_run",
    ).get(pk=eval_run_pk)

    eval_run.status = EvalRunStatus.IN_PROGRESS
    eval_run.save(update_fields=["status"])

    config = eval_run.evaluation_config
    judge_model = config.judge_model_config
    prompt_template = config.judge_prompt_template or ""
    output_fields = config.scoring_criteria.get("output_fields", [])
    model_id = judge_model.model_name if judge_model else "unknown"

    results = TestRunResult.objects.filter(
        test_run=eval_run.test_run
    ).select_related("test_case_row")

    for result in results:
        # Build the judge prompt
        input_text = "\n".join(
            f"{k}: {v}" for k, v in (result.test_case_row.input_fields or {}).items()
        )
        expected_text = "\n".join(
            f"{k}: {v}" for k, v in (result.test_case_row.expected_output_fields or {}).items()
        )
        output_text = result.raw_response or ""

        try:
            prompt = prompt_template.format(
                input=input_text,
                output=output_text,
                expected=expected_text,
            )
        except KeyError as e:
            prompt = (
                f"{prompt_template}\n\nInput:\n{input_text}\n\n"
                f"Model output:\n{output_text}\n\nExpected:\n{expected_text}"
            )

        assessment = {}
        error_note = ""
        raw_judge_response = ""

        if judge_model:
            try:
                llm_result = call_llm(
                    judge_model,
                    prompt,
                    temperature=0.0,
                    response_format_json=True,
                )
                raw_judge_response = llm_result.get("text", "")
                if llm_result.get("error"):
                    error_note = llm_result["error"]
                else:
                    raw = llm_result.get("parsed") or {}
                    if not raw and llm_result.get("text"):
                        # Try parsing manually if response_format_json didn't parse it
                        try:
                            raw = json.loads(llm_result["text"])
                        except (json.JSONDecodeError, ValueError):
                            # Extract JSON block from prose response
                            m = re.search(r'\{.*\}', llm_result["text"], re.DOTALL)
                            raw = json.loads(m.group()) if m else {}

                    # Coerce values to declared types
                    for field in output_fields:
                        fname = field.get("name")
                        ftype = field.get("type", "text")
                        val = raw.get(fname)
                        if val is None:
                            assessment[fname] = None
                        elif ftype == "boolean":
                            if isinstance(val, bool):
                                assessment[fname] = val
                            else:
                                assessment[fname] = str(val).lower() in ("true", "yes", "1")
                        elif ftype == "integer":
                            try:
                                assessment[fname] = int(val)
                            except (ValueError, TypeError):
                                assessment[fname] = None
                        else:
                            assessment[fname] = str(val)
            except Exception as e:
                error_note = str(e)

        EvaluationResult.objects.update_or_create(
            evaluation_run=eval_run,
            test_run_result=result,
            defaults={
                "assessor_type": AssessorType.AI,
                "assessor_id": model_id,
                "assessment": assessment,
                "notes": error_note,
                "raw_judge_response": raw_judge_response,
            },
        )

    eval_run.status = EvalRunStatus.COMPLETED
    eval_run.completed_at = timezone.now()
    eval_run.save(update_fields=["status", "completed_at"])
