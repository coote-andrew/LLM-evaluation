"""
tool_runner.py

Executes arbitrary Python evaluation scripts stored in EvaluationConfig.

Security model:
- Code runs in a restricted globals dict (no builtins by default)
- A small whitelist of safe builtins is provided
- Row data (input_fields, expected_output_fields, raw_response, response_parsed)
  are injected as locals before exec
- The script must set a variable called ``result`` which must be a dict whose
  values are booleans, integers, or strings — matching the output_fields
  declared in scoring_criteria
- Any exception is caught and returned as an error string so the evaluation
  run records the failure rather than crashing

This is intentionally simple.  It is NOT a proper sandbox — a user with Django
admin access or the ability to edit evaluation configs can still cause harm.
Treat eval-config edit access like server access.
"""

from __future__ import annotations

import datetime
import json
import math
import re
import traceback
import urllib.parse

import requests

SAFE_BUILTINS = {
    "abs": abs,
    "float": float,
    "int": int,
    "max": max,
    "min": min,
    "round": round,
    "sum": sum,
    "bool": bool,
    "chr": chr,
    "dict": dict,
    "list": list,
    "ord": ord,
    "repr": repr,
    "set": set,
    "str": str,
    "tuple": tuple,
    "type": type,
    "all": all,
    "any": any,
    "enumerate": enumerate,
    "filter": filter,
    "map": map,
    "range": range,
    "sorted": sorted,
    "zip": zip,
    "getattr": getattr,
    "hasattr": hasattr,
    "isinstance": isinstance,
    "len": len,
    "vars": vars,
    "Exception": Exception,
    "ValueError": ValueError,
    "KeyError": KeyError,
    "TypeError": TypeError,
    "NameError": NameError,
    "IndexError": IndexError,
    "AttributeError": AttributeError,
    "StopIteration": StopIteration,
    "print": print,
    "json": json,
}

ALLOWED_MODULES = {
    "datetime": datetime,
    "math": math,
    "re": re,
    "requests": requests,
    "urllib": urllib,
}

_ALLOWED_MODULE_NAMES = frozenset(ALLOWED_MODULES)


def _restricted_import(name, *args, **kwargs):
    """Allow importing only pre-whitelisted modules."""
    if name not in _ALLOWED_MODULE_NAMES:
        raise ImportError(f"Import of '{name}' is not allowed in evaluation scripts.")
    return ALLOWED_MODULES[name]


def run_python_eval(script: str, row_locals: dict) -> dict | str:
    """
    Execute a Python evaluation script against a single test-run row.

    ``row_locals`` should contain at least:
      - ``input_fields``          – dict of input column values
      - ``expected_output_fields`` – dict of expected output column values
      - ``raw_response``           – raw string response from the LLM
      - ``response_parsed``        – parsed JSON object (dict/list) or None

    The script must set a variable named ``result``, which must be a dict.
    Dict values may be booleans, ints, floats, or strings.

    Returns either the result dict (on success) or an error string (on
    failure). The caller stores the error string in EvaluationResult.notes
    and leaves the assessment empty so the row is not counted as correct.
    """
    restricted_globals: dict = {
        "__builtins__": {**SAFE_BUILTINS, "__import__": _restricted_import},
        **ALLOWED_MODULES,
    }

    local_vars = dict(row_locals)

    try:
        exec(script, restricted_globals, local_vars)  # noqa: S102
    except Exception:
        return f"Script execution error:\n{traceback.format_exc()}"

    if "result" not in local_vars:
        return (
            "Script did not set a 'result' variable. "
            "The script must assign a dict to 'result' before it finishes."
        )

    raw = local_vars["result"]

    if not isinstance(raw, dict):
        return (
            f"'result' must be a dict, got {type(raw).__name__}. "
            "Example: result = {'correct': True}"
        )

    return raw
