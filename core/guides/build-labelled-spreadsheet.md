---
title: Build a labelled spreadsheet
summary: Compile notes any way you like; match input_/output_ names to the prompt
group: Start here
order: 20
---

The workbench only needs a simple Excel or CSV file: text the model should read,
and the answers you already know (or have hand-labelled). Everything else —
prompts, runs, scoring — hangs off those column names.

> **One rule:** column names must match. The prompt uses `{input_…}` placeholders
> for what goes *in*, and the model should return JSON keys with the same names
> as your `output_…` columns for what you want to score.

## What you actually need

At minimum:

- **One input column** whose name matches the curly-brace placeholder in your
  prompt — e.g. column `input_note_text` pairs with `{input_note_text}`.
- **One or more output columns** starting with `output_`, filled with your
  hand-evaluated (or known) results — e.g. `output_flagged` with values like
  `yes` / `no`.

It is also worth adding columns that help *you* find the case again later —
a visit ID, MRN, date, ward, etc. Those do not need to go into the prompt. The
model never sees them unless you put them in curly brackets.

## How to compile the rows

Any way you like. Common options:

- Start from a workbench **export** and edit it
- Copy and paste free-text notes from the clinical system
- Type notes by hand for a small gold set
- Mix known positive cases, model-flagged cases you have already reviewed, and a sample of negatives

You do not need a perfect pipeline. A small, carefully labelled sheet is enough
to evaluate a prompt and iterate.

> **Review burden:** if you later run a broader screen (more months, looser
> criteria), every flagged case still needs a human look. A wide net can mean
> hours of review — plan the dataset size for the labelling time you actually have.

## Example sheet

Four rows is enough to see the pattern. The middle columns are for you; only
`input_note_text` is sent to the model (because that is what the prompt asks for).

| input_note_text | input_visit_id | output_category | output_flagged |
| --- | --- | --- | --- |
| Free-text note for case 1… | helps matching — not required by the model | alpha | yes |
| Note copied from the clinical system… | neither is this used by the LLM | beta | yes |
| Another note… | 1241525 | alpha | no |
| Short note typed by hand… | 125125125 | beta | no |

## Make the prompt ask for the same names

If you want to measure how well the model recovers `output_flagged`, tell it to
return JSON with that exact key — typically `yes` or `no`. If you also care
about `output_category`, ask for that key too.

**In the spreadsheet:** `output_flagged`, `output_category`

**In the prompt (idea):**

```
Read the note:
{input_note_text}

Return JSON only:
{
  "output_flagged": "yes" or "no",
  "output_category": "alpha" or "beta"
}
```

Once the names line up, field-match evaluation (and similar configs) can compare
the model’s JSON to your labelled columns without extra glue code.

## Practical tips when building a gold set

1. **Include variety, not just positives.** Known positives, reviewed model hits,
   and a sample of true negatives give a fairer picture when you tweak the prompt.
2. **Keep an ID you can look up.** Visit ID, MRN, and date save time when a row looks wrong.
3. **Name columns for the job.** If the question is “was this case flagged?”, use
   something clear like `output_flagged` — and ask the model for that same key.
4. **Start small, then widen carefully.** A tight set is good for prompt iteration.
   Expanding to “all months” is useful, but budget review time for every new flag.
5. **Optional side experiments.** A second, very simple prompt (narrower question)
   can be a cheap way to spot misses — just don’t over-invest if yield is low.

## Upload and run

1. Create or open a project and upload the CSV/Excel as a new version.
2. Add a prompt that uses your `{input_…}` placeholders and asks for JSON keys matching `output_…`.
3. Run against a model, then attach a field-match (or other) evaluation config that scores those fields.
