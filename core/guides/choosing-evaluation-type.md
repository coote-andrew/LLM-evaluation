---
title: Choosing an evaluation type
summary: Plain-language guide to keyword, field match, Python, AI judge, and human review
group: Evaluation types
order: 10
---

An evaluation config is a reusable scoring recipe. After a model run finishes,
you apply a config to mark each row correct or incorrect. Start with the simplest
option that fits; you can add more configs later.

## Keyword match

**In plain English:** “Does the answer mention X?” or “Does this JSON field equal Y?”

Good first step. Doesn’t require the whole answer to be structured — just checks you define.

**Best for:** smoke tests, required phrases, checking a key exists.

## Field match

**In plain English:** “The answer is a form. Compare each box to the expected value in my spreadsheet.”

Exact = text must match. LLM judge (per field) = another model decides if meaning matches when wording differs.

**Best for:** JSON outputs with clear expected columns (`output_*`).

## Python script

**In plain English:** “I’ll write a short rule myself for what counts as correct.”

Useful for list membership, regex, validating codes against an API, or combining several conditions.

**Best for:** domain rules the other types can’t express.

## AI judge

**In plain English:** “Ask another AI to mark this answer right or wrong, with a short reason.”

Scores the whole response (not just one field). Costs an extra model call per row.

**Best for:** free-text answers where meaning matters more than exact wording.

## Human review

**In plain English:** “A person clicks through each row and scores it.”

Define yes/no questions, numeric scores, and notes. Often used as a gold standard to compare against automatic methods.

**Best for:** calibration, edge cases, and authoritative labels.

> Tip: many projects keep both a fast automatic config (field/keyword) and a slower AI or human config for deeper checks.
