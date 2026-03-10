# LLM Evaluation Workbench — Technical Proposal

**Project:** LLM Evaluation Workbench  
**Team:** Clinical Informatics Centre, Royal Melbourne Hospital  
**Date:** March 2026  
**Status:** Proposal / Pre-development

---

## 1. Purpose

This document proposes a web-based tool for systematically evaluating Large Language Model (LLM) outputs against structured test data. The tool allows the Clinical Informatics team to upload test datasets, define prompt templates, run those prompts across one or more LLMs, and then evaluate the outputs through automated checks, AI-based judging, and human review.

The core goal is to provide a repeatable, auditable, and comparable way to assess whether an LLM produces correct and useful outputs for clinical informatics use cases — before any model or prompt is deployed into production workflows.

---

## 2. Key Concepts

**Test Case:** A named container for a particular evaluation task (e.g. "Extract diagnosis from ED notes", "Classify nitrous oxide presentations"). Each test case can have multiple versions of its input data.

**Test Case Version:** A specific CSV/Excel upload for a test case. Column names follow a convention: columns prefixed with `input_` are fed into the prompt; columns prefixed with `output_` are used for scoring. Re-uploading data creates a new version, preserving history.

**Prompt Template:** A reusable text template with `{column_name}` placeholders that get filled from `input_` columns. Multiple prompt templates can exist per test case, enabling prompt comparison.

**Model Configuration:** A saved record of how to reach a particular LLM — API endpoint, key, model name, temperature, rate limit. These persist across runs and are managed in a dedicated admin section.

**Test Run:** A single execution of one prompt template against one model, using rows from one test case version. Produces a result for each row. Runs are immutable once completed.

**Evaluation:** An assessment of a test run's outputs. Can be automated (keyword/phrase matching), AI-judged (a second LLM evaluates the outputs), or human-reviewed. Evaluations are stored separately from runs, so the same run can be evaluated multiple times by different methods.

**Comparison:** A grouping of multiple test runs for side-by-side analysis. Used for comparing models (same prompt, different models) or comparing prompts (same model, different prompts).

---

## 3. Workflows

### 3.1 Core Evaluation Pipeline

```
Upload CSV → Create Test Case + Version
                    ↓
        Define Prompt Template(s)
                    ↓
        Configure Model(s)
                    ↓
        Create Test Run (pick template + model + row limit)
                    ↓
        Run executes autonomously (row by row, respecting rate limits)
                    ↓
        Results stored per row (prompt sent, raw response, timing, tokens)
                    ↓
        Evaluate outputs (automated / AI judge / human review)
```

### 3.2 Sample-Then-Extend Workflow

1. Create a run with a row limit (e.g. first 20 rows)
2. Review outputs — manually or via AI judge
3. If satisfied, create a continuation run that processes the remaining rows (skipping already-completed ones)
4. The continuation run links back to the original via a `parent_run_id`

### 3.3 Human Gold Standard Workflow

1. Upload a test case
2. A human reviewer steps through rows in the web UI
3. For each row, the reviewer sees the input data and scores it against the `output_` columns (boolean correct/incorrect, numeric score, free text notes)
4. These human evaluations are flagged as "gold standard"
5. When an AI judge later evaluates the same test run, its scores can be compared against the gold standard to measure the judge's own accuracy

### 3.4 AI Judge Workflow

1. Select a completed test run by its run ID
2. Choose or create a judge evaluation configuration: which model to use for judging, what prompt to give the judge, what the expected output structure is
3. The judge receives: the original input, the LLM's output, and the expected output — then scores each row
4. Judge results are stored as a separate evaluation, linked to the test run
5. If gold standard human evaluations exist, the system can compare the AI judge's results against them

### 3.5 Comparison Workflows

**Model Comparison:** Same test case version + same prompt template → run across multiple models. Group these runs into a comparison to see outputs and scores side by side.

**Prompt Comparison:** Same test case version + same model → run with different prompt templates. Group into a comparison.

**Dashboard View:** Aggregated view showing how different models or prompts perform on the same test case over time — accuracy scores, latency, token usage, trends.

---

## 4. Data Model

### 4.1 Entity Relationship Summary

```
test_case
  └── test_case_version (one per CSV upload)
        └── test_case_row (one per row in the CSV)

prompt_template (linked to test_case)

model_config (global — shared across all test cases)

test_run (links: test_case_version + prompt_template + model_config)
  └── test_run_result (one per row processed)

evaluation_config (linked to test_case)
  - type: keyword_match | ai_judge | human

evaluation_run (links: evaluation_config + test_run)
  └── evaluation_result (one per test_run_result assessed)

comparison (groups multiple test_runs for side-by-side view)
  └── comparison_member (links comparison to individual test_runs)
```

### 4.2 Table Definitions

**test_case**

| Field | Type | Notes |
|-------|------|-------|
| id | UUID | Primary key |
| name | String | e.g. "ED Diagnosis Extraction" |
| description | Text | Purpose and context |
| created_by | FK → User | |
| created_at | DateTime | |

**test_case_version**

| Field | Type | Notes |
|-------|------|-------|
| id | UUID | Primary key |
| test_case_id | FK → test_case | |
| version_number | Integer | Auto-incrementing per test case |
| original_filename | String | Name of uploaded file |
| column_names | JSON | List of all column names |
| input_columns | JSON | Columns starting with `input_` |
| output_columns | JSON | Columns starting with `output_` |
| row_count | Integer | Total rows |
| uploaded_by | FK → User | |
| uploaded_at | DateTime | |

**test_case_row**

| Field | Type | Notes |
|-------|------|-------|
| id | UUID | Primary key |
| version_id | FK → test_case_version | |
| row_number | Integer | Position in original file (1-indexed) |
| input_fields | JSON | All `input_` column values for this row |
| expected_output_fields | JSON | All `output_` column values for this row |

**prompt_template**

| Field | Type | Notes |
|-------|------|-------|
| id | UUID | Primary key |
| test_case_id | FK → test_case | |
| name | String | e.g. "v1 - simple extraction" |
| template_text | Text | Contains `{input_column_name}` placeholders |
| response_format | Enum | `json` or `free_text` |
| created_by | FK → User | |
| created_at | DateTime | |

**model_config**

| Field | Type | Notes |
|-------|------|-------|
| id | UUID | Primary key |
| name | String | Display name, e.g. "GPT-4o (Azure)" |
| provider | Enum | `azure_openai`, `openai`, `anthropic`, `local`, `custom` |
| api_endpoint | String | Full URL |
| api_key | String | Encrypted at rest |
| model_name | String | e.g. "gpt-4o", "claude-sonnet-4-20250514" |
| default_temperature | Float | |
| default_max_tokens | Integer | |
| rate_limit_rpm | Integer | Requests per minute (used for throttling) |
| is_active | Boolean | Can be disabled without deletion |
| created_by | FK → User | |

**test_run**

| Field | Type | Notes |
|-------|------|-------|
| id | UUID | Primary key (this is the "run ID" used everywhere) |
| test_case_version_id | FK → test_case_version | |
| prompt_template_id | FK → prompt_template | |
| model_config_id | FK → model_config | |
| parent_run_id | FK → test_run (nullable) | Links continuation runs to originals |
| status | Enum | `pending`, `running`, `completed`, `failed`, `cancelled` |
| row_limit | Integer (nullable) | If set, only process first N rows |
| skip_rows_from_parent | Boolean | If true, skip rows already processed in parent run |
| rows_total | Integer | How many rows will be processed |
| rows_completed | Integer | Progress counter |
| rows_failed | Integer | Error counter |
| temperature_override | Float (nullable) | If different from model default |
| prompt_snapshot | Text | Full template text at time of run (in case template is later edited) |
| created_by | FK → User | |
| created_at | DateTime | |
| started_at | DateTime (nullable) | |
| completed_at | DateTime (nullable) | |
| total_duration_seconds | Float (nullable) | Wall clock time |
| total_input_tokens | Integer | Sum across all rows |
| total_output_tokens | Integer | Sum across all rows |

**test_run_result**

| Field | Type | Notes |
|-------|------|-------|
| id | UUID | Primary key |
| test_run_id | FK → test_run | |
| test_case_row_id | FK → test_case_row | |
| prompt_sent | Text | The fully constructed prompt for this row |
| raw_response | Text | Complete LLM response |
| response_parsed | JSON (nullable) | If response_format is `json`, the parsed output |
| latency_ms | Integer | Time for this individual request |
| input_tokens | Integer | |
| output_tokens | Integer | |
| status | Enum | `success`, `error`, `timeout` |
| error_message | Text (nullable) | If status is error |
| created_at | DateTime | |

**evaluation_config**

| Field | Type | Notes |
|-------|------|-------|
| id | UUID | Primary key |
| test_case_id | FK → test_case | |
| name | String | e.g. "Keyword check - diagnosis", "GPT-4o judge" |
| eval_type | Enum | `keyword_match`, `ai_judge`, `human` |
| judge_prompt_template | Text (nullable) | For AI judge: template with `{input}`, `{output}`, `{expected}` |
| judge_model_config_id | FK → model_config (nullable) | Which model judges |
| scoring_criteria | JSON | Defines what to check — see section 4.3 |
| created_by | FK → User | |
| created_at | DateTime | |

**evaluation_run**

| Field | Type | Notes |
|-------|------|-------|
| id | UUID | Primary key |
| evaluation_config_id | FK → evaluation_config | |
| test_run_id | FK → test_run | |
| is_gold_standard | Boolean | True if this is the authoritative human review |
| status | Enum | `pending`, `in_progress`, `completed` |
| created_by | FK → User | |
| created_at | DateTime | |
| completed_at | DateTime (nullable) | |

**evaluation_result**

| Field | Type | Notes |
|-------|------|-------|
| id | UUID | Primary key |
| evaluation_run_id | FK → evaluation_run | |
| test_run_result_id | FK → test_run_result | |
| assessor_type | Enum | `ai`, `human` |
| assessor_id | String | User ID or model name |
| assessment | JSON | The actual scores/flags/notes — structure matches scoring_criteria |
| created_at | DateTime | |

**comparison**

| Field | Type | Notes |
|-------|------|-------|
| id | UUID | Primary key |
| name | String | e.g. "GPT-4o vs Claude on ED notes" |
| comparison_type | Enum | `model_comparison`, `prompt_comparison` |
| test_case_id | FK → test_case | |
| created_by | FK → User | |
| created_at | DateTime | |

**comparison_member**

| Field | Type | Notes |
|-------|------|-------|
| id | UUID | Primary key |
| comparison_id | FK → comparison | |
| test_run_id | FK → test_run | |
| label | String (nullable) | Optional display label |

### 4.3 Scoring Criteria Structure

The `scoring_criteria` JSON in `evaluation_config` defines what the evaluation checks. This is flexible enough to handle the different scoring types:

**Keyword/phrase matching (for `keyword_match` type):**

```json
{
  "checks": [
    {
      "name": "diagnosis_present",
      "type": "contains_phrase",
      "target": "full_response",
      "phrase": "glaucoma",
      "case_sensitive": false
    },
    {
      "name": "medication_in_json",
      "type": "json_key_contains",
      "json_path": "medications",
      "phrase": "timolol",
      "case_sensitive": false
    },
    {
      "name": "urgency_flag",
      "type": "json_key_equals",
      "json_path": "urgency",
      "expected_value": "high"
    }
  ]
}
```

**AI judge (for `ai_judge` type):**

```json
{
  "output_fields": [
    {"name": "overall_correct", "type": "boolean"},
    {"name": "accuracy_score", "type": "integer", "min": 0, "max": 10},
    {"name": "missing_information", "type": "text"},
    {"name": "hallucinated_content", "type": "boolean"}
  ]
}
```

**Human review (for `human` type):**

```json
{
  "review_fields": [
    {"name": "correct", "type": "boolean", "label": "Is the output correct?"},
    {"name": "quality_score", "type": "integer", "min": 1, "max": 5, "label": "Quality (1-5)"},
    {"name": "notes", "type": "text", "label": "Reviewer notes"}
  ]
}
```

---

## 5. Technical Architecture

### 5.1 Stack

| Component | Choice | Rationale |
|-----------|--------|-----------|
| Web framework | Django 5.x | ORM, admin interface, auth, mature ecosystem. Team familiarity. |
| Database | SQLite (initial) | Simple, no extra infrastructure. Persistent volume in Docker. Migrate to PostgreSQL if needed later. |
| Task queue | Celery + Redis | Runs execute asynchronously. Redis is lightweight and handles the message broker role. |
| LLM communication | `httpx` (async) | Handles different API formats (OpenAI-compatible, Anthropic, custom). Rate limiting built in. |
| Frontend | Django templates + HTMX | Keeps things simple. HTMX gives interactive feel (live progress updates, partial page refreshes) without a full JS framework. |
| Containerisation | Docker + Docker Compose | One container for Django/Celery, one for Redis. Compose manages both. |
| Persistent storage | Docker volume for SQLite file | Survives container restarts. On OpenShift, use a PersistentVolumeClaim. |

### 5.2 Container Architecture

```
docker-compose.yml
├── web (Django + Gunicorn)
│   ├── Serves the web UI
│   ├── Handles file uploads, CRUD operations
│   └── Hosts the Django admin
├── worker (Celery)
│   ├── Processes test runs asynchronously
│   ├── Processes AI judge evaluation runs
│   └── Respects per-model rate limits
├── redis
│   └── Message broker for Celery
└── volumes
    └── db_data (mounted to /app/data — holds SQLite file and uploaded CSVs)
```

### 5.3 Key Design Decisions

**Why Celery for runs?** A test run of 200 rows could take 10-30 minutes depending on the model and rate limits. This must happen in the background. Celery handles this cleanly — the user presses "Start Run", gets a run ID, and can check progress or leave and come back.

**Why HTMX instead of React?** The tool is internal, the team is small, and Django templates with HTMX give you live-updating progress bars, inline form submission, and partial page updates without the overhead of a separate frontend build. If the tool needs to scale to a wider audience later, a React frontend could be added on top of the same Django API.

**Why SQLite initially?** For a team of 5-10 users running a few hundred evaluations, SQLite is more than sufficient. It simplifies deployment (no database server to manage). The Django ORM means migrating to PostgreSQL later is a one-line settings change.

**Why store `prompt_snapshot` on test_run?** Prompt templates might be edited over time. The snapshot ensures you always know exactly what prompt was used for a given run, even if the template has since changed.

**Encryption of API keys:** API keys in `model_config` should be encrypted at rest using Django's built-in signing framework or `django-encrypted-model-fields`. They should never appear in logs or be exposed in the UI after initial entry.

---

## 6. Django App Structure

```
eval_workbench/
├── manage.py
├── config/
│   ├── settings.py
│   ├── urls.py
│   ├── celery.py
│   └── wsgi.py
├── core/                      # Main application
│   ├── models.py              # All models from section 4
│   ├── admin.py               # Django admin for model_config, etc.
│   ├── views/
│   │   ├── test_cases.py      # Upload, list, detail views
│   │   ├── prompt_templates.py
│   │   ├── model_configs.py
│   │   ├── test_runs.py       # Create, monitor, detail, results
│   │   ├── evaluations.py     # Configure, run, human review UI
│   │   ├── comparisons.py     # Create, dashboard views
│   │   └── dashboard.py       # Overview / home page
│   ├── tasks.py               # Celery tasks (run execution, AI judge)
│   ├── services/
│   │   ├── llm_client.py      # Unified LLM API client
│   │   ├── csv_parser.py      # CSV/Excel ingestion with input_/output_ convention
│   │   ├── prompt_builder.py  # Template → filled prompt
│   │   ├── scorer.py          # Keyword/phrase matching logic
│   │   └── rate_limiter.py    # Per-model throttling
│   ├── templates/             # Django templates + HTMX
│   └── urls.py
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── README.md
```

---

## 7. UI Pages (Summary)

| Page | Purpose |
|------|---------|
| **Dashboard** | Overview of recent runs, active runs, quick stats |
| **Test Cases** | List all test cases. Click into one to see versions, rows, linked templates. |
| **Upload CSV** | Upload a new CSV/Excel → creates or updates a test case. Preview of columns and first few rows. Validates `input_`/`output_` convention. |
| **Prompt Templates** | Create/edit templates for a test case. Live preview showing a sample row filled in. |
| **Model Configuration** | Add/edit/disable LLM endpoints. Test connection button. |
| **Create Run** | Pick test case version + prompt + model + row limit. "Start Run" button. |
| **Run Monitor** | Live progress (rows completed, current status, estimated time). Auto-refreshes via HTMX. |
| **Run Results** | Table showing each row: input summary, output, latency. Filterable by status. Expandable rows for full detail. |
| **Human Review** | Step through rows one at a time. Left panel: input data. Right panel: LLM output. Bottom: scoring form (configured by evaluation_config). Keyboard shortcuts for speed. Can also be used without an LLM run (pure human review of input data). |
| **AI Judge** | Select a completed run, pick/create judge config, start evaluation. Results appear alongside the original run results. |
| **Comparisons** | Create a comparison by selecting multiple runs. Side-by-side table or chart view. Aggregate stats (accuracy, latency, token cost). |
| **Comparison Dashboard** | Filterable overview: how does model X perform across test cases? How do different prompts compare? |

---

## 8. LLM Client Design

The LLM client needs to handle multiple provider APIs behind a single interface. Each provider has a slightly different request/response format.

**Supported providers:**

| Provider | API Format | Notes |
|----------|-----------|-------|
| Azure OpenAI | OpenAI-compatible | Endpoint includes deployment name |
| OpenAI | OpenAI standard | |
| Anthropic | Anthropic Messages API | Different auth header, response structure |
| Local (e.g. Ollama) | OpenAI-compatible | Usually no auth required |
| Custom | Configurable | For anything else — expects OpenAI-compatible by default |

**The client should:**

1. Accept a `model_config` and a prompt string
2. Format the request appropriately for the provider
3. Send the request with timeout handling
4. Parse the response into a standard format: `{text, input_tokens, output_tokens, latency_ms}`
5. Handle errors gracefully (timeout, rate limit, auth failure, malformed response)
6. Respect the configured rate limit (sleep between requests if needed)

**For JSON responses:** If the prompt template's `response_format` is `json`, the client should attempt to parse the response as JSON. If parsing fails, store the raw text and flag the result.

---

## 9. Considerations and Risks

### 9.1 Data Sensitivity

If test data contains patient information (even de-identified), the tool and its database must be hosted within the appropriate security boundary. On the HI box or OpenShift within the hospital network, this should be fine, but it's worth confirming with the information security team. API calls to external LLMs (Azure, OpenAI, Anthropic) would send this data outside the network — ensure this is covered under existing data processing agreements.

**Recommendation:** Include a flag on each test case indicating whether it contains sensitive data. If it does, restrict which model configurations can be used (e.g. only allow models hosted on Azure within the hospital's tenant, or local models).

### 9.2 Cost Management

Running 1,000 rows through GPT-4o could cost several dollars. Running across multiple models and prompts multiplies this. The tool should show an estimated cost before starting a run (based on average prompt length × token pricing per model) and track actual cost per run.

**Recommendation:** Add optional `cost_per_1k_input_tokens` and `cost_per_1k_output_tokens` fields to `model_config`. Display estimated and actual cost on run detail pages.

### 9.3 Rate Limiting and Timeouts

Different models have different rate limits and response times. A local model might handle 60 RPM easily, while an Azure endpoint might be capped at 10 RPM.

**Recommendation:** The `rate_limit_rpm` on `model_config` is used by the Celery task to throttle. Implement exponential backoff on 429 (rate limit) responses. Set a per-request timeout (configurable, default 120 seconds) and mark timed-out rows as failed rather than retrying indefinitely.

### 9.4 Reproducibility

LLM outputs are non-deterministic (even at temperature 0, there can be minor variation). The tool stores every input and output, which helps. But be aware that re-running the same test case may produce slightly different results.

**Recommendation:** Store the full model parameters used for each run (temperature, max_tokens, any other settings). Consider allowing a `seed` parameter for models that support it.

### 9.5 SQLite Limitations

SQLite handles concurrent reads well but only allows one write at a time. If multiple Celery workers try to write results simultaneously, there could be contention.

**Recommendation:** Use a single Celery worker initially (sufficient for the expected workload). If scaling up, either move to PostgreSQL or use Django's `ATOMIC_REQUESTS` setting with appropriate retry logic.

### 9.6 Authentication

Initial deployment uses Django's built-in user authentication (username/password, created by an admin). For future Active Directory integration, `django-auth-ldap` or `django-microsoft-auth` can be added without major architectural changes.

---

## 10. Development Phases

### Phase 1 — Core Pipeline (MVP)

Goal: Upload a CSV, define a prompt, run it against one model, see results.

Includes: test_case, test_case_version, test_case_row, prompt_template, model_config, test_run, test_run_result. Basic UI for all of the above. Celery task execution. CSV upload with `input_`/`output_` parsing. Single model run with rate limiting. Results table view.

### Phase 2 — Evaluation

Goal: Score outputs automatically, via AI judge, and via human review.

Includes: evaluation_config, evaluation_run, evaluation_result. Keyword/phrase matching scorer. AI judge workflow. Human review UI with keyboard shortcuts. Gold standard flagging.

### Phase 3 — Comparison and Dashboard

Goal: Compare runs side by side. Track performance over time.

Includes: comparison, comparison_member. Model comparison view. Prompt comparison view. Aggregate dashboard with charts. Cost tracking.

### Phase 4 — Polish and Scale

Goal: Prepare for wider use.

Includes: Active Directory authentication. PostgreSQL migration (if needed). Export results to CSV. Audit logging. Role-based access (viewer vs editor vs admin). API endpoints for programmatic access (optional).

---

## 11. Open Questions

1. **Existing tools:** Should we evaluate existing open-source LLM evaluation frameworks (e.g. promptfoo, OpenAI Evals, LangSmith) to see if any meet enough of these requirements to avoid building from scratch? The human review and hospital-specific hosting requirements may rule them out, but worth a quick look.

2. **Data retention:** How long should test run results be kept? Indefinitely, or should there be an archival/deletion policy?

3. **Notifications:** Should the tool notify users (email, Teams) when a long-running test completes? Or is polling the UI sufficient?

4. **Multi-tenancy:** If this tool is eventually used by multiple teams, should test cases and runs be scoped to teams/projects, or is a flat structure with user-based ownership sufficient?

5. **Versioning of model configs:** If an API endpoint or key changes, should the old config be preserved (for audit trail) or updated in place?
