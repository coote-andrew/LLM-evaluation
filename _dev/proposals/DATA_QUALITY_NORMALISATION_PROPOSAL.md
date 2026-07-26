# Data Quality Reporting and Selective Normalisation Proposal

> **Status: discussion only — not approved for implementation.**
>
> This proposal describes a possible direction for making uploaded spreadsheet
> data easier to assess and use. It is deliberately not an implementation
> commitment. The difficult part is balancing useful, trustworthy quality
> feedback against configuration, processing, storage, and user-interface
> complexity. The project should only proceed after deciding that the value for
> researchers justifies that complexity.

## Problem

Uploaded CSV and Excel manifests may contain both detailed free-text values and
small categorical fields. The existing `input_*` / `output_*` convention makes
the upload format flexible, but it does not describe what each column means or
what values it accepts.

This leads to several data-quality problems:

- A numeric-looking result column such as `output_pvalue` can contain numbers,
  `not reported`, `N/A`, and `NA`.
- Boolean-like fields can contain `yes`, `no`, `Y`, `TRUE`, blanks, or
  unexpected phrases.
- Entity names such as funding bodies can differ only in capitalisation,
  punctuation, whitespace, or a known alias:
  `the Bill and melinda Gates foundation` versus
  `Bill and Melinda Gates Foundation`.
- High-cardinality columns may be legitimate free text or identifiers, rather
  than an error that needs cleaning.

Blindly modifying uploaded values would be unsafe. In particular, `not
reported`, missing, and not applicable may carry different research meanings.
Likewise, a similarity between organisation names is not proof that they are
the same organisation.

## Goals

- Preserve every submitted cell value for provenance and reproducibility.
- Surface likely data-quality issues without requiring researchers to manually
  inspect large spreadsheets.
- Support optional, column-specific rules for fields where standardisation is
  valuable.
- Make issues reviewable at scales from tens of rows to 112,000+ rows.
- Avoid treating high cardinality as an error by default.
- Make any applied mapping or normalisation explicit, reviewable, and stable
  for a dataset version.

## Non-goals

- Replacing Excel or source-system data cleaning workflows.
- Automatically correcting data based on fuzzy name matching or an LLM.
- Making every `output_*` column categorical.
- Rendering all unique values, aliases, or offending rows in HTML.
- Blocking every upload because it contains imperfect data.
- Retrospectively changing values used by existing test runs or evaluations.

## Current behaviour

The importer stores `input_*` and `output_*` values in JSON fields on each
dataset row. Apart from the required column prefixes, there is no persisted
per-column schema, allowed-value set, missing-value policy, or entity-alias
registry.

Some comparison code can ignore case and edge punctuation when configured.
Boolean interpretation used in sensitivity/specificity is more permissive: an
unrecognised non-empty string is currently interpreted as positive. That is
reasonable for a "value present" test but is unsafe as a general Yes/No
normalisation rule.

## Proposed approach

Treat this as three separate layers rather than one generic "clean data"
feature:

1. **Raw submitted value** — retained unchanged and always exportable.
2. **Quality report** — reports likely issues and summaries; it does not alter
   the raw data.
3. **Optional normalised value** — produced only by an explicit column rule,
   with the rule and result preserved for audit.

The initial delivery could stop at layer 2. It would provide useful visibility
without introducing a second representation of every value or making
normalisation decisions prematurely.

## Column definitions

A project could optionally define a small schema for selected columns. Columns
without a definition remain unconstrained text.

Possible types are:

- **Free text:** no value-level validation; report only blanks and basic format
  statistics.
- **Identifier:** high cardinality expected; optionally validate a supplied
  regular expression or length.
- **Boolean:** accepted positive and negative aliases, with an explicit policy
  for blank and unrecognised values.
- **Number / p-value:** accepted numeric notation and an explicit policy for
  comparison operators such as `<0.001`.
- **Date:** an agreed date format or a deliberately limited set of formats.
- **Categorical:** a small allowed list, optionally with aliases.
- **Entity/reference:** canonical values plus reviewed aliases, for example a
  funding-body registry.

Definitions must be optional. Requiring researchers to classify every input
and output column would make a flexible upload process burdensome and could
encourage inaccurate metadata.

## Missingness is not one category

For a field such as `output_pvalue`, the report should distinguish at least:

- a parsable value, such as `0.05`;
- blank/missing;
- not reported;
- not applicable;
- invalid or unrecognised text.

Whether `N/A` means "not applicable" or merely "not available" cannot be
reliably inferred from the spelling. A column rule may designate aliases, but
the application should not collapse these meanings automatically.

## Boolean rules

Boolean columns should use an explicit vocabulary, for example:

```text
positive: yes, y, true, 1
negative: no, n, false, 0
blank: missing
anything else: unrecognised
```

The report would count recognised positive/negative values, blanks, and
unrecognised values. It should show only a capped sample of the latter and
offer export for the complete list. Unknown non-empty values must not silently
be treated as positive for a column declared Boolean.

## Entity names and aliases

Entity columns need two deliberately separate levels of matching:

### Safe formatting key

Use a reversible comparison key for review only, for example:

- Unicode normalisation;
- trimming;
- collapsing repeated whitespace;
- case folding;
- optionally standardising superficial punctuation.

This can identify likely variants such as differently capitalised versions of
the same funding-body name. The raw value remains unchanged.

### Reviewed canonical mapping

Map an exact submitted value or safe formatting key to a selected canonical
entity only after a user confirms the relationship. The mapping should record
the canonical name, source alias, creator, and creation time.

Fuzzy matching and LLM-based suggestions might be considered later as
review-assistance tools, but must never automatically merge values. The cost
of a false merge is higher than the inconvenience of reviewing a candidate.

## Data quality report

The report is a compact triage view, not a browser for all unique values. Each
column summary can include:

- configured/inferred type;
- total row count, blank count, and non-blank count;
- exact or thresholded distinct count;
- valid, invalid, unrecognised, or unmapped count where a rule exists;
- a capped list of common values or common invalid values;
- count of possible formatting-normalisation collisions;
- links to downloadable detailed exceptions or alias candidates.

For example, a funding-body column may say:

```text
112,000 rows
12,480 submitted distinct values
12,021 distinct formatting keys
287 potential variant groups
9,420 values not mapped to a canonical entity
```

The HTML page would show a small number of the most consequential groups. A
CSV export can contain the complete candidate or exception list with row
numbers, raw values, normalised comparison keys, counts, and reasons.

## Scalability and processing

The report must be designed for large imports rather than expanded from a
small-dataset user interface.

- Never render every distinct value or every invalid row in HTML.
- Maintain counters and bounded examples while reading data.
- For configured categorical and entity columns, calculate exact counts and
  retain enough data to export full exceptions.
- For unconfigured free-text columns, use a configurable distinct-value
  threshold. Once exceeded, report "at least N distinct values" and stop
  retaining value-level detail.
- Limit displayed samples and top-value lists to a fixed size.
- For large files, generate the report after a successful import in a
  background task, with a status page rather than holding the request open.
- Store aggregate report results and downloadable exception exports separately
  from the row data, with retention limits appropriate to deployment storage.

Exact distinct-value calculation requires retaining a set of values and can
be expensive for unrestricted text. The design should therefore reserve exact
value-level analysis for columns where it is useful, rather than performing it
indiscriminately for every spreadsheet column.

## Versioning and auditability

Raw uploaded values must remain immutable for a dataset version. If the
application later supports normalised values or canonical mappings:

- rules should be defined at the project level for reuse;
- the effective rule set should be copied or otherwise frozen for each dataset
  version;
- reports, exports, scoring, and evaluation should state whether they use raw
  or normalised values;
- changing a rule must not silently change a historical dataset version,
  test-run result, or evaluation.

This protects reproducibility while allowing new uploads to benefit from
improved mappings.

## Possible delivery stages

### Stage 1: reporting only

Generate non-blocking upload reports for blanks, column sizes, bounded
distinctness, and simple invalid-value patterns. No data is transformed.

### Stage 2: selected column definitions

Allow project owners to declare Boolean, numeric, categorical, and
free-text/identifier columns. Use these definitions to improve reporting and
to make validation more precise.

### Stage 3: reviewed alias mapping

Add canonical entity values and reviewed aliases, starting with a limited
number of high-value entity columns such as funding body.

### Stage 4: optional normalised-data consumers

Only after the earlier stages prove useful, permit selected reports or
evaluations to explicitly consume normalised values. This requires clear
versioning and audit controls.

Each stage should be independently evaluated before proceeding. Stages 2–4
may not be worth implementing if upload reports alone provide sufficient value.

## Key decisions required before approval

1. Which researchers or roles may create column definitions and approve entity
   aliases?
2. Which columns are important enough to configure initially?
3. Should reports be advisory only, or should selected schema violations block
   an import?
4. Which missing-value categories are meaningful for each configured field?
5. What data-retention and access rules apply to exception exports, which may
   reproduce sensitive row-level data?
6. What size thresholds and background-processing capacity are acceptable for
   the deployment?
7. Is the benefit of canonical entity reporting sufficient to justify a
   mapping-review workflow?

## Complexity and risk

The technical challenge is not counting values; it is ensuring that a
seemingly convenient cleaning feature does not introduce false equivalences,
confuse users about which value is authoritative, or make large uploads slow
and costly.

The proposal adds potential new concepts: column schemas, missingness policies,
normalisation rules, report jobs, stored aggregates, downloadable sensitive
data, alias review, and versioned configuration. These concepts affect import,
export, evaluation, permissions, background work, and testing. A narrowly
scoped reporting-only implementation has much lower risk and should be
considered the default starting point if this work is approved.

## Success criteria

If approved, the work is successful when:

- researchers can identify likely inconsistent values without opening a
  112,000-row spreadsheet;
- reports remain responsive and bounded for large imports;
- free-text and identifier fields are not falsely presented as data-quality
  failures merely because they have many distinct values;
- raw submitted data remains available and unchanged;
- no automatic alias merge or boolean inference changes research meaning;
- normalisation, if later enabled, is explicit, reviewable, and reproducible
  for every dataset version.
