"""
Celery tasks for async test run execution.

Processes rows concurrently up to model_config.max_concurrency, respecting
the per-model rate limit (rate_limit_rpm).
"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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


def _call_row(run_id, limiter, model_config, prompt_snapshot, temperature, response_format_json, row):
    """
    Worker function: check for cancellation, throttle, call LLM.

    Returns (row, result_dict) where result_dict is None if the run was
    cancelled before this row's LLM call was dispatched.
    """
    check = TestRun.objects.only("status").get(id=run_id)
    if check.status == RunStatus.CANCELLED:
        return row, None

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
    result["_prompt"] = prompt
    return row, result


@shared_task(bind=True, max_retries=3)
def execute_test_run(self, run_id: str) -> None:
    """
    Execute a test run: for each row, build prompt, call LLM, store result.

    Up to model_config.max_concurrency rows are processed in parallel, still
    respecting the model's rate_limit_rpm throttle.
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

    start_wall = time.monotonic()

    try:
        version = run.test_case_version
        template = run.prompt_template
        model_config = run.model_config
        prompt_snapshot = run.prompt_snapshot or template.template_text
        response_format_json = template.response_format == "json"
        temperature = run.temperature_override if run.temperature_override is not None else model_config.default_temperature
        max_concurrency = max(1, model_config.max_concurrency or 1)

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

        limiter = get_limiter(str(model_config.id), model_config.rate_limit_rpm, max_concurrency)

        total_input = 0
        total_output = 0
        cancelled = False

        with ThreadPoolExecutor(max_workers=max_concurrency) as pool:
            futures = {
                pool.submit(
                    _call_row,
                    run_id,
                    limiter,
                    model_config,
                    prompt_snapshot,
                    temperature,
                    response_format_json,
                    row,
                ): row
                for row in rows
            }

            for future in as_completed(futures):
                row, result = future.result()

                if result is None:
                    # Worker detected the run was cancelled before dispatching.
                    cancelled = True
                    continue

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
                            "prompt_sent": result.get("_prompt", ""),
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
                    run.save(update_fields=[
                        "rows_completed", "rows_failed",
                        "total_input_tokens", "total_output_tokens",
                    ])

        if not cancelled:
            run.status = RunStatus.COMPLETED
            run.save(update_fields=["status"])

    except Exception as exc:
        run.status = RunStatus.FAILED
        run.error_message = str(exc)
        run.save(update_fields=["status", "error_message"])
        raise

    finally:
        run.completed_at = timezone.now()
        run.total_duration_seconds = time.monotonic() - start_wall
        run.save(update_fields=["completed_at", "total_duration_seconds"])
