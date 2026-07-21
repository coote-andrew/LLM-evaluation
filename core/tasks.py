"""
Celery tasks for async test-run and evaluation execution.

LLM worker threads must not touch the Django ORM (avoids holding a Postgres
connection open for the duration of each HTTP call). Cancel state is shared
via ``threading.Event``; result writes stay on the main task thread.
"""

from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait

from celery import shared_task
from django.conf import settings
from django.db import close_old_connections, connections, transaction
from django.utils import timezone

from core.models import (
    AssessorType,
    EvalRunStatus,
    EvaluationResult,
    EvaluationRun,
    ResultStatus,
    RunStatus,
    TestCaseAttachment,
    TestCaseRow,
    TestRun,
    TestRunResult,
)
from core.services.llm_client import call_llm
from core.services.prompt_builder import build_prompt
from core.services.rate_limiter import get_limiter
from core.services.scorer import score_field_match, score_result


def _effective_max_concurrency(configured: int | None) -> int:
    """Clamp model concurrency to at least 1 and at most settings budget."""
    cap = max(1, int(getattr(settings, "MAX_MODEL_CONCURRENCY", 16)))
    return max(1, min(int(configured or 1), cap))


def _call_row(
    cancel_event,
    limiter,
    model_config,
    prompt_snapshot,
    temperature,
    response_format_json,
    row,
    attachments=None,
):
    """
    Worker function: check cancel flag, throttle, call LLM.

    Must not use the Django ORM — pool threads must not open Postgres sessions
    that would stay open for the LLM HTTP wait.
    """
    if cancel_event.is_set():
        return row, None

    limiter.wait_if_needed()

    if cancel_event.is_set():
        return row, None

    prompt = build_prompt(prompt_snapshot, row.input_fields)
    try:
        result = call_llm(
            model_config,
            prompt,
            temperature=temperature,
            response_format_json=response_format_json,
            attachments=attachments or [],
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


def _row_attachments(row, attachments_by_path):
    """Materialise a row's attachment bytes on the task thread, before fan-out."""
    attachments = []
    seen_paths = set()
    for value in (row.file_fields or {}).values():
        paths = value if isinstance(value, list) else [value]
        for path in paths:
            if path in seen_paths:
                continue
            seen_paths.add(path)
            attachment = attachments_by_path.get(path)
            if not attachment:
                raise ValueError(f"Referenced attachment '{path}' is unavailable.")
            with attachment.file.open("rb") as uploaded_file:
                attachments.append({
                    "name": attachment.relative_path,
                    "mime_type": attachment.mime_type,
                    "content": uploaded_file.read(),
                    "sha256": attachment.sha256,
                })
    return attachments


@shared_task(bind=True, max_retries=3)
def execute_test_run(self, run_id: str) -> None:
    """
    Execute a test run: for each row, build prompt, call LLM, store result.

    Up to model_config.max_concurrency rows are processed in parallel, still
    respecting the model's rate_limit_rpm throttle.
    """
    close_old_connections()
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
    cancel_event = threading.Event()

    try:
        version = run.test_case_version
        template = run.prompt_template
        model_config = run.model_config
        prompt_snapshot = run.prompt_snapshot or template.template_text
        response_format_json = template.response_format == "json"
        temperature = (
            run.temperature_override
            if run.temperature_override is not None
            else model_config.default_temperature
        )
        max_concurrency = _effective_max_concurrency(model_config.max_concurrency)

        rows_qs = TestCaseRow.objects.filter(version=version).order_by("row_number")
        if run.row_limit:
            rows_qs = rows_qs[: run.row_limit]
        if run.skip_rows_from_parent and run.parent_run_id:
            completed_row_ids = set(
                TestRunResult.objects.filter(test_run=run.parent_run).values_list(
                    "test_case_row_id", flat=True
                )
            )
            rows_qs = rows_qs.exclude(id__in=completed_row_ids)

        rows = list(rows_qs)
        attachments_by_path = {
            attachment.relative_path: attachment
            for attachment in TestCaseAttachment.objects.filter(version=version)
        }
        run.rows_total = len(rows)
        run.save(update_fields=["rows_total"])

        limiter = get_limiter(str(model_config.id), model_config.rate_limit_rpm, max_concurrency)

        total_input = 0
        total_output = 0
        cancelled = False

        # Submit at most max_concurrency at a time so cancel (checked on the
        # main thread) can stop further dispatch without ORM in workers.
        rows_iter = iter(rows)
        pending = {}

        def _submit_next(pool):
            try:
                row = next(rows_iter)
            except StopIteration:
                return False
            attachments = _row_attachments(row, attachments_by_path)
            future = pool.submit(
                _call_row,
                cancel_event,
                limiter,
                model_config,
                prompt_snapshot,
                temperature,
                response_format_json,
                row,
                attachments,
            )
            pending[future] = row
            return True

        with ThreadPoolExecutor(max_workers=max_concurrency) as pool:
            for _ in range(min(max_concurrency, len(rows))):
                if not _submit_next(pool):
                    break

            while pending:
                done, _ = wait(pending.keys(), return_when=FIRST_COMPLETED)
                for future in done:
                    pending.pop(future)
                    row, result = future.result()

                    run.refresh_from_db(fields=["status"])
                    if run.status == RunStatus.CANCELLED:
                        cancel_event.set()
                        cancelled = True

                    if result is None:
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
                                "attachment_metadata": result.get("attachment_metadata", []),
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
                        run.save(
                            update_fields=[
                                "rows_completed",
                                "rows_failed",
                                "total_input_tokens",
                                "total_output_tokens",
                            ]
                        )

                if not cancelled:
                    while len(pending) < max_concurrency:
                        if not _submit_next(pool):
                            break


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
        close_old_connections()
        connections.close_all()


# ---------------------------------------------------------------------------
# Evaluation tasks
# ---------------------------------------------------------------------------


def _judge_one_result(result, judge_model, prompt_template, output_fields, limiter):
    """Worker: call judge LLM for a single test result row (no ORM)."""
    input_text = "\n".join(
        f"{k}: {v}" for k, v in (result.test_case_row.input_fields or {}).items()
    )
    expected_text = "\n".join(
        f"{k}: {v}" for k, v in (result.test_case_row.expected_output_fields or {}).items()
    )
    output_text = re.sub(r"^```json\s*", "", result.raw_response or "", flags=re.IGNORECASE)
    output_text = re.sub(r"\s*```$", "", output_text)

    try:
        prompt = prompt_template.format(
            input=input_text,
            output=output_text,
            expected=expected_text,
        )
    except KeyError:
        prompt = (
            f"{prompt_template}\n\nInput:\n{input_text}\n\n"
            f"Model output:\n{output_text}\n\nExpected:\n{expected_text}"
        )

    assessment = {}
    error_note = ""
    raw_judge_response = ""

    if judge_model:
        try:
            limiter.wait_if_needed()
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
                    try:
                        raw = json.loads(llm_result["text"])
                    except (json.JSONDecodeError, ValueError):
                        m = re.search(r"\{.*\}", llm_result["text"], re.DOTALL)
                        raw = json.loads(m.group()) if m else {}

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

    return result, assessment, error_note, prompt, raw_judge_response


def _field_match_one_result(
    result, fields_config, case_sensitive, llm_fields, judge_model, judge_prompt_template, limiter
):
    """Worker: score one result row; LLM calls only (no ORM)."""
    from core.services.scorer import _parse_response_json

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
                        f"Expected: {expected_val}\n"
                        f"Actual: {actual_val}\n\n"
                        f'Respond with JSON only: {{"{fname}": true}} or {{"{fname}": false}}'
                    )
                limiter.wait_if_needed()
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
                            m = re.search(r"\{.*\}", llm_result["text"], re.DOTALL)
                            raw = json.loads(m.group()) if m else {}
                    val = raw.get(fname)
                    if isinstance(val, bool):
                        assessment[fname] = val
                    else:
                        assessment[fname] = (
                            str(val).lower() in ("true", "yes", "1") if val is not None else None
                        )
            except Exception as e:
                error_note += str(e) + "\n"
                assessment[fname] = None

    return result, assessment, error_note.strip(), raw_judge_response


@shared_task
def execute_keyword_eval(eval_run_pk) -> None:
    """Run keyword matching for all results in an eval run."""
    close_old_connections()
    try:
        eval_run = EvaluationRun.objects.select_related("evaluation_config", "test_run").get(
            pk=eval_run_pk
        )
        eval_run.status = EvalRunStatus.IN_PROGRESS
        eval_run.save(update_fields=["status"])

        criteria = eval_run.evaluation_config.scoring_criteria
        results = TestRunResult.objects.filter(test_run=eval_run.test_run).select_related(
            "test_case_row"
        )

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
    finally:
        close_old_connections()
        connections.close_all()


@shared_task
def execute_ai_judge_eval(eval_run_pk) -> None:
    """Call a judge LLM for every result row and store its assessment."""
    close_old_connections()
    try:
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
        max_concurrency = _effective_max_concurrency(
            judge_model.max_concurrency if judge_model else 1
        )

        results = list(
            TestRunResult.objects.filter(test_run=eval_run.test_run).select_related("test_case_row")
        )

        limiter = get_limiter(
            str(judge_model.id) if judge_model else "noop",
            judge_model.rate_limit_rpm if judge_model else 60,
            max_concurrency,
        )

        with ThreadPoolExecutor(max_workers=max_concurrency) as pool:
            futures = {
                pool.submit(
                    _judge_one_result, result, judge_model, prompt_template, output_fields, limiter
                ): result
                for result in results
            }

            for future in as_completed(futures):
                result, assessment, error_note, prompt, raw_judge_response = future.result()
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
    finally:
        close_old_connections()
        connections.close_all()


@shared_task
def execute_field_match_eval(eval_run_pk) -> None:
    """Compare response fields to expected_output_fields (exact and/or LLM judge)."""
    close_old_connections()
    try:
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
        max_concurrency = _effective_max_concurrency(
            judge_model.max_concurrency if judge_model else 1
        )

        llm_fields = [f for f in fields_config if f.get("match_type") == "llm_judge"]

        results = list(
            TestRunResult.objects.filter(test_run=eval_run.test_run).select_related("test_case_row")
        )

        limiter = get_limiter(
            str(judge_model.id) if judge_model else "noop",
            judge_model.rate_limit_rpm if judge_model else 60,
            max_concurrency,
        )

        with ThreadPoolExecutor(max_workers=max_concurrency) as pool:
            futures = {
                pool.submit(
                    _field_match_one_result,
                    result,
                    fields_config,
                    case_sensitive,
                    llm_fields,
                    judge_model,
                    judge_prompt_template,
                    limiter,
                ): result
                for result in results
            }

            for future in as_completed(futures):
                result, assessment, error_note, raw_judge_response = future.result()
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
    finally:
        close_old_connections()
        connections.close_all()


@shared_task
def execute_python_eval(eval_run_pk) -> None:
    """Execute a user-supplied Python script against every result row."""
    from core.services.tool_runner import run_python_eval

    close_old_connections()
    try:
        eval_run = EvaluationRun.objects.select_related(
            "evaluation_config",
            "test_run",
        ).get(pk=eval_run_pk)

        eval_run.status = EvalRunStatus.IN_PROGRESS
        eval_run.save(update_fields=["status"])

        script = eval_run.evaluation_config.scoring_criteria.get("script", "")

        results = TestRunResult.objects.filter(test_run=eval_run.test_run).select_related(
            "test_case_row"
        )

        for result in results:
            row_locals = {
                "input_fields": result.test_case_row.input_fields or {},
                "expected_output_fields": result.test_case_row.expected_output_fields or {},
                "raw_response": result.raw_response or "",
                "response_parsed": result.response_parsed,
            }

            outcome = run_python_eval(script, row_locals)

            if isinstance(outcome, str):
                assessment = {}
                error_note = outcome
            else:
                assessment = outcome
                error_note = ""

            EvaluationResult.objects.update_or_create(
                evaluation_run=eval_run,
                test_run_result=result,
                defaults={
                    "assessor_type": AssessorType.AI,
                    "assessor_id": "python_eval",
                    "assessment": assessment,
                    "notes": error_note,
                },
            )

        eval_run.status = EvalRunStatus.COMPLETED
        eval_run.completed_at = timezone.now()
        eval_run.save(update_fields=["status", "completed_at"])
    finally:
        close_old_connections()
        connections.close_all()
