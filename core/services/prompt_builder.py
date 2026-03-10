"""
Template → filled prompt builder.

Replaces {column_name} placeholders with values from input_fields.
"""

from typing import Any


def build_prompt(template_text: str, input_fields: dict[str, Any]) -> str:
    """
    Fill template with input_fields. Placeholders use {input_column_name} syntax.
    """
    return template_text.format(**input_fields)


def get_placeholder_names(template_text: str) -> set[str]:
    """
    Extract {placeholder} names from template. Returns set of names.
    """
    import re
    return set(re.findall(r"\{(\w+)\}", template_text))


def validate_template(template_text: str, input_columns: list[str]) -> tuple[bool, list[str]]:
    """
    Check that all placeholders in template exist in input_columns.
    Returns (is_valid, list of missing columns).
    """
    placeholders = get_placeholder_names(template_text)
    input_set = set(input_columns)
    missing = [p for p in placeholders if p not in input_set]
    return len(missing) == 0, missing
