# Grouped Upload Feature Proposal

## Problem

The LLM evaluation workbench processes one file row → one test case row → one LLM call.
For tasks like discharge summary generation, a single evaluation unit (one admission)
spans multiple rows in the source export (one row per clinical note). The current upload
pipeline cannot represent this natively.

## Proposed Solution: Grouped Upload Mode

Add an optional "group by" mode to the existing CSV/Excel upload flow. When enabled,
the parser aggregates multiple source rows into a single `TestCaseRow`, with static
(admission-level) fields hoisted to the top level and per-note fields collected into a
JSON array (`input_notes`).

## Data Model

No schema changes required. `TestCaseRow.input_fields` is already a `JSONField` and
supports nested structures natively.

### Example Source File

| input_csn | input_admission_date | input_discharge_date | input_unit | input_note_date | input_note_text        |
|-----------|---------------------|---------------------|------------|-----------------|------------------------|
| 111       | 2026-01-01          | 2026-01-05          | ICU        | 2026-01-01      | Arrived stable         |
| 111       | 2026-01-01          | 2026-01-05          | ICU        | 2026-01-03      | Review by Dr Smith     |
| 222       | 2026-01-10          | 2026-01-12          | Ward 4     | 2026-01-10      | Admitted post-op       |

### Resulting `TestCaseRow.input_fields` (one per admission)

```json
// Row 1 — two notes
{
  "input_csn": "111",
  "input_admission_date": "2026-01-01",
  "input_discharge_date": "2026-01-05",
  "input_unit": "ICU",
  "input_notes": [
    {"input_note_date": "2026-01-01", "input_note_text": "Arrived stable"},
    {"input_note_date": "2026-01-03", "input_note_text": "Review by Dr Smith"}
  ]
}

// Row 2 — one note
{
  "input_csn": "222",
  "input_admission_date": "2026-01-10",
  "input_discharge_date": "2026-01-12",
  "input_unit": "Ward 4",
  "input_notes": [
    {"input_note_date": "2026-01-10", "input_note_text": "Admitted post-op"}
  ]
}
```

## Upload Form Changes

Two new optional fields added to `TestCaseUploadForm` in `core/forms.py`:

| Field             | Type                   | Description |
|-------------------|------------------------|-------------|
| `group_by_columns` | Comma-separated text  | Column names that are **static per group** (e.g. `input_csn, input_admission_date, input_discharge_date, input_unit`). Defines the composite group key. |
| `sort_by_column`  | Text (optional)        | Column name to **sort notes** within each group (e.g. `input_note_date`). |

**Aggregation rule:** Any `input_*` column that is **not** listed in `group_by_columns`
is treated as a note-level column and collected into `input_notes`. The user only needs
to specify the static (admission-level) columns.

If `group_by_columns` is left blank, the upload behaves identically to the current flat
mode — no behaviour change for existing users.

## Parser Changes (`core/services/csv_parser.py`)

### What stays the same

`parse_csv` and `parse_excel` are unchanged — they always return a flat list of rows.

### New function: `group_rows`

```python
def group_rows(
    rows: list[dict],
    group_by_cols: list[str],
    sort_by_col: str | None = None,
) -> list[dict]:
    """
    Aggregate flat rows into one row per unique group-key.

    Static fields (group_by_cols) are hoisted to the top level of input_fields.
    All remaining input_* fields are collected into input_notes as a list of dicts.
    """
```

Algorithm:

1. For each source row, compute the **composite group key** from the values of
   `group_by_cols` in `input_fields`.
2. Accumulate rows in insertion-order groups (preserves the source file's admission
   ordering).
3. For each group:
   - Static fields come from the **first row** in the group (values are assumed
     consistent across all rows — see Out of Scope below).
   - Note-level fields are a list of dicts, one per source row, containing all
     `input_*` keys **not** in `group_by_cols`.
   - If `sort_by_col` is set, sort the note list by that key before storing.
4. Return one output row per group, with `row_number` assigned sequentially.

### `parse_upload` signature change

```python
def parse_upload(
    file_content: bytes,
    filename: str,
    group_by_columns: list[str] | None = None,
    sort_by_column: str | None = None,
) -> dict:
```

When `group_by_columns` is non-empty, `parse_upload` calls `group_rows()` on the flat
result before returning. The returned `row_count` reflects the number of groups, not
source rows.

## View Changes (`core/views/cases.py`)

Extract the two new fields from `form.cleaned_data`, split `group_by_columns` on commas
and strip whitespace, then pass to `parse_upload`:

```python
raw_group_by = form.cleaned_data.get("group_by_columns") or ""
group_by_columns = [c.strip() for c in raw_group_by.split(",") if c.strip()]
sort_by_column = form.cleaned_data.get("sort_by_column") or None

parsed = parse_upload(
    content,
    filename,
    group_by_columns=group_by_columns or None,
    sort_by_column=sort_by_column,
)
```

The transaction loop that creates `TestCaseVersion` + `TestCaseRow` objects is
**unchanged**.

## Template (`core/templates/core/testcase_upload.html`)

Add a collapsible or clearly labelled "Grouped upload (optional)" section beneath the
existing file field, with short help text explaining the column naming convention.

## Prompt Template Usage

After upload, the static fields are referenced directly and `input_notes` holds the
note array:

```
Patient: {{input_csn}}
Admitted: {{input_admission_date}}  Discharged: {{input_discharge_date}}
Unit: {{input_unit}}

Clinical notes (chronological):
{{input_notes}}

Write a discharge summary.
```

The agent receives the full JSON array for `input_notes` and processes it in one shot.

## Testing

New test class `GroupedUploadTests` in `core/tests.py` covering:

| # | Scenario |
|---|----------|
| 1 | Two admissions, multiple notes each — correct grouping and row count |
| 2 | Sort order applied within a group |
| 3 | Single-note admission included correctly |
| 4 | Missing sort-column value — row sorts last (stable fallback) |
| 5 | Blank `group_by_columns` — falls through to flat mode unchanged |
| 6 | `group_by_columns` names a column absent from the file — validation error raised |
| 7 | Composite group key (multiple static columns) works correctly |
| 8 | Works identically for CSV and Excel input |

## Migration

None required.

## Out of Scope

- **Static field consistency validation.** If `input_admission_date` differs across rows
  in the same `csn` group, the first row's value is used silently. A future warning
  could flag this.
- **UI column picker.** A JavaScript dropdown populated after file selection would
  improve UX but is not required for the initial implementation; a plain text input is
  sufficient.
- **`output_*` columns in grouped mode.** An `output_expected_summary` column can still
  be included in the flat export; it will be taken from the first row of each group.
  This supports gold-standard evaluation workflows.
