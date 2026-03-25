"""
Celery tasks for async test run execution.

Processes rows one by one, respecting rate limits.
"""

import time
from uuid import UUID

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from core.models import (
    ModelConfig,
    PromptTemplate,
    RunStatus,
    ResultStatus,
    TestCaseRow,
    TestCaseVersion,
    TestRun,
    TestRunResult,
)
from core.services.llm_client import call_llm
from core.services.prompt_builder import build_prompt
from core.services.rate_limiter import get_limiter


@shared_task(bind=True, max_retries=3)
def execute_test_run(self, run_id: str) -> None:
    """
    Execute a test run: for each row, build prompt, call LLM, store result.
    """
    run = TestRun.objects.select_related(
        "test_case_version",
        "prompt_template",
        "model_config",
    ).get(id=run_id)

    if run.status != RunStatus.PENDING:
        return

    run.status = RunStatus.RUNNING
    run.started_at = timezone.now()
    run.save(update_fields=["status", "started_at"])

    version = run.test_case_version
    template = run.prompt_template
    model_config = run.model_config
    prompt_snapshot = run.prompt_snapshot or template.template_text
    response_format_json = template.response_format == "json"
    temperature = run.temperature_override if run.temperature_override is not None else model_config.default_temperature

    # Determine which rows to process
    rows_qs = TestCaseRow.objects.filter(version=version).order_by("row_number")
    if run.row_limit:
        rows_qs = rows_qs[: run.row_limit]
    if run.skip_rows_from_parent and run.parent_run_id:
        completed_row_ids = set(
            TestRunResult.objects.filter(test_run=run.parent_run)
            .values_list("test_case_row_id", flat=True)
        )
        rows_qs = rows_qs.exclude(id__in=completed_row_ids)

    rows = list(rows_qs)
    run.rows_total = len(rows)
    run.save(update_fields=["rows_total"])

    limiter = get_limiter(str(model_config.id), model_config.rate_limit_rpm)

    total_input = 0
    total_output = 0
    start_wall = time.monotonic()

    cancelled = False
    for row in rows:
        run.refresh_from_db(fields=["status"])
        if run.status == RunStatus.CANCELLED:
            cancelled = True
            break

        limiter.wait_if_needed()

        prompt = build_prompt(prompt_snapshot, row.input_fields)
        try:
            result = call_llm(
                model_config,
                prompt,
                temperature=temperature,
                response_format_json=response_format_json,
            )
        except Exception as e:
            result = {
                "text": "",
                "input_tokens": 0,
                "output_tokens": 0,
                "latency_ms": 0,
                "error": str(e),
                "parsed": None,
            }

        status = ResultStatus.SUCCESS
        error_msg = ""
        if result.get("error"):
            status = ResultStatus.ERROR
            error_msg = result["error"]

        with transaction.atomic():
            TestRunResult.objects.update_or_create(
                test_run=run,
                test_case_row=row,
                defaults={
                    "prompt_sent": prompt,
                    "raw_response": result.get("text", ""),
                    "response_parsed": result.get("parsed"),
                    "latency_ms": result.get("latency_ms"),
                    "input_tokens": result.get("input_tokens", 0),
                    "output_tokens": result.get("output_tokens", 0),
                    "status": status,
                    "error_message": error_msg,
                },
            )
            run.rows_completed += 1
            if status == ResultStatus.ERROR:
                run.rows_failed += 1
            total_input += result.get("input_tokens", 0)
            total_output += result.get("output_tokens", 0)
            run.total_input_tokens = total_input
            run.total_output_tokens = total_output
            run.save(update_fields=["rows_completed", "rows_failed", "total_input_tokens", "total_output_tokens"])

    if not cancelled:
        run.status = RunStatus.COMPLETED
    run.completed_at = timezone.now()
    run.total_duration_seconds = time.monotonic() - start_wall
    run.save(update_fields=["status", "completed_at", "total_duration_seconds"])
