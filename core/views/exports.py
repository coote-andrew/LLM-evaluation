"""CSV export views for test runs and evaluation runs."""

import csv
import io

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.views import View

from core.models import EvaluationRun, TestRun, TestRunResult


class ExportTestRunView(LoginRequiredMixin, View):
    """Export all results from a test run as CSV."""

    def get(self, request, pk):
        test_run = get_object_or_404(
            TestRun.objects.select_related(
                "test_case_version",
                "prompt_template",
                "model_config",
            ),
            pk=pk,
        )

        results = (
            TestRunResult.objects.filter(test_run=test_run)
            .select_related("test_case_row")
            .order_by("test_case_row__row_number")
        )

        # Collect all input/output column names from the version metadata,
        # falling back to scanning rows if the version metadata is empty.
        input_cols = list(test_run.test_case_version.input_columns or [])
        output_cols = list(test_run.test_case_version.output_columns or [])
        if not input_cols or not output_cols:
            for r in results:
                for k in (r.test_case_row.input_fields or {}):
                    if k not in input_cols:
                        input_cols.append(k)
                for k in (r.test_case_row.expected_output_fields or {}):
                    if k not in output_cols:
                        output_cols.append(k)

        # Use column names as stored (already include input_/output_ prefixes).
        header = (
            ["test_run_id", "model", "prompt_template"]
            + list(input_cols)
            + list(output_cols)
            + ["prompt_sent", "raw_response", "status", "latency_ms", "input_tokens", "output_tokens"]
        )

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(header)

        run_id = str(test_run.id)[:8]
        model_name = test_run.model_config.name
        prompt_name = test_run.prompt_template.name

        for r in results:
            input_fields = r.test_case_row.input_fields or {}
            expected_fields = r.test_case_row.expected_output_fields or {}
            row = (
                [run_id, model_name, prompt_name]
                + [input_fields.get(c, "") for c in input_cols]
                + [expected_fields.get(c, "") for c in output_cols]
                + [
                    r.prompt_sent or "",
                    r.raw_response or "",
                    r.status,
                    r.latency_ms if r.latency_ms is not None else "",
                    r.input_tokens if r.input_tokens is not None else "",
                    r.output_tokens if r.output_tokens is not None else "",
                ]
            )
            writer.writerow(row)

        filename = f"run_{run_id}.csv"
        response = HttpResponse(buf.getvalue(), content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response


class ExportEvaluationRunView(LoginRequiredMixin, View):
    """Export all results from an evaluation run as CSV."""

    def get(self, request, pk):
        eval_run = get_object_or_404(
            EvaluationRun.objects.select_related(
                "evaluation_config",
                "test_run__test_case_version",
                "test_run__prompt_template",
                "test_run__model_config",
            ),
            pk=pk,
        )

        eval_results = list(
            eval_run.results.select_related(
                "test_run_result__test_case_row"
            ).order_by("test_run_result__test_case_row__row_number")
        )

        test_run = eval_run.test_run
        config = eval_run.evaluation_config

        input_cols = list(test_run.test_case_version.input_columns or [])
        output_cols = list(test_run.test_case_version.output_columns or [])
        if not input_cols or not output_cols:
            for er in eval_results:
                row_data = er.test_run_result.test_case_row
                for k in (row_data.input_fields or {}):
                    if k not in input_cols:
                        input_cols.append(k)
                for k in (row_data.expected_output_fields or {}):
                    if k not in output_cols:
                        output_cols.append(k)

        # Collect assessment field names from scoring criteria.
        criteria = config.scoring_criteria or {}
        assessment_fields = []
        for key in ("checks", "review_fields", "output_fields", "fields"):
            for f in criteria.get(key, []):
                name = f.get("name")
                if name and name not in assessment_fields:
                    assessment_fields.append(name)
        # Fallback: scan actual results for keys
        if not assessment_fields and eval_results:
            for key in (eval_results[0].assessment or {}):
                if key not in assessment_fields:
                    assessment_fields.append(key)

        # Use column names as stored (already include input_/output_ prefixes).
        header = (
            ["test_run_id", "model", "prompt_template", "eval_config", "eval_type"]
            + list(input_cols)
            + list(output_cols)
            + ["prompt_sent", "raw_response"]
            + [f"eval_{f}" for f in assessment_fields]
            + ["eval_notes", "judge_prompt_sent", "raw_judge_response"]
        )

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(header)

        run_id = str(test_run.id)[:8]
        eval_id = str(eval_run.id)[:8]
        model_name = test_run.model_config.name
        prompt_name = test_run.prompt_template.name
        eval_config_name = config.name
        eval_type = config.get_eval_type_display()

        for er in eval_results:
            trr = er.test_run_result
            row_data = trr.test_case_row
            input_fields = row_data.input_fields or {}
            expected_fields = row_data.expected_output_fields or {}
            assessment = er.assessment or {}
            row = (
                [run_id, model_name, prompt_name, eval_config_name, eval_type]
                + [input_fields.get(c, "") for c in input_cols]
                + [expected_fields.get(c, "") for c in output_cols]
                + [trr.prompt_sent or "", trr.raw_response or ""]
                + [assessment.get(f, "") for f in assessment_fields]
                + [
                    er.notes or "",
                    er.judge_prompt_sent or "",
                    er.raw_judge_response or "",
                ]
            )
            writer.writerow(row)

        filename = f"eval_{eval_id}.csv"
        response = HttpResponse(buf.getvalue(), content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response
