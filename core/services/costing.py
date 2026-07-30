"""Rough AUD cost estimates from per-1M-token model rates."""

from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

MILLION = Decimal("1000000")


def estimate_cost_aud(
    cost_per_1m_input: Optional[Decimal],
    cost_per_1m_output: Optional[Decimal],
    input_tokens: int,
    output_tokens: int,
) -> Optional[Decimal]:
    """
    Return rough AUD cost, or None if either rate is missing.

    Formula: (input_tokens / 1e6) * in_rate + (output_tokens / 1e6) * out_rate
    """
    if cost_per_1m_input is None or cost_per_1m_output is None:
        return None
    total = (
        (Decimal(input_tokens) / MILLION) * Decimal(cost_per_1m_input)
        + (Decimal(output_tokens) / MILLION) * Decimal(cost_per_1m_output)
    )
    return total.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def format_aud(amount: Optional[Decimal]) -> str:
    """Format an AUD amount for display, or an em dash when unknown."""
    if amount is None:
        return "—"
    return f"A${amount:.4f}"


def rough_token_estimate(
    prompt_chars: int,
    row_count: int,
    default_max_tokens: int,
) -> tuple[int, int]:
    """
    Heuristic pre-run token estimate.

    Input ≈ chars/4 per row; output ≈ min(default_max_tokens, input_per_row) per row.
    """
    if row_count <= 0:
        return 0, 0
    input_per_row = max(1, prompt_chars // 4)
    output_per_row = min(max(1, default_max_tokens), input_per_row)
    return input_per_row * row_count, output_per_row * row_count
