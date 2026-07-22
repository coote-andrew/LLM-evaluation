---
title: Getting started
summary: Create a project, run a model, and score results
group: Start here
order: 10
---

The workbench helps you compare LLM prompts and models against a labelled dataset.
Work is organised around a **project**: one dataset, many prompts, runs, and evaluation configs.

> **Typical loop:** upload data → write prompt → run model → score with an evaluation config → iterate.

## Five-minute path

1. **Create a project** — Projects → upload a CSV (or zip bundle with `file_*` columns).
2. **Add a prompt template** — Use `{input_*}` placeholders that match your columns.
3. **Configure a model** — Models → provider, endpoint, API key, model name.
4. **Start a run** — Runs → New run: pick project version, prompt, model.
5. **Evaluate** — From the completed run, choose an evaluation config (or create one).

## Dataset conventions

Column prefixes control how rows are interpreted:

- `input_*` — fed into prompts via placeholders
- `output_*` — ground truth for scoring
- `file_*` — relative paths inside an uploaded zip bundle

```
input_note,input_unit,output_primary_code,output_label
"Patient presents with…","ED","73211009","Diabetes mellitus"
```

## Next

See **Build a labelled spreadsheet** for a practical walkthrough of compiling notes and lining up column names with your prompt.
