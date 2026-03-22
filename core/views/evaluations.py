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
from core.services.scorer import score_field_match, score_result


# ---------------------------------------------------------------------------
# Evaluation config
# ---------------------------------------------------------------------------

class EvaluationConfigCreateView(LoginRequiredMixin, View):
    """Create an evaluation config for a test case."""

    template_name = "core/evaluationconfig_form.html"

    def _get_test_case(self, test_case_id):
        return get_object_or_404(TestCase, pk=test_case_id)

    def _base_context(self, test_case, form_data=None, editing=None):
        latest_version = test_case.versions.order_by("-version_number").first()
        output_columns = latest_version.output_columns if latest_version else []
        return {
            "test_case": test_case,
            "eval_types": EvalType.choices,
            "model_configs": ModelConfig.objects.filter(is_active=True).order_by("name"),
            "form_data": form_data or {},
            "output_columns": output_columns,
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
        latest_version = config.test_case.versions.order_by("-version_number").first()
        output_columns = latest_version.output_columns if latest_version else []
        return {
            "test_case": config.test_case,
            "eval_types": EvalType.choices,
            "model_configs": ModelConfig.objects.filter(is_active=True).order_by("name"),
            "editing": config,
            "output_columns": output_columns,
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
        output_columns = test_run.test_case_version.output_columns or []
        return {
            "test_run": test_run,
            "configs": configs,
            "eval_types": EvalType.choices,
            "model_configs": ModelConfig.objects.filter(is_active=True).order_by("name"),
            "output_columns": output_columns,
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

        if config.eval_type == EvalType.FIELD_MATCH:
            t = threading.Thread(target=_run_field_match_eval, args=(eval_run.pk,), daemon=True)
            t.start()
            messages.success(request, "Field match evaluation started.")
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
            ss_col = check.get("expected_output_column", "")
            ss_badge = (
                f' <span style="font-size:0.7rem; background:rgba(124,196,255,0.15); '
                f'color:var(--accent); border-radius:3px; padding:0.1rem 0.35rem; '
                f'margin-left:0.3rem;">sens/spec ← {ss_col}</span>'
                if check.get("sens_spec") and ss_col else ""
            )
            if ctype == "contains_phrase":
                lines.append(f'<strong>{name}</strong>: response contains "{phrase}" ({case}){ss_badge}')
            elif ctype == "json_key_contains":
                lines.append(f'<strong>{name}</strong>: JSON field <code>{path}</code> contains "{phrase}" ({case}){ss_badge}')
            elif ctype == "json_key_equals":
                lines.append(f'<strong>{name}</strong>: JSON field <code>{path}</code> equals "{phrase}" ({case}){ss_badge}')
            else:
                lines.append(f'<strong>{name}</strong>: {ctype}{ss_badge}')

    elif config.eval_type == EvalType.HUMAN:
        for field in criteria.get("review_fields", []):
            label = field.get("label") or field.get("name", "unnamed")
            ftype = field.get("type", "text")
            ss_col = field.get("expected_output_column", "")
            ss_badge = (
                f' <span style="font-size:0.7rem; background:rgba(124,196,255,0.15); '
                f'color:var(--accent); border-radius:3px; padding:0.1rem 0.35rem; '
                f'margin-left:0.3rem;">sens/spec ← {ss_col}</span>'
                if field.get("sens_spec") and ss_col else ""
            )
            if ftype == "boolean":
                lines.append(f'<strong>{label}</strong> — yes / no{ss_badge}')
            elif ftype == "integer":
                lo, hi = field.get("min", 0), field.get("max", 10)
                lines.append(f'<strong>{label}</strong> — integer {lo}–{hi}{ss_badge}')
            else:
                lines.append(f'<strong>{label}</strong> — free text{ss_badge}')

    elif config.eval_type == EvalType.AI_JUDGE:
        for field in criteria.get("output_fields", []):
            label = field.get("label") or field.get("name", "unnamed")
            ftype = field.get("type", "text")
            ss_col = field.get("expected_output_column", "")
            ss_badge = (
                f' <span style="font-size:0.7rem; background:rgba(124,196,255,0.15); '
                f'color:var(--accent); border-radius:3px; padding:0.1rem 0.35rem; '
                f'margin-left:0.3rem;">sens/spec ← {ss_col}</span>'
                if field.get("sens_spec") and ss_col else ""
            )
            if ftype == "boolean":
                lines.append(f'<strong>{label}</strong> — boolean (used for accuracy){ss_badge}')
            elif ftype == "integer":
                lo, hi = field.get("min", 0), field.get("max", 10)
                lines.append(f'<strong>{label}</strong> — integer {lo}–{hi}{ss_badge}')
            else:
                lines.append(f'<strong>{label}</strong> — free text{ss_badge}')

    elif config.eval_type == EvalType.FIELD_MATCH:
        cs = criteria.get("case_sensitive", False)
        cs_label = "case-sensitive" if cs else "case-insensitive"
        for field in criteria.get("fields", []):
            name = field.get("name", "unnamed")
            match_type = field.get("match_type", "exact")
            ss_col = field.get("expected_output_column", "")
            ss_badge = (
                f' <span style="font-size:0.7rem; background:rgba(124,196,255,0.15); '
                f'color:var(--accent); border-radius:3px; padding:0.1rem 0.35rem; '
                f'margin-left:0.3rem;">sens/spec ← {ss_col}</span>'
                if field.get("sens_spec") and ss_col else ""
            )
            if match_type == "exact":
                lines.append(f'<strong>{name}</strong>: exact match ({cs_label}){ss_badge}')
            else:
                lines.append(f'<strong>{name}</strong>: LLM-judged match{ss_badge}')

    return [mark_safe(line) for line in lines]


def _ground_truth_positive(value) -> bool | None:
    """
    Interpret an expected_output_fields value as a boolean ground truth.

    Returns True (positive), False (negative), or None if the value is
    ambiguous / cannot be interpreted.

    Handles:
      - Python bool: True / False
      - Numbers: 0 → False (negative); any non-zero number → True (positive)
        e.g. 1, 2, 0.5, -1 are all positive; only 0 / 0.0 is negative
      - Strings: 'true','yes','1','positive','present' → True
                 'false','no','0','negative','absent','' → False
      - None / empty → False (absent = negative)
      - Any other non-empty string → True (value present = positive)
        e.g. 'diabetes', 'E11.9', '2' are all positive
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in ("true", "yes", "1", "positive", "present"):
        return True
    if s in ("false", "no", "0", "negative", "absent", ""):
        return False
    # Non-empty string that doesn't match a negative keyword → positive
    return True


def _get_sens_spec_checks(eval_run) -> list[dict]:
    """
    Return the list of checks/fields from scoring_criteria that have
    both sens_spec=true and an expected_output_column set.
    Works across all eval types.
    """
    criteria = eval_run.evaluation_config.scoring_criteria or {}
    eval_type = eval_run.evaluation_config.eval_type

    if eval_type == EvalType.KEYWORD_MATCH:
        items = criteria.get("checks", [])
    elif eval_type == EvalType.FIELD_MATCH:
        items = criteria.get("fields", [])
    elif eval_type == EvalType.AI_JUDGE:
        items = criteria.get("output_fields", [])
    elif eval_type == EvalType.HUMAN:
        items = criteria.get("review_fields", [])
    else:
        items = []

    return [
        item for item in items
        if item.get("sens_spec") and item.get("expected_output_column")
    ]


def compute_sens_spec(eval_run) -> list[dict] | None:
    """
    Compute sensitivity, specificity, PPV, and NPV for each check/field
    that has been flagged with sens_spec=true and expected_output_column.

    Returns a list of per-check dicts, or None if no flagged checks exist.

    Each dict contains:
      name, expected_output_column,
      tp, fp, tn, fn, total,
      sensitivity, specificity, ppv, npv
    """
    flagged = _get_sens_spec_checks(eval_run)
    if not flagged:
        return None

    results = list(
        eval_run.results.select_related(
            "test_run_result__test_case_row"
        ).all()
    )
    if not results:
        return None

    stats: dict[str, dict] = {
        item["name"]: {"tp": 0, "fp": 0, "tn": 0, "fn": 0, "item": item}
        for item in flagged
    }

    for er in results:
        row = er.test_run_result.test_case_row
        expected_fields = row.expected_output_fields or {}
        assessment = er.assessment or {}

        for item in flagged:
            name = item["name"]
            col = item["expected_output_column"]
            raw_ground_truth = expected_fields.get(col)
            ground_truth = _ground_truth_positive(raw_ground_truth)
            eval_passed = assessment.get(name)

            # Only classify when eval result is boolean
            if not isinstance(eval_passed, bool):
                continue

            s = stats[name]
            if ground_truth and eval_passed:
                s["tp"] += 1
            elif not ground_truth and eval_passed:
                s["fp"] += 1
            elif not ground_truth and not eval_passed:
                s["tn"] += 1
            else:  # ground_truth and not eval_passed
                s["fn"] += 1

    output = []
    for name, s in stats.items():
        tp, fp, tn, fn = s["tp"], s["fp"], s["tn"], s["fn"]
        total = tp + fp + tn + fn
        if total == 0:
            continue

        sensitivity = round(tp / (tp + fn), 3) if (tp + fn) > 0 else None
        specificity = round(tn / (tn + fp), 3) if (tn + fp) > 0 else None
        ppv = round(tp / (tp + fp), 3) if (tp + fp) > 0 else None
        npv = round(tn / (tn + fn), 3) if (tn + fn) > 0 else None

        output.append({
            "name": name,
            "expected_output_column": s["item"]["expected_output_column"],
            "tp": tp, "fp": fp, "tn": tn, "fn": fn, "total": total,
            "sensitivity": sensitivity,
            "specificity": specificity,
            "ppv": ppv,
            "npv": npv,
        })

    return output if output else None


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
    # For field_match every declared field produces a boolean in the assessment
    elif eval_run.evaluation_config.eval_type == EvalType.FIELD_MATCH:
        bool_fields = [
            f["name"]
            for f in eval_run.evaluation_config.scoring_criteria.get("fields", [])
        ]

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
        ctx["sens_spec"] = compute_sens_spec(self.object)
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
        output_text = re.sub(r'^```json\s*', '', result.raw_response or "", flags=re.IGNORECASE)
        output_text = re.sub(r'\s*```$', '', output_text)

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
                "judge_prompt_sent": prompt,
                "raw_judge_response": raw_judge_response,
            },
        )

    eval_run.status = EvalRunStatus.COMPLETED
    eval_run.completed_at = timezone.now()
    eval_run.save(update_fields=["status", "completed_at"])


# ---------------------------------------------------------------------------
# Background task: field match evaluation
# ---------------------------------------------------------------------------

def _run_field_match_eval(eval_run_pk):
    """
    Compare parsed JSON response fields to expected_output_fields from the CSV.

    Fields with match_type="exact" are compared directly (case-insensitive by
    default, or case-sensitive when scoring_criteria["case_sensitive"] is True).
    Fields with match_type="llm_judge" are sent to the judge LLM with a prompt
    asking whether the actual value is semantically equivalent to the expected
    value; the LLM must return JSON {"<field_name>": true|false}.
    """
    from core.models import EvaluationRun
    from core.services.llm_client import call_llm
    from core.services.scorer import _parse_response_json

    eval_run = EvaluationRun.objects.select_related(
        "evaluation_config__judge_model_config",
        "test_run",
    ).get(pk=eval_run_pk)

    eval_run.status = EvalRunStatus.IN_PROGRESS
    eval_run.save(update_fields=["status"])

    config = eval_run.evaluation_config
    criteria = config.scoring_criteria or {}
    fields_config = criteria.get("fields", [])
    case_sensitive = criteria.get("case_sensitive", False)
    judge_model = config.judge_model_config
    judge_prompt_template = config.judge_prompt_template or ""
    model_id = judge_model.model_name if judge_model else "field_match"

    llm_fields = [f for f in fields_config if f.get("match_type") == "llm_judge"]

    results = TestRunResult.objects.filter(
        test_run=eval_run.test_run
    ).select_related("test_case_row")

    for result in results:
        expected = result.test_case_row.expected_output_fields or {}

        assessment = score_field_match(result, expected, fields_config, case_sensitive)
        error_note = ""
        raw_judge_response = ""

        if llm_fields and judge_model:
            parsed_response = None
            try:
                parsed_response = _parse_response_json(result)
            except Exception:
                pass

            for field in llm_fields:
                fname = field.get("name", "")
                actual_val = None
                if parsed_response and isinstance(parsed_response, dict):
                    actual_val = parsed_response.get(fname)
                expected_val = expected.get(fname)

                try:
                    if judge_prompt_template:
                        prompt = judge_prompt_template.format(
                            field=fname,
                            actual=actual_val,
                            expected=expected_val,
                        )
                    else:
                        prompt = (
                            f'Does the actual value semantically match the expected value '
                            f'for the field "{fname}"?\n\n'
                            f'Expected: {expected_val}\n'
                            f'Actual: {actual_val}\n\n'
                            f'Respond with JSON only: {{"{fname}": true}} or {{"{fname}": false}}'
                        )
                    llm_result = call_llm(
                        judge_model,
                        prompt,
                        temperature=0.0,
                        response_format_json=True,
                    )
                    raw_judge_response += llm_result.get("text", "")
                    if llm_result.get("error"):
                        error_note += llm_result["error"] + "\n"
                        assessment[fname] = None
                    else:
                        raw = llm_result.get("parsed") or {}
                        if not raw and llm_result.get("text"):
                            try:
                                raw = json.loads(llm_result["text"])
                            except (json.JSONDecodeError, ValueError):
                                m = re.search(r'\{.*\}', llm_result["text"], re.DOTALL)
                                raw = json.loads(m.group()) if m else {}
                        val = raw.get(fname)
                        if isinstance(val, bool):
                            assessment[fname] = val
                        else:
                            assessment[fname] = str(val).lower() in ("true", "yes", "1") if val is not None else None
                except Exception as e:
                    error_note += str(e) + "\n"
                    assessment[fname] = None

        EvaluationResult.objects.update_or_create(
            evaluation_run=eval_run,
            test_run_result=result,
            defaults={
                "assessor_type": AssessorType.AI,
                "assessor_id": model_id,
                "assessment": assessment,
                "notes": error_note.strip(),
                "raw_judge_response": raw_judge_response,
            },
        )

    eval_run.status = EvalRunStatus.COMPLETED
    eval_run.completed_at = timezone.now()
    eval_run.save(update_fields=["status", "completed_at"])
