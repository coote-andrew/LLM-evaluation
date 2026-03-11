"""
Keyword / phrase matching scorer.

Evaluates a TestRunResult against the scoring_criteria from an EvaluationConfig
of type keyword_match. Returns a dict of {check_name: bool/str}.
"""

from __future__ import annotations

import json
import re
from typing import Any


def _get_json_path(data: Any, path: str) -> Any:
    """Traverse a dot-separated path into a parsed JSON structure.

    Returns the whole structure unchanged when path is empty.
    """
    if not path:
        return data
    parts = path.split(".")
    current = data
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return current


def _json_key_exists(data: Any, key: str, case_sensitive: bool) -> bool:
    """Return True if *key* appears as a key anywhere in *data* (recursive)."""
    needle = key if case_sensitive else key.lower()
    if isinstance(data, dict):
        for k, v in data.items():
            k_cmp = k if case_sensitive else k.lower()
            if k_cmp == needle:
                return True
            if _json_key_exists(v, key, case_sensitive):
                return True
    elif isinstance(data, list):
        return any(_json_key_exists(item, key, case_sensitive) for item in data)
    return False


def _json_value_exists(data: Any, phrase: str, case_sensitive: bool) -> bool:
    """Return True if *phrase* appears in any scalar value anywhere in *data* (recursive)."""
    needle = phrase if case_sensitive else phrase.lower()
    if isinstance(data, dict):
        return any(_json_value_exists(v, phrase, case_sensitive) for v in data.values())
    elif isinstance(data, list):
        return any(_json_value_exists(item, phrase, case_sensitive) for item in data)
    else:
        haystack = str(data) if case_sensitive else str(data).lower()
        return needle in haystack


def run_keyword_checks(result_text: str, parsed_json: Any, checks: list[dict]) -> dict[str, Any]:
    """
    Run a list of keyword checks against a response.

    Supported check types:
      - contains_phrase:   phrase found in full response text (or at a json_path target)
      - json_key_exists:   a key with the given name exists anywhere in the JSON
      - json_value_exists: a value containing the phrase exists anywhere in the JSON
      - json_key_contains: value at json_path contains phrase
      - json_key_equals:   value at json_path equals expected_value
    """
    outcomes: dict[str, Any] = {}

    for check in checks:
        name = check.get("name", "unnamed")
        check_type = check.get("type", "")
        case_sensitive = check.get("case_sensitive", False)

        try:
            if check_type == "contains_phrase":
                phrase = check.get("phrase", "")
                target = check.get("target", "full_response")
                if target == "full_response":
                    haystack = result_text
                else:
                    haystack = str(_get_json_path(parsed_json, target) or "")
                if not case_sensitive:
                    outcomes[name] = phrase.lower() in haystack.lower()
                else:
                    outcomes[name] = phrase in haystack

            elif check_type == "json_key_exists":
                key = check.get("key", "")
                if parsed_json is None:
                    outcomes[name] = False
                else:
                    outcomes[name] = _json_key_exists(parsed_json, key, case_sensitive)

            elif check_type == "json_value_exists":
                phrase = check.get("phrase", "")
                if parsed_json is None:
                    outcomes[name] = False
                else:
                    outcomes[name] = _json_value_exists(parsed_json, phrase, case_sensitive)

            elif check_type == "json_key_contains":
                phrase = check.get("phrase", "")
                path = check.get("json_path", "")
                value = str(_get_json_path(parsed_json, path) or "")
                if not case_sensitive:
                    outcomes[name] = phrase.lower() in value.lower()
                else:
                    outcomes[name] = phrase in value

            elif check_type == "json_key_equals":
                path = check.get("json_path", "")
                expected = check.get("expected_value")
                value = _get_json_path(parsed_json, path)
                if not case_sensitive and isinstance(value, str) and isinstance(expected, str):
                    outcomes[name] = value.lower() == expected.lower()
                else:
                    outcomes[name] = value == expected

            else:
                outcomes[name] = f"unknown check type: {check_type}"

        except Exception as e:
            outcomes[name] = f"error: {e}"

    return outcomes


def score_result(test_run_result, scoring_criteria: dict) -> dict[str, Any]:
    """
    Apply keyword scoring_criteria to a single TestRunResult.
    Returns the assessment dict to store on EvaluationResult.
    """
    checks = scoring_criteria.get("checks", [])
    raw_text = test_run_result.raw_response or ""

    parsed = test_run_result.response_parsed
    if parsed is None and raw_text:
        try:
            cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", raw_text.strip(), count=1)
            cleaned = re.sub(r"\n?```$", "", cleaned.rstrip(), count=1)
            parsed = json.loads(cleaned)
        except (json.JSONDecodeError, ValueError):
            parsed = None

    return run_keyword_checks(raw_text, parsed, checks)


def _parse_response_json(test_run_result) -> Any:
    """Extract parsed JSON from a TestRunResult, trying response_parsed then raw_response."""
    parsed = test_run_result.response_parsed
    if parsed is not None:
        return parsed
    raw_text = test_run_result.raw_response or ""
    if raw_text:
        try:
            cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", raw_text.strip(), count=1)
            cleaned = re.sub(r"\n?```$", "", cleaned.rstrip(), count=1)
            return json.loads(cleaned)
        except (json.JSONDecodeError, ValueError):
            pass
    return None


def score_field_match(
    test_run_result,
    expected_output_fields: dict,
    fields_config: list[dict],
    case_sensitive: bool = False,
) -> dict[str, Any]:
    """
    Compare parsed JSON response fields against expected_output_fields.

    Each entry in fields_config is:
      {"name": "output_data1", "match_type": "exact"}
    where match_type is "exact" (default) — LLM-judged fields are handled
    separately in the background task since they require an LLM call.

    Returns a dict of {field_name: bool | "error: ..."}.
    """
    parsed = _parse_response_json(test_run_result)
    outcomes: dict[str, Any] = {}

    for field in fields_config:
        name = field.get("name", "")
        match_type = field.get("match_type", "exact")

        if match_type != "exact":
            # LLM-judged fields are handled by the background task; skip here.
            continue

        actual = None
        if parsed is not None and isinstance(parsed, dict):
            actual = parsed.get(name)

        expected = expected_output_fields.get(name)

        if actual is None and expected is None:
            outcomes[name] = True
            continue

        if actual is None or expected is None:
            outcomes[name] = False
            continue

        actual_str = str(actual)
        expected_str = str(expected)

        if case_sensitive:
            outcomes[name] = actual_str == expected_str
        else:
            outcomes[name] = actual_str.strip().lower() == expected_str.strip().lower()

    return outcomes
